"""命令行：aichat init / user / agent / serve / export / backup / restore / doctor / status"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from . import auth, backup, chat, config, db, export, tasks
from .errors import ApiError


def _open(args) -> None:
    config.load(args.data_dir)
    db.connect(config.get().db_path)


def _password(args, prompt="密码：") -> str:
    if getattr(args, "password", None):
        return args.password
    env = os.environ.get("AICHAT_PASSWORD")
    if env:
        return env
    p1 = getpass.getpass(prompt)
    if getpass.getpass("再输入一次：") != p1:
        raise SystemExit("两次输入不一致")
    return p1


def cmd_init(args):
    _open(args)
    if db.one("SELECT 1 FROM users WHERE role='owner'"):
        raise SystemExit("已经初始化过（Owner 已存在）。新增用户请用：aichat user add")
    uid = auth.create_user(args.username, args.display or args.username, _password(args), "owner")
    auth.ensure_agent_user()
    chat.bootstrap_user(uid)
    print(f"已创建 Owner：{args.username}")
    print("下一步：")
    print("  1) aichat user add <用户名>        # 添加第二位使用者")
    print("  2) aichat serve                    # 启动服务，浏览器打开 http://127.0.0.1:8000")
    print("  3) 登录后在 设置 → Agent 接入 中创建凭证并启动 Bridge")


def cmd_user(args):
    _open(args)
    if args.action == "add":
        uid = auth.create_user(args.username, args.display or args.username, _password(args), "user")
        chat.bootstrap_user(uid)
        print(f"已创建用户 {args.username}，并自动建立了与 Agent 的私聊以及与其他成员的私聊")
    elif args.action == "list":
        for r in db.all("SELECT * FROM users ORDER BY id"):
            print(f"{r['id']:>3}  {r['username']:<20} {r['role']:<6} {'已停用' if r['disabled'] else ''}")
    else:
        u = db.one("SELECT * FROM users WHERE username=?", (args.username,))
        if not u:
            raise SystemExit("用户不存在")
        if args.action == "passwd":
            auth.set_password(u["id"], _password(args, "新密码："))
            print("密码已更新，所有已登录设备已下线")
        elif args.action in ("disable", "enable"):
            auth.set_disabled(u["id"], args.action == "disable")
            print("已停用" if args.action == "disable" else "已启用")


def cmd_agent(args):
    _open(args)
    if args.action == "token":
        cid, token = auth.issue_credential(args.label or "cli")
        print(f"凭证 ID：{cid}\n令牌（只显示这一次，请立即保存）：\n{token}")
    elif args.action == "list":
        for c in auth.list_credentials():
            print(f"{c['id']:>3}  {c['label'] or '-':<20} {c['created_at']}  {'已撤销 ' + c['revoked_at'] if c['revoked_at'] else '有效'}")
    elif args.action == "revoke":
        print("已撤销" if auth.revoke_credential(args.id) else "凭证不存在或已撤销")


def cmd_serve(args):
    import uvicorn

    from .main import create_app

    if args.data_dir:
        os.environ["AICHAT_DATA_DIR"] = args.data_dir
    app = create_app(args.data_dir)
    print(f"数据目录：{config.get().data_dir}")
    print("提示：本服务使用进程内实时推送，请只运行一个 worker；公网访问请放在 HTTPS 反向代理之后，并设置 AICHAT_COOKIE_SECURE=1")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")


def cmd_export(args):
    _open(args)
    u = db.one("SELECT * FROM users WHERE username=?", (args.user,))
    if not u:
        raise SystemExit("用户不存在")
    data, name, _ = export.export_room(args.room, u["id"], args.format, args.from_time, args.to_time, args.memory, args.attachments)
    out = args.out or name
    with open(out, "wb") as f:
        f.write(data)
    print(f"已导出到 {out}")


def cmd_backup(args):
    _open(args)
    m = backup.create_backup(args.out)
    print(f"备份完成：{args.out}  {json.dumps(m['counts'])}")
    print("注意：备份包含签名密钥与凭证哈希，请妥善保管。")


def cmd_restore(args):
    target = args.data_dir or os.environ.get("AICHAT_DATA_DIR") or "./data"
    issues = backup.restore_backup(args.archive, target, args.force)
    print("恢复完成。一致性检查：" + ("通过" if not issues else ""))
    for i in issues:
        print(f"  [{i['level']}] {i['msg']}")


def cmd_doctor(args):
    _open(args)
    issues = backup.doctor()
    print("一致性检查：" + ("通过" if not issues else f"{len(issues)} 项提示"))
    for i in issues:
        print(f"  [{i['level']}] {i['msg']}")
    sys.exit(1 if any(i["level"] == "error" for i in issues) else 0)


def cmd_status(args):
    _open(args)
    print(json.dumps(tasks.agent_status(), ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="aichat", description="SophosChat 管理命令")
    p.add_argument("--data-dir", default=None, help="数据目录（默认 $AICHAT_DATA_DIR 或 ./data）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="创建 Owner 并初始化")
    s.add_argument("--username", required=True)
    s.add_argument("--display")
    s.add_argument("--password")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("user", help="用户管理")
    s.add_argument("action", choices=["add", "list", "passwd", "disable", "enable"])
    s.add_argument("username", nargs="?")
    s.add_argument("--display")
    s.add_argument("--password")
    s.set_defaults(fn=cmd_user)

    s = sub.add_parser("agent", help="Agent 凭证")
    s.add_argument("action", choices=["token", "list", "revoke"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--label")
    s.set_defaults(fn=cmd_agent)

    s = sub.add_parser("serve", help="启动服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("export", help="导出会话")
    s.add_argument("--room", type=int, required=True)
    s.add_argument("--user", required=True, help="以哪位用户的可见范围导出")
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("--from-time")
    s.add_argument("--to-time")
    s.add_argument("--memory", action="store_true")
    s.add_argument("--attachments", action="store_true")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("backup", help="完整备份（数据库+附件+密钥）")
    s.add_argument("out")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="从备份恢复到数据目录")
    s.add_argument("archive")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("doctor", help="一致性检查")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("status", help="Agent 状态")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    if args.cmd == "user" and args.action in ("add", "passwd", "disable", "enable") and not args.username:
        p.error("需要用户名")
    if args.cmd == "agent" and args.action == "revoke" and args.id is None:
        p.error("需要凭证 ID")
    try:
        args.fn(args)
    except ApiError as e:
        raise SystemExit(f"错误：{e.message}")


if __name__ == "__main__":
    main()
