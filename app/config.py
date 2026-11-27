"""環境変数から設定を読み込む。

秘密情報（LINE_CHANNEL_SECRET など）は Cloud Run が Secret Manager から
環境変数として注入する。それ以外の設定値はデプロイ時に通常の環境変数で渡す。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(f"環境変数 {name} が設定されていません")
    return value


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"環境変数 {name} は整数で指定してください: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"環境変数 {name} は {minimum} 以上で指定してください: {value}")
    return value


def _effort(name: str, default: str) -> str | None:
    raw = _get(name, default).lower()
    if raw in {"", "none", "off", "default"}:
        return None  # API の既定値に任せる
    if raw not in _EFFORTS:
        raise ConfigError(f"環境変数 {name} は {sorted(_EFFORTS)} のいずれかで指定してください: {raw!r}")
    return raw


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(value: str) -> str:
    """「owner/repo」形式に正規化する（https://github.com/owner/repo(.git) も受け付ける）。"""
    v = value.strip()
    v = re.sub(r"^(https?://)?(www\.)?github\.com/", "", v)
    v = v.removesuffix("/").removesuffix(".git")
    if not _REPO_RE.match(v):
        raise ConfigError(f"VAULT_REPO は「owner/repo」の形式で指定してください: {value!r}")
    return v


@dataclass(frozen=True)
class Settings:
    # --- Secret Manager から注入される値 ---
    line_channel_secret: str
    line_channel_access_token: str
    owner_line_user_id: str
    anthropic_api_key: str
    github_token: str
    vault_repo: str
    # --- 通常の環境変数 ---
    vault_branch: str | None
    gcp_project_id: str
    tasks_location: str
    ingest_queue: str
    ask_queue: str
    service_url: str
    tasks_invoker_sa: str
    scheduler_invoker_sa: str
    claude_model_transcribe: str
    claude_model_answer: str
    claude_effort_transcribe: str | None
    claude_effort_answer: str | None
    claude_effort_router: str | None
    claude_fallbacks: bool
    vault_dir: str
    pdf_pages_per_chunk: int
    max_attachment_bytes: int
    max_extract_chars: int
    reminder_catchup_minutes: int
    reply_deadline_seconds: int
    ask_time_budget_seconds: int
    confirm_ttl_minutes: int
    task_dispatch_deadline_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        service_url = _required("SERVICE_URL").rstrip("/")
        if not service_url.startswith("https://"):
            raise ConfigError("SERVICE_URL は https:// で始まる Cloud Run の URL を指定してください")
        fallbacks = _get("CLAUDE_FALLBACKS", "default").lower()
        if fallbacks not in {"default", "off"}:
            raise ConfigError("CLAUDE_FALLBACKS は default または off で指定してください")
        location = _get("TASKS_LOCATION") or _get("GCP_REGION", "asia-northeast1")
        return cls(
            line_channel_secret=_required("LINE_CHANNEL_SECRET"),
            line_channel_access_token=_required("LINE_CHANNEL_ACCESS_TOKEN"),
            owner_line_user_id=_required("OWNER_LINE_USER_ID"),
            anthropic_api_key=_required("ANTHROPIC_API_KEY"),
            github_token=_required("GITHUB_TOKEN"),
            vault_repo=normalize_repo(_required("VAULT_REPO")),
            vault_branch=_get("VAULT_BRANCH") or None,
            gcp_project_id=_required("GCP_PROJECT_ID"),
            tasks_location=location,
            ingest_queue=_get("INGEST_QUEUE", "line-ingest"),
            ask_queue=_get("ASK_QUEUE", "line-ask"),
            service_url=service_url,
            tasks_invoker_sa=_required("TASKS_INVOKER_SA"),
            scheduler_invoker_sa=_required("SCHEDULER_INVOKER_SA"),
            claude_model_transcribe=_required("CLAUDE_MODEL_TRANSCRIBE"),
            claude_model_answer=_required("CLAUDE_MODEL_ANSWER"),
            claude_effort_transcribe=_effort("CLAUDE_EFFORT_TRANSCRIBE", "medium"),
            claude_effort_answer=_effort("CLAUDE_EFFORT_ANSWER", "medium"),
            claude_effort_router=_effort("CLAUDE_EFFORT_ROUTER", "low"),
            claude_fallbacks=(fallbacks == "default"),
            vault_dir=_get("VAULT_DIR", "/tmp/vault"),
            pdf_pages_per_chunk=_int("PDF_PAGES_PER_CHUNK", 20, minimum=1),
            max_attachment_bytes=_int("MAX_ATTACHMENT_MB", 50, minimum=1) * 1024 * 1024,
            max_extract_chars=_int("MAX_EXTRACT_CHARS", 1_000_000, minimum=1000),
            reminder_catchup_minutes=_int("REMINDER_CATCHUP_MINUTES", 30, minimum=5),
            reply_deadline_seconds=_int("REPLY_DEADLINE_SECONDS", 50, minimum=5),
            ask_time_budget_seconds=_int("ASK_TIME_BUDGET_SECONDS", 300, minimum=30),
            confirm_ttl_minutes=_int("CONFIRM_TTL_MINUTES", 30, minimum=1),
            task_dispatch_deadline_seconds=_int("TASK_DISPATCH_DEADLINE_SECONDS", 1800, minimum=15),
        )
