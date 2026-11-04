import asyncio
from pathlib import Path

import pytest

from aichat import auth, chat, config, db, tasks, transcription as stt
from aichat_bridge import prompts


def voice(env, *, room=None, auto=False):
    env.cfg.asr_auto_agent = auto
    room = room or env.dm
    a = env.a.post(f'/api/rooms/{room}/attachments', files={'file': ('voice.webm', b'\x1aE\xdf\xa3' + b'\0' * 200, 'audio/webm')}, data={'duration_ms': '3000'}).json()['attachment']
    return env.say(env.a, room, '', attachment_id=a['id'])


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(stt, 'availability', lambda: {'error': None, 'ready': True})


class FakeWorker:
    def __init__(self, fn=None):
        self.calls = 0
        self.fn = fn

    async def recognize(self, a):
        self.calls += 1
        if self.fn:
            self.fn(a)
        return {'text': '明天下午三点开会。'}


def test_manual_asr_without_agent_reuses_result_and_preserves_voice(env, ready):
    assert env.a.post(f'/api/rooms/{env.dm}/agent', json={'enabled': False}).status_code == 200
    m = voice(env)
    before = db.one('SELECT COUNT(*) n FROM messages')['n']
    r = env.a.post(f"/api/messages/{m['id']}/transcribe")
    assert r.status_code == 200, r.text
    assert r.json()['transcription']['status'] == 'queued'
    assert env.b.post(f"/api/messages/{m['id']}/transcribe").json()['transcription']['attachment_id'] == m['attachment']['id']
    w = FakeWorker()
    assert asyncio.run(stt.run_one(w))
    data = env.b.get(f"/api/messages/{m['id']}/transcription").json()['transcription']
    assert data['status'] == 'done' and data['text'] == '明天下午三点开会。'
    assert env.a.post(f"/api/messages/{m['id']}/transcribe").json()['transcription']['status'] == 'done'
    assert not asyncio.run(stt.run_one(w)) and w.calls == 1
    row = db.one('SELECT * FROM messages WHERE id=?', (m['id'],))
    assert row['body'] == '' and row['version'] == 2
    assert db.one('SELECT COUNT(*) n FROM messages')['n'] == before
    assert db.one("SELECT COUNT(*) n FROM events WHERE message_id=? AND type='message.edited'", (m['id'],))['n'] == 1
    assert env.a.get(m['attachment']['url']).status_code == 200
    assert not db.one('SELECT 1 FROM agent_tasks WHERE trigger_message_id=?', (m['id'],))


def test_asr_authorization_and_visibility(env, ready):
    m = voice(env)
    for method, path in [('post', 'transcribe'), ('get', 'transcription')]:
        url = f"/api/messages/{m['id']}/{path}"
        assert getattr(env.anon, method)(url).status_code == 401
        assert getattr(env.c, method)(url).status_code in (403, 404)
        db.run('UPDATE room_members SET history_from_seq=? WHERE room_id=? AND user_id=?', (m['seq'], env.dm, env.bob_id))
        assert getattr(env.b, method)(url).status_code == 404
    text = env.say(env.a, env.dm, 'plain text')
    assert env.a.post(f"/api/messages/{text['id']}/transcribe").status_code == 400


def test_deleted_audio_cannot_be_restored_by_late_inference(env, ready):
    m = voice(env)
    env.a.post(f"/api/messages/{m['id']}/transcribe")
    def delete(a):
        chat.delete_message(m['id'], env.alice_id)
    assert asyncio.run(stt.run_one(FakeWorker(delete)))
    a = db.one('SELECT * FROM attachments WHERE id=?', (m['attachment']['id'],))
    assert a['deleted_at'] and a['transcript'] is None and a['transcription_generation'] is None
    assert env.a.get(f"/api/messages/{m['id']}/transcription").status_code == 404


def test_queue_limit_failure_retry_and_generation(env, ready):
    m = voice(env)
    env.a.post(f"/api/messages/{m['id']}/transcribe")
    class Failure:
        async def recognize(self, a):
            return {'error': 'private-file-path-or-secret'}
    assert asyncio.run(stt.run_one(Failure()))
    r = env.a.get(f"/api/messages/{m['id']}/transcription").json()['transcription']
    assert r['status'] == 'failed' and r['error'] == 'asr_failed'
    env.a.post(f"/api/messages/{m['id']}/transcribe")
    def replace(a):
        db.run("UPDATE attachments SET transcription_generation='new' WHERE id=?", (a['id'],))
    asyncio.run(stt.run_one(FakeWorker(replace)))
    assert db.one('SELECT transcript FROM attachments WHERE id=?', (m['attachment']['id'],))['transcript'] is None
    db.run("UPDATE attachments SET transcription_state='failed' WHERE id=?", (m['attachment']['id'],))
    env.a.post(f"/api/messages/{m['id']}/transcribe")
    asyncio.run(stt.run_one(FakeWorker()))
    assert env.a.get(f"/api/messages/{m['id']}/transcription").json()['transcription']['status'] == 'done'


def test_missing_model_does_not_break_chat_and_manual_error_is_explicit(env, monkeypatch):
    monkeypatch.setattr(stt, 'availability', lambda: {'error': 'asr_model_missing'})
    m = voice(env, room=env.agent_dm_a, auto=True)
    assert m['attachment']['transcription_status'] == 'failed'
    assert env.a.post(f"/api/messages/{m['id']}/transcribe").status_code == 503
    lease = env.lease()
    assert lease and lease['task']['trigger_message_id'] == m['id']
    line = prompts._msg_line(m, {env.alice_id: 'Alice'})
    assert 'asr_model_missing' in line and '不得猜测' in line


def test_agent_waits_for_local_asr_and_reads_text_in_context(env, ready):
    m = voice(env, room=env.agent_dm_a, auto=True)
    assert env.lease() is None
    assert asyncio.run(stt.run_one(FakeWorker()))
    lease = env.lease()
    assert lease and lease['task']['trigger_message_id'] == m['id']
    ctx = env.anon.get('/api/tools/whoami', headers=env.th(lease)).json()
    assert ctx['trigger_message']['attachment']['transcript'] == '明天下午三点开会。'
    assert '机器转写' in prompts._msg_line(ctx['trigger_message'], {env.alice_id: 'Alice'})
    assert env.anon.post(f"/api/tools/attachments/{m['attachment']['id']}/transcribe", headers=env.th(lease)).json()['transcription']['status'] == 'done'
    foreign = voice(env, room=env.dm)
    assert env.anon.get(f"/api/tools/attachments/{foreign['attachment']['id']}/transcription", headers=env.th(lease)).status_code == 404
    env.a.post(f"/api/tasks/{lease['task']['id']}/cancel")
    assert env.anon.get(f"/api/tools/attachments/{m['attachment']['id']}/transcription", headers=env.th(lease)).status_code in (401, 403, 409)


def test_quoted_voice_is_transcribed_before_agent_reads_it(env, ready):
    m = voice(env)
    env.cfg.asr_auto_agent = True
    q = env.say(env.a, env.dm, '@agent 帮我解释这段语音', reply_to=m['id'])
    assert env.lease() is None
    asyncio.run(stt.run_one(FakeWorker()))
    lease = env.lease()
    assert lease['task']['trigger_message_id'] == q['id']
    chain = env.anon.get(f"/api/tools/messages/{q['id']}/thread", headers=env.th(lease)).json()['chain']
    assert any(x.get('attachment', {}).get('transcript') == '明天下午三点开会。' for x in chain if x.get('attachment'))


def test_worker_timeout_kills_process_and_returns_public_code(env):
    class Input:
        def write(self, data): pass
        async def drain(self): pass
    class Output:
        async def readline(self): raise asyncio.TimeoutError()
    class Process:
        returncode = None
        stdin = Input()
        stdout = Output()
    w = stt.Worker()
    w.process = Process()
    closed = []
    async def close(): closed.append(True)
    w.close = close
    r = asyncio.run(w.recognize({'path': 'any.webm'}))
    assert r['error'] == 'asr_timeout' and closed


def test_availability_is_local_and_does_not_import_model(env, monkeypatch):
    monkeypatch.setattr(stt.importlib.util, 'find_spec', lambda p: object())
    env.cfg.asr_enabled = False
    assert stt.availability()['error'] == 'asr_disabled'
    env.cfg.asr_enabled = True
    assert stt.availability()['error'] == 'asr_model_missing'
    target = Path(env.cfg.asr_model_dir)
    target.mkdir(parents=True)
    for name in ('model.int8.onnx', 'tokens.txt'): (target / name).touch()
    monkeypatch.setattr(stt.shutil, 'which', lambda p: None)
    assert stt.availability()['error'] == 'asr_ffmpeg_missing'


def test_pending_queue_is_bounded(env, ready):
    m = voice(env)
    a = db.one('SELECT * FROM attachments WHERE id=?', (m['attachment']['id'],))
    for _ in range(32):
        db.run("INSERT INTO attachments(room_id,uploader_id,message_id,filename,mime,size,path,created_at,duration_ms,transcription_state) VALUES(?,?,?,?,?,?,?,?,?,'queued')",
               (env.dm,env.alice_id,m['id'],'voice.webm','audio/webm',a['size'],a['path'],db.now(),3000))
    r = env.a.post(f"/api/messages/{m['id']}/transcribe")
    assert r.status_code == 429 and r.json()['error']['code'] == 'asr_busy'
    assert db.one('SELECT transcription_state FROM attachments WHERE id=?', (a['id'],))['transcription_state'] is None


def test_restart_recovers_interrupted_work_and_shutdown_closes_worker(env, ready, monkeypatch):
    m = voice(env)
    env.a.post(f"/api/messages/{m['id']}/transcribe")
    db.run("UPDATE attachments SET transcription_state='running' WHERE id=?", (m['attachment']['id'],))
    closed = []
    async def recognize(self, a): return {'text': '恢复后的语音文字'}
    async def close(self): closed.append(True)
    monkeypatch.setattr(stt.Worker, 'recognize', recognize)
    monkeypatch.setattr(stt.Worker, 'close', close)
    async def run():
        task = asyncio.create_task(stt.serve())
        for _ in range(100):
            if db.one('SELECT transcript FROM attachments WHERE id=?', (m['attachment']['id'],))['transcript']:
                break
            await asyncio.sleep(.01)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    asyncio.run(run())
    assert closed and db.one('SELECT transcript FROM attachments WHERE id=?', (m['attachment']['id'],))['transcript'] == '恢复后的语音文字'
