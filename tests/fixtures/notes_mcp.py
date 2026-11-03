"""A real stdio MCP notes service used only for integration tests."""
import asyncio
import json
import os
from pathlib import Path
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server


async def tools(ctx,params):
    return types.ListToolsResult(tools=[
        types.Tool(name='append_note',description='Persist a note in the external notes service',input_schema={'type':'object','properties':{'text':{'type':'string'}},'required':['text']}),
        types.Tool(name='list_notes',description='Read notes stored in the external notes service',input_schema={'type':'object','properties':{}}),
    ])


async def call(ctx,params):
    path=Path(os.environ['NOTES_PATH'])
    notes=json.loads(path.read_text()) if path.exists() else []
    if params.name=='append_note':
        notes.append(params.arguments['text']);path.write_text(json.dumps(notes))
    return types.CallToolResult(content=[types.TextContent(type='text',text=json.dumps(notes))])


async def main():
    server=Server('test-notes',on_list_tools=tools,on_call_tool=call)
    async with stdio_server() as (r,w):
        await server.run(r,w,server.create_initialization_options())


if __name__=='__main__':asyncio.run(main())
