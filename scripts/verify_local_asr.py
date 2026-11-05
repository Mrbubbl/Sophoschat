"""Real local ASR acceptance in a disposable server, never calls a paid LLM.

Requires .[asr], FFmpeg and the model bundle's test_wavs. --keep-running leaves
the disposable server available for UI checks until interrupted. Credentials
are generated and stored only in the chosen local output directory.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model-dir', type=Path, required=True)
    p.add_argument('--out', type=Path, default=ROOT / 'output/asr-acceptance')
    p.add_argument('--keep-running', action='store_true')
    args = p.parse_args()
    model = args.model_dir.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    data = out / ('data-' + secrets.token_hex(4))
    data.mkdir()
    login = {'username': 'asr_owner', 'password': secrets.token_urlsafe(18)}
    login_path = out / 'login.json'
    login_path.write_text(json.dumps(login))
    login_path.chmod(0o600)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'ASR_TEST_LOGIN': json.dumps(login),
           'AICHAT_DATA_DIR': str(data), 'AICHAT_ASR_MODEL_DIR': str(model),
           'AICHAT_START_AGENT_SERVICE': '0', 'ASR_TEST_PORT': str(port)}
    server_code = '''import json,os,uvicorn
from aichat import auth,chat
from aichat.main import create_app
app=create_app(allow_legacy_agent=True,start_agent_service=False,consolidate_quiet=3600)
u=json.loads(os.environ['ASR_TEST_LOGIN'])
uid=auth.create_user(u['username'],'本地语音验收',u['password'],'owner')
chat.bootstrap_user(uid)
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['ASR_TEST_PORT']),log_level='warning')
'''
    with (out / 'server.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-c', server_code], cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    report = {'url': url, 'engine': 'sensevoice-small-int8', 'formats': []}
    try:
        with httpx.Client(base_url=url, timeout=20, trust_env=False) as c:
            for _ in range(150):
                try:
                    if c.post('/api/login', json=login).status_code == 200: break
                except httpx.HTTPError: pass
                if server.poll() is not None: raise RuntimeError('test_server_exited')
                time.sleep(.1)
            else: raise RuntimeError('test_server_not_ready')
            assert c.get('/api/transcription/status').json()['ready']
            other = c.post('/api/users', json={'username': 'asr_peer', 'display_name': '另一位成员', 'password': secrets.token_urlsafe(18)}).json()['user']
            c.post('/api/contacts', json={'user_id': other['id']}).raise_for_status()
            room = c.post('/api/rooms', json={'type': 'dm', 'user_id': other['id'], 'include_agent': False}).json()['room']['id']
            assert c.post(f'/api/rooms/{room}/agent', json={'enabled': False}).status_code == 200
            agent_room = next(r['id'] for r in c.get('/api/rooms').json()['rooms'] if r['type'] == 'agent_dm')

            def send(file, mime, room_id=room):
                r = c.post(f'/api/rooms/{room_id}/attachments', files={'file': (file.name, file.read_bytes(), mime)}, data={'duration_ms': '1000'})
                assert r.status_code == 200, r.text
                r = c.post(f'/api/rooms/{room_id}/messages', json={'body': '', 'attachment_id': r.json()['attachment']['id']})
                assert r.status_code == 200, r.text
                return r.json()['message']

            def wait(m):
                for _ in range(400):
                    r = c.get(f"/api/messages/{m['id']}/transcription").json()['transcription']
                    if r['status'] in ('done', 'failed'): return r
                    time.sleep(.1)
                raise RuntimeError('recognition_did_not_finish')

            first = None
            for lang, ext, codec, mime in [('zh','webm','libopus','audio/webm'),('en','ogg','libopus','audio/ogg'),('yue','m4a','aac','audio/mp4')]:
                sample = out / f'{lang}.{ext}'
                subprocess.run(['ffmpeg','-v','error','-y','-i',str(model / f'test_wavs/{lang}.wav'),'-c:a',codec,str(sample)], check=True)
                m = send(sample, mime)
                t = time.monotonic()
                r = c.post(f"/api/messages/{m['id']}/transcribe")
                assert r.status_code == 200, r.text
                result = wait(m)
                assert result['status'] == 'done' and result['text'] and result['engine'] == report['engine'], result
                report['formats'].append({'language': lang, 'format': ext, 'elapsed_seconds': round(time.monotonic()-t, 3), 'text': result['text']})
                assert c.post(f"/api/messages/{m['id']}/transcribe").json()['transcription']['text'] == result['text']
                if first is None: first = sample
            assert len(c.get(f'/api/rooms/{room}/messages').json()['messages']) == 3
            report['no_extra_messages'] = True
            bad = out / 'broken.webm'
            bad.write_bytes(b'\x1aE\xdf\xa3' + b'\0'*200)
            m = send(bad, 'audio/webm')
            assert c.post(f"/api/messages/{m['id']}/transcribe").status_code == 200
            assert wait(m)['error'] == 'asr_invalid_audio'
            silence = out / 'silence.webm'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1','-c:a','libopus',str(silence)],check=True)
            m = send(silence, 'audio/webm')
            c.post(f"/api/messages/{m['id']}/transcribe")
            assert wait(m)['error'] == 'asr_no_speech'
            report['invalid_audio_and_silence_rejected'] = True
            m = send(first, 'audio/webm', agent_room)
            assert wait(m)['status'] == 'done'  # no manual transcription request
            token = c.post('/api/agent/credentials', json={'label': 'ASR context test'}).json()['token']
            lease = c.post('/api/agent/lease', headers={'Authorization': 'Bearer '+token}).json()['lease']
            assert lease and lease['task']['trigger_message_id'] == m['id']
            context = c.get('/api/tools/whoami', headers={'Authorization':'Bearer '+lease['task_token']}).json()
            assert context['trigger_message']['attachment']['transcript']
            report['agent_context_contains_local_transcript'] = True
            c.post(f"/api/agent/tasks/{lease['task']['id']}/fail", headers={'Authorization':'Bearer '+token,'X-Task-Token':lease['task_token']}, json={'error':'test_complete','retry':False})
            report['manual_room_id'] = room
            report['agent_room_id'] = agent_room
            (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            if args.keep_running:
                print(f'UI acceptance server: {url}; login file: {login_path}', flush=True)
                while True: time.sleep(1)
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try: server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()


if __name__ == '__main__':
    main()
