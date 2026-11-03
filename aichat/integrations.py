"""Owner-managed MCP/search connections; secrets are encrypted separately."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from . import db, runtime
from .errors import ApiError

KEEP = "__UNCHANGED__"
DEFAULT_SEARCH = {"provider": "auto", "url": "", "api_key": ""}


def configuration(*, secrets=False):
    encrypted = db.get_kv("agent_integrations")
    value = json.loads(runtime._decrypt(encrypted)) if encrypted else {}
    value = {"mcp_servers": {}, "search": dict(DEFAULT_SEARCH), **value}
    if secrets:
        return value
    # Headers, env and args may all contain credentials. Never return them.
    servers = {}
    for name, server in value["mcp_servers"].items():
        servers[name] = {k: KEEP if k in ("headers", "env", "args") else v for k,v in server.items()}
    search = {**value["search"], "api_key": KEEP if value["search"].get("api_key") else ""}
    return {"mcp_servers": servers, "search": search}


def _string(v, maximum=4096):
    return isinstance(v,str) and len(v)<=maximum and "\x00" not in v


def validate_server(name, server):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}",name) or name=="platform":
        raise ApiError(422,"invalid_mcp_name", "MCP 名称只能使用字母、数字、下划线和短横线，platform 为保留名称")
    if not isinstance(server,dict):
        raise ApiError(422,"invalid_mcp_server")
    kind=server.get("type","stdio")
    permitted={"type","command","args","env"} if kind=="stdio" else {"type","url","headers"}
    if kind not in ("stdio","http","sse") or set(server)-permitted:
        raise ApiError(422,"invalid_mcp_transport")
    if kind=="stdio":
        if not _string(server.get("command"),1000) or not server["command"].strip():
            raise ApiError(422,"invalid_mcp_command")
        args=server.get("args",[])
        if not isinstance(args,list) or len(args)>100 or any(not _string(a) for a in args):
            raise ApiError(422,"invalid_mcp_args")
    else:
        url=server.get("url","")
        p=urlparse(url) if _string(url) else None
        if not p or p.scheme not in ("http","https") or not p.hostname or p.username or p.password or p.fragment:
            raise ApiError(422,"invalid_mcp_url")
        if p.query:
            raise ApiError(422,"invalid_mcp_url","凭证请放在 headers，不要放进 URL 查询参数")
    for field in ("env","headers"):
        values=server.get(field,{})
        if not isinstance(values,dict) or len(values)>50 or any(not _string(k,200) or not _string(v,8192) or '\n' in k or '\r' in k or (field=='headers' and ('\n' in v or '\r' in v)) for k,v in values.items()):
            raise ApiError(422,"invalid_mcp_credentials")
    return {"type":kind,**server}


def update(body, actor_id):
    old=configuration(secrets=True)
    if set(body)-{"mcp_servers","search"}:
        raise ApiError(422,"invalid_integrations")
    value={**old}
    if "mcp_servers" in body:
        servers=body["mcp_servers"]
        if not isinstance(servers,dict) or len(servers)>20:
            raise ApiError(422,"invalid_mcp_servers")
        value["mcp_servers"]={}
        for name, server in servers.items():
            if not isinstance(server,dict): raise ApiError(422,"invalid_mcp_server")
            restored={k:old["mcp_servers"].get(name,{}).get(k) if v==KEEP else v for k,v in server.items()}
            if any(v is None for v in restored.values()): raise ApiError(422,"missing_mcp_secret")
            value["mcp_servers"][name]=validate_server(name,restored)
    if "search" in body:
        search=body["search"]
        if not isinstance(search,dict) or set(search)-set(DEFAULT_SEARCH): raise ApiError(422,"invalid_search_settings")
        search={**DEFAULT_SEARCH,**old["search"],**search}
        if search["api_key"]==KEEP: search["api_key"]=old["search"].get("api_key","")
        if search["provider"] not in ("auto","bing","duckduckgo","brave","tavily","searxng") or any(not _string(search[k]) for k in ("url","api_key")):
            raise ApiError(422,"invalid_search_settings")
        if search["provider"] in ("brave","tavily") and not search["api_key"]: raise ApiError(422,"search_key_required")
        if search["url"]:
            p=urlparse(search["url"])
            if p.scheme not in ("http","https") or not p.hostname or p.username or p.password or p.query or p.fragment: raise ApiError(422,"invalid_search_url")
        if search["provider"]=="searxng" and not search["url"]: raise ApiError(422,"search_url_required")
        value["search"]=search
    if len(json.dumps(value))>131072: raise ApiError(413,"integrations_too_large")
    with db.tx():
        db.set_kv("agent_integrations",runtime._encrypt(json.dumps(value,ensure_ascii=False)))
        # Force a fresh SDK configuration; old processes cannot retain revoked tools.
        runtime.update_settings({},actor_id)
        db.audit(f"user:{actor_id}","agent.integrations",None,servers=list(value["mcp_servers"]),search=value["search"]["provider"])
    return configuration()
