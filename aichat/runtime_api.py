from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Body

from . import artifacts, auth, chat, db, runtime, tasks, integrations
from .agent_api import agent_cred
from .api import current_user, owner_user, uid
from .errors import ApiError

router = APIRouter()


@router.get("/api/agent/integrations")
async def get_integrations(u=Depends(owner_user)):
    import shutil,sys,os
    return {"integrations":integrations.configuration(),"execution":{"permission_mode":"bypassPermissions","builtin_tools":"all","os_uid":os.getuid() if hasattr(os,'getuid') else None,"python":sys.executable,"node":shutil.which('node'),"npm":shutil.which('npm')},"registration_enabled":db.get_kv('registration_enabled')!='0'}


@router.put("/api/agent/integrations")
async def set_integrations(body:dict=Body(...),u=Depends(owner_user)):
    return {"integrations":integrations.update(body,uid(u))}


@router.post("/api/agent/integrations/check")
async def check_integration(body:dict=Body(...),u=Depends(owner_user)):
    from aichat_agent.mcpcheck import check_server
    name=body.get('name')
    server=integrations.configuration(secrets=True)['mcp_servers'].get(name)
    if not server: raise ApiError(404,'mcp_server_not_found')
    result=await check_server(server)
    db.audit(f'user:{uid(u)}','agent.mcp_check',None,name=name,status=result['status'])
    return result


@router.put("/api/registration")
async def set_registration(body:dict=Body(...),u=Depends(owner_user)):
    if set(body)!={'enabled'} or type(body['enabled']) is not bool:raise ApiError(422,'invalid_registration_settings')
    db.set_kv('registration_enabled','1' if body['enabled'] else '0')
    db.audit(f'user:{uid(u)}','registration.settings',None,enabled=body['enabled'])
    return {'enabled':body['enabled']}


@router.get("/api/agent/settings")
async def settings(u=Depends(owner_user)):
    return {"settings": runtime.settings(), "sdk": runtime.installed_sdk()}


@router.put("/api/agent/settings")
async def save_settings(body: dict = Body(...), u=Depends(owner_user)):
    return {"settings": runtime.update_settings(body, uid(u))}


@router.get("/api/agent/usage")
async def usage(u=Depends(current_user)):
    return runtime.usage_for(uid(u))


@router.get("/api/agent/runtime")
async def control_config(found=Depends(agent_cred)):
    runtime.require_service(found[1])
    # Configuration keys are readable only by the platform controller, never
    # exposed as an SDK tool or sent to a browser/model prompt.
    return {"settings": runtime.settings(), "sdk": runtime.installed_sdk()}


def attempt(task_id: int, request: Request, found):
    runtime.require_service(found[1])
    return tasks._owned(task_id, found[1], request.headers.get("x-task-token", ""))


@router.get("/api/agent/tasks/{task_id}/runtime")
async def task_config(task_id: int, request: Request, found=Depends(agent_cred)):
    t = attempt(task_id, request, found)
    return runtime.attempt_config(t)


@router.post("/api/agent/tasks/{task_id}/result")
async def report_result(task_id: int, request: Request, body: dict = Body(...), found=Depends(agent_cred)):
    t = attempt(task_id, request, found)
    runtime.record_result(t, body)
    # Once chat delegates a file task, its acknowledgement is platform-owned.
    # The chat model cannot truthfully assert browser checks or file completion
    # that happen independently in the delivery worker.
    child = db.one("SELECT id FROM agent_tasks WHERE parent_task_id=? AND profile='delivery' ORDER BY id LIMIT 1", (task_id,)) if t["profile"] == "chat" else None
    return {"ok": True, "reply_override": "已接收文件制作请求。请在本对话查看任务进度和交付文件卡片。" if child else None}


@router.post("/api/agent/tasks/{task_id}/session-reset")
async def reset_session(task_id: int, request: Request, found=Depends(agent_cred)):
    t = attempt(task_id, request, found)
    with db.tx():
        db.run("UPDATE sdk_sessions SET sdk_session_id=NULL,total_cost=NULL,usage_json=NULL,updated_at=? WHERE session_key=?", (db.now(),t["session_key"]))
        db.audit("platform-sdk-service", "agent.session_reset", t["room_id"], task_id=task_id, reason="transcript_missing")
    return {"ok": True}


@router.post("/api/agent/tasks/{task_id}/progress")
async def progress(task_id: int, request: Request, body: dict = Body(...), found=Depends(agent_cred)):
    t = attempt(task_id, request, found)
    labels = {"reading": "读取上下文", "generating": "正在制作", "checking": "检查文件", "publishing": "保存交付物", "replying": "正在回复", "memory": "整理记忆", "recovering": "核查已有交付", "execute":"执行代码与命令", "browsing":"浏览网页", "searching":"搜索互联网", "external":"调用外部应用", "files":"读写项目文件"}
    if body.get("stage") not in labels:
        raise ApiError(422, "invalid_progress")
    db.run("UPDATE agent_tasks SET progress=? WHERE id=?", (labels[body["stage"]], t["id"]))
    tasks._publish_task(t["id"])
    return {"ok": True}


@router.post("/api/agent/tasks/{task_id}/tool-event")
async def tool_event(task_id:int,request:Request,body:dict=Body(...),found=Depends(agent_cred)):
    import re
    t=attempt(task_id,request,found)
    name=body.get('name','');object_id=body.get('id','')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,180}',name) or not isinstance(object_id,str) or len(object_id)>160:
        raise ApiError(422,'invalid_tool_event')
    runtime.receipt(t,'tool.invoked.'+name,object_id)
    return {'ok':True}


def start_selfcheck(user_id: int) -> dict:
    s = runtime.settings()
    sdk = runtime.installed_sdk()
    st = tasks.agent_status()
    checks = [
        {"name": "模型与服务端密钥已配置", "ok": s["configured"]},
        {"name": "服务端 AI 已启用", "ok": s["enabled"]},
        {"name": "固定版本 Claude Agent SDK 已安装", "ok": sdk["installed"] and sdk["version"] == runtime.SDK_VERSION},
        {"name": "SDK 执行服务心跳在线", "ok": st.get("adapter") == "claude-sdk" and st["state"] not in ("offline", "unconfigured")},
    ]
    if not all(c["ok"] for c in checks):
        return {"ok": False, "state": "blocked", "checks": checks}
    rid = chat.ensure_agent_dm(user_id)
    pending = db.one("SELECT * FROM agent_tasks WHERE room_id=? AND params LIKE '%\"selfcheck\":true%' AND status IN ('queued','leased')", (rid,))
    if pending:
        return selfcheck_status(pending["id"], user_id)
    with db.tx():
        msg, _ = chat.send_message(rid, user_id, "【接入自检】请用真实 SDK 读取本任务上下文，交付 selfcheck.md，并读回验证文件。", f"selfcheck-{db.now()}", is_test=True)
        tid = db.insert("INSERT INTO agent_tasks(room_id,kind,profile,trigger_message_id,trigger_message_version,initiator_id,status,created_at,expires_at,params) VALUES(?,'chat','delivery',?,?,?,'queued',?,?,?)", (rid,msg["id"],msg["version"],user_id,db.now(),db.after(600),db.jl({"selfcheck":True})))
        tasks._wake()
        tasks._publish_task(tid)
    return {"ok": False, "state": "pending", "checks": checks, "task_id": tid, "room_id": rid, "message_id": msg["id"]}


def selfcheck_status(task_id: int, user_id: int) -> dict:
    t = db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not t or not db.jd(t["params"], {}).get("selfcheck"):
        raise ApiError(404, "selfcheck_not_found")
    chat.require_member(t["room_id"], user_id)
    tasks.require_controller(t, user_id)
    receipts = {r["action"] for r in db.all("SELECT action FROM task_tool_receipts WHERE task_id=?", (task_id,))}
    checks = [{"name": "真实 SDK 调用成功", "ok": "sdk.result" in receipts}, {"name": "作用域工具已读取上下文", "ok": "messages.read" in receipts}, {"name": "文件已持久保存与校验", "ok": "artifact.verified" in receipts}, {"name": "测试文件已读回", "ok": "artifact.read" in receipts}]
    ok = t["status"] == "done" and all(c["ok"] for c in checks)
    return {"ok": ok, "state": "passed" if ok else "failed" if t["status"] in ("failed", "cancelled", "expired", "done") else "pending", "checks": checks, "task_id": task_id, "room_id": t["room_id"], "error": t["error"], "versions": artifacts.task_versions(t)}


@router.get("/api/agent/selfcheck/{task_id}")
async def get_selfcheck(task_id: int, u=Depends(owner_user)):
    return selfcheck_status(task_id, uid(u))
