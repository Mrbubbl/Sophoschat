"""运行配置。所有项都可以用环境变量覆盖，默认值适合单机自部署的双人使用。"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    data_dir: Path
    secret: bytes = b""
    # 聊天互动任务的有效期：超过后标记“未及时处理”，不再补发过时回答
    chat_task_ttl: int = 600
    # 任务租约（Bridge 需要在此时间内续租，否则任务回到队列）
    lease_seconds: int = 120
    # 后台整理：讨论静默多久后开始整理 / 最长等待 / 每批最多处理多少事件
    consolidate_quiet: int = 300
    consolidate_max_wait: int = 3600
    consolidate_batch_events: int = 200
    # Bridge 超过该时间没有心跳，界面显示“离线”
    agent_offline_after: int = 90
    scheduler_tick: int = 10
    session_days: int = 30
    max_upload_mb: int = 25
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    allow_legacy_agent: bool = False
    start_agent_service: bool = True
    agent_base_url: str = "http://127.0.0.1:8000"
    asr_enabled: bool = True
    asr_auto_agent: bool = True
    asr_model_dir: str = ""
    asr_ffmpeg: str = "ffmpeg"
    asr_threads: int = 2
    asr_timeout: int = 180
    asr_max_seconds: int = 300

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aichat.db"

    @property
    def attach_dir(self) -> Path:
        return self.data_dir / "attachments"


cfg: Config | None = None


def load(data_dir: str | os.PathLike | None = None, **overrides) -> Config:
    """加载配置并确保数据目录与签名密钥存在。"""
    global cfg
    d = Path(data_dir or os.environ.get("AICHAT_DATA_DIR", "./data")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    (d / "attachments").mkdir(exist_ok=True)
    key_file = d / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    c = Config(
        data_dir=d,
        secret=bytes.fromhex(key_file.read_text().strip()),
        chat_task_ttl=_int("AICHAT_CHAT_TASK_TTL", 600),
        lease_seconds=_int("AICHAT_LEASE_SECONDS", 120),
        consolidate_quiet=_int("AICHAT_CONSOLIDATE_QUIET", 300),
        consolidate_max_wait=_int("AICHAT_CONSOLIDATE_MAX_WAIT", 3600),
        consolidate_batch_events=_int("AICHAT_CONSOLIDATE_BATCH", 200),
        agent_offline_after=_int("AICHAT_AGENT_OFFLINE_AFTER", 90),
        scheduler_tick=_int("AICHAT_SCHEDULER_TICK", 10),
        session_days=_int("AICHAT_SESSION_DAYS", 30),
        max_upload_mb=_int("AICHAT_MAX_UPLOAD_MB", 25),
        cookie_secure=os.environ.get("AICHAT_COOKIE_SECURE", "0") == "1",
        allowed_origins=tuple(
            o.strip() for o in os.environ.get("AICHAT_ALLOWED_ORIGINS", "").split(",") if o.strip()
        ),
        allow_legacy_agent=os.environ.get("AICHAT_ALLOW_LEGACY_AGENT") == "1",
        start_agent_service=os.environ.get("AICHAT_START_AGENT_SERVICE", "1") == "1",
        agent_base_url=os.environ.get("AICHAT_AGENT_BASE_URL", "http://127.0.0.1:8000"),
        asr_enabled=os.environ.get("AICHAT_ASR_ENABLED", "1") == "1",
        asr_auto_agent=os.environ.get("AICHAT_ASR_AUTO_AGENT", "1") == "1",
        asr_model_dir=os.environ.get("AICHAT_ASR_MODEL_DIR", str(d / "models" / "sensevoice-small-int8")),
        asr_ffmpeg=os.environ.get("AICHAT_ASR_FFMPEG", "ffmpeg"),
        asr_threads=max(1, min(8, _int("AICHAT_ASR_THREADS", 2))),
        asr_timeout=max(10, min(600, _int("AICHAT_ASR_TIMEOUT", 180))),
        asr_max_seconds=max(1, min(900, _int("AICHAT_ASR_MAX_SECONDS", 300))),
    )
    for k, v in overrides.items():
        setattr(c, k, v)
    cfg = c
    return c


def get() -> Config:
    if cfg is None:
        load()
    assert cfg is not None
    return cfg
