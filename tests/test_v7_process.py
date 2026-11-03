"""Exercise actual process cancellation without a paid model call."""
import asyncio
import json
import os
from pathlib import Path
import sys

from aichat_agent import service
from aichat_agent.gateway import GatewayError


def test_invalidated_lease_stops_worker_and_its_children(tmp_path, monkeypatch):
    marker = tmp_path / 'child.json'
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        code = "import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(json.dumps(p.pid)); time.sleep(60)"
        proc = await real_spawn(sys.executable, '-c', code, str(marker), **kwargs)
        processes.append(proc)
        return proc

    class API:
        async def call(self, method, path, *args, **kwargs):
            if path.endswith('/runtime'):
                return {'config': {'api_key': 'test', 'timeout_seconds': 10}, 'session': {'session_key': 'test'}}
            if path.endswith('/renew'):
                raise GatewayError(409, 'lease_not_held')
            raise AssertionError(path)

    async def exercise():
        controller = service.AgentService('http://127.0.0.1:1', 'test', tmp_path/'state')
        await controller.api.close()
        controller.api = API()
        monkeypatch.setattr(service.asyncio, 'create_subprocess_exec', spawn)
        await asyncio.wait_for(controller.execute({'task': {'id': 1, 'profile': 'delivery'}, 'task_token': 'test'}), 5)
        assert marker.exists()  # worker genuinely started a child
        assert processes[0].returncode is not None
        child_pid = json.loads(marker.read_text())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(.05)
        else:
            # Linux can retain a dead orphan as a zombie until init reaps it.
            stat = Path(f'/proc/{child_pid}/stat')
            assert stat.exists() and stat.read_text().split()[2] == 'Z'

    asyncio.run(exercise())


def test_worker_environment_excludes_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'ambient-secret')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'unrelated-token')
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted-proxy')
    env = service.worker_environment(tmp_path, {'api_key': 'configured-key', 'base_url': 'https://model.example'})
    assert env['HOME'] == str(tmp_path)
    assert env['ANTHROPIC_API_KEY'] == 'configured-key'
    assert not {'AWS_SECRET_ACCESS_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDECODE','HTTPS_PROXY'} & env.keys()
