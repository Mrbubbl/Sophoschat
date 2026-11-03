import asyncio
import json
from pathlib import Path
import sys

import pytest

from aichat import auth,db,integrations,runtime,chat
from aichat_agent.mcpcheck import check_server
from aichat_agent.webtools import TextHTML,http_url,search,fetch


def test_registration_creates_member_and_logged_in_agent_dm(env):
    r=env.anon.post('/api/register',json={'username':'newbie','display_name':'新成员','password':'a-good-password'})
    assert r.status_code==200,r.text
    u=r.json()['user'];assert u['role']=='user' and u['display_name']=='新成员'
    assert env.anon.get('/api/me').json()['user']['id']==u['id']
    assert any(r['type']=='agent_dm' for r in env.anon.get('/api/rooms').json()['rooms'])
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=lax' in r.headers['set-cookie']
    stored=auth.get_user(u['id']);assert stored['password_hash']!='a-good-password'
    assert env.anon.get(f'/api/rooms/{env.agent_dm_a}/messages').status_code in (403,404)


def test_tool_events_require_current_service_attempt(env):
    token=runtime.service_credential();headers={'Authorization':'Bearer '+token}
    runtime.update_settings({'api_key':'test-only','model':'claude-test','enabled':True},env.alice_id)
    env.say(env.a,env.agent_dm_a,'执行测试')
    r=env.anon.post('/api/agent/lease',headers=headers,params={'engine':'claude-sdk','owner':'test'})
    lease=r.json()['lease'];tid=lease['task']['id'];control={**headers,'X-Task-Token':lease['task_token']}
    assert env.anon.post(f'/api/agent/tasks/{tid}/tool-event',headers=control,json={'name':'Bash','id':'call-1'}).status_code==200
    assert db.one('SELECT 1 FROM task_tool_receipts WHERE task_id=? AND action=?',(tid,'tool.invoked.Bash'))
    assert env.anon.post(f'/api/agent/tasks/{tid}/progress',headers=control,json={'stage':'execute'}).status_code==200
    assert env.a.get(f'/api/rooms/{env.agent_dm_a}/tasks').json()['tasks'][0]['progress']=='执行代码与命令'
    env.a.post(f'/api/tasks/{tid}/cancel')
    assert env.anon.post(f'/api/agent/tasks/{tid}/tool-event',headers=control,json={'name':'Bash','id':'old'}).status_code==409


def test_registration_cannot_promote_or_ignore_owner_setting(env):
    assert env.anon.post('/api/register',json={'username':'promote','password':'12345678','role':'owner'}).status_code==422
    assert env.b.put('/api/registration',json={'enabled':False}).status_code==403
    assert env.a.put('/api/registration',json={'enabled':False}).status_code==200
    assert env.anon.get('/api/registration').json()=={'enabled':False}
    assert env.anon.post('/api/register',json={'username':'closed','password':'12345678'}).status_code==403
    env.a.put('/api/registration',json={'enabled':True})
    assert env.anon.post('/api/register',headers={'Origin':'https://attacker.invalid'},json={'username':'origin','password':'12345678'}).status_code==403


def test_registration_validates_duplicate_and_rate_limit(env):
    assert env.anon.post('/api/register',json={'username':'alice','password':'12345678'}).status_code==409
    assert env.anon.post('/api/register',json={'username':'short','password':'123'}).status_code==422
    for i in range(4):assert env.anon.post('/api/register',json={'username':f'new{i}','password':'12345678'}).status_code==200
    assert env.anon.post('/api/register',json={'username':'limited','password':'12345678'}).status_code==429


def test_mcp_secrets_encrypted_preserved_and_revoked(env):
    body={'mcp_servers':{'notes':{'type':'stdio','command':'python','args':['secret-arg'],'env':{'KEY':'secret-env'}},'calendar':{'type':'http','url':'https://calendar.example/mcp','headers':{'Authorization':'Bearer secret-header'}}},'search':{'provider':'brave','api_key':'secret-search'}}
    assert env.b.put('/api/agent/integrations',json=body).status_code==403
    r=env.a.put('/api/agent/integrations',json=body)
    assert r.status_code==200,r.text
    for secret in ('secret-arg','secret-env','secret-header','secret-search'):
        assert secret not in r.text and secret not in db.get_kv('agent_integrations')
    redacted=r.json()['integrations']
    assert env.a.put('/api/agent/integrations',json=redacted).status_code==200
    assert integrations.configuration(secrets=True)['mcp_servers']==body['mcp_servers']
    rev=runtime.settings()['revision']
    env.a.put('/api/agent/integrations',json={'mcp_servers':{}})
    assert not integrations.configuration(secrets=True)['mcp_servers']
    assert runtime.settings()['revision']>rev
    assert env.anon.get('/api/agent/integrations').status_code==401


@pytest.mark.parametrize('servers',[
    {'platform':{'type':'stdio','command':'x'}},
    {'x':{'type':'http','url':'file:///etc/passwd'}},
    {'x':{'type':'http','url':'https://example.org/?token=secret'}},
    {'x':{'type':'stdio','command':'python','args':'not-list'}},
    {'x':{'type':'http','url':'https://example.org','headers':{'X':'a\r\nb'}}},
])
def test_mcp_validates_connection_definition(env,servers):
    assert env.a.put('/api/agent/integrations',json={'mcp_servers':servers}).status_code==422


def test_actual_mcp_stdio_probe_and_external_write(tmp_path):
    fixture=Path(__file__).parent/'fixtures'/'notes_mcp.py'
    server={'type':'stdio','command':sys.executable,'args':[str(fixture)],'env':{'NOTES_PATH':str(tmp_path/'notes.json')}}
    result=asyncio.run(check_server(server))
    assert result['status']=='connected',result
    assert set(result['tools'])=={'append_note','list_notes'}
    async def write_note():
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(fixture)],env=server['env'])) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result=await session.call_tool('append_note',{'text':'MCP actual write'})
                assert 'MCP actual write' in result.content[0].text
    asyncio.run(write_note())
    assert json.loads((tmp_path/'notes.json').read_text())==['MCP actual write']


def test_webpage_parser_excludes_script_and_collects_source_links():
    p=TextHTML();p.feed('<h1>Source</h1><script>secret-script</script><p>Fact</p><a href="/source">Reference</a>')
    assert 'secret-script' not in ''.join(p.parts)
    assert p.links==[{'url':'/source','title':'Reference'}]
    for url in ['file:///etc/passwd','javascript:alert(1)','https://user:pass@example.com']:
        with pytest.raises(ValueError):http_url(url)


def test_web_search_and_fetch_use_actual_response_content(monkeypatch):
    import httpx
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            return httpx.Response(200,request=httpx.Request('GET',url),content=b'<rss><channel><item><title>Actual title</title><link>https://example.org/fact</link><description>Actual snippet</description></item></channel></rss>')
    monkeypatch.setattr('aichat_agent.webtools.httpx.AsyncClient',Client)
    result=asyncio.run(search('fact',{}))
    assert result['results'][0]['url']=='https://example.org/fact'


def test_actual_http_mcp_connection_and_authorization_header(tmp_path):
    import contextlib,socket,threading,time
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.responses import JSONResponse
    from mcp import types
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    async def tools(ctx,params):
        return types.ListToolsResult(tools=[types.Tool(name='calendar_list',input_schema={'type':'object'})])
    manager=StreamableHTTPSessionManager(Server('http-calendar-test',on_list_tools=tools),json_response=True,stateless=True)
    async def secured(scope,receive,send):
        if dict(scope.get('headers',[])).get(b'authorization')!=b'Bearer test-calendar-key':
            return await JSONResponse({'error':'unauthorized'},status_code=401)(scope,receive,send)
        await manager.handle_request(scope,receive,send)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():yield
    app=Starlette(routes=[Mount('/mcp',app=secured)],lifespan=lifespan)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        result=asyncio.run(check_server({'type':'http','url':f'http://127.0.0.1:{port}/mcp/','headers':{'Authorization':'Bearer test-calendar-key'}}))
        assert result['status']=='connected',result
        assert result['tools']==['calendar_list']
    finally:server.should_exit=True;thread.join(5)
