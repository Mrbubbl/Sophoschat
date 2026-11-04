"""Probe MCP in a disposable, cancellable process without model inference."""
from __future__ import annotations
import asyncio
import json
import os
import sys


async def check_server(server):
    from .service import terminate_tree,ROOT
    env={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"PYTHONPATH":str(ROOT)}
    proc=await asyncio.create_subprocess_exec(sys.executable,'-m','aichat_agent.mcpcheck',env=env,start_new_session=True,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        out,_=await asyncio.wait_for(proc.communicate(json.dumps({'server':server,'parent':os.getpid()}).encode()),25)
        return json.loads(out) if proc.returncode==0 else {'status':'failed','error':'mcp_connection_failed'}
    except asyncio.TimeoutError:return {'status':'failed','error':'mcp_connection_timeout'}
    except (ValueError,OSError):return {'status':'failed','error':'mcp_connection_failed'}
    finally:await terminate_tree(proc)


async def probe(server):
    import contextlib
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp import types
    import httpx2
    async with contextlib.AsyncExitStack() as stack:
        kind=server.get('type','stdio')
        if kind=='stdio':
            transport=stdio_client(StdioServerParameters(command=server['command'],args=server.get('args',[]),env={**os.environ,**server.get('env',{})}))
        elif kind=='sse':transport=sse_client(server['url'],headers=server.get('headers',{}),timeout=15)
        else:
            client=await stack.enter_async_context(httpx2.AsyncClient(headers=server.get('headers',{}),timeout=15,trust_env=False,follow_redirects=False))
            transport=streamable_http_client(server['url'],http_client=client)
        streams=await stack.enter_async_context(transport)
        session=await stack.enter_async_context(ClientSession(streams[0],streams[1],read_timeout_seconds=15))
        await session.initialize()
        names=[];cursor=None
        for _ in range(10):
            result=await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            names.extend(t.name for t in result.tools)
            cursor=result.next_cursor
            if not cursor:break
        return {'status':'connected','tools':names[:500],'tool_count':len(names)}


if __name__=='__main__':
    from .watchdog import watch_parent
    data=json.loads(sys.stdin.read());watch_parent(data['parent'])
    async def bounded_probe():
        return await asyncio.wait_for(probe(data['server']),20)
    try:result=asyncio.run(bounded_probe())
    except BaseException:result={'status':'failed','error':'mcp_connection_failed'}
    print(json.dumps(result),flush=True)
