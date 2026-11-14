"""読み込みに時間のかかる部品（Claude・LINE API クライアント・文書処理など）の組み立て。

LINE の Webhook は2秒以内に応答する必要があるため、Webhook の処理に要らない
これらの部品は、起動後に裏で読み込む（app.main の Services.get_workers）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .claude import ClaudeService
from .config import Settings
from .cron import ReminderRunner
from .github_store import GitHubStore
from .groups import GroupRegistry
from .ingest import Ingestor
from .line_api import LineClient
from .owner import OwnerAssistant
from .tasks_client import TasksClient
from .transcribe import Transcriber
from .vault import VaultMirror


@dataclass
class Workers:
    line: Any
    ingestor: Any
    owner: Any
    reminders: Any
    vault: Any = None
    closers: list = field(default_factory=list)


def build_workers(settings: Settings, tasks: TasksClient) -> Workers:
    line = LineClient(settings.line_channel_access_token)
    store = GitHubStore(settings.github_token, settings.vault_repo, settings.vault_branch)
    claude = ClaudeService(settings.anthropic_api_key, use_fallbacks=settings.claude_fallbacks)
    groups = GroupRegistry(store, line)
    vault = VaultMirror(settings.vault_repo, settings.github_token, settings.vault_dir, settings.vault_branch)
    transcriber = Transcriber(claude, settings.claude_model_transcribe, settings.claude_effort_transcribe,
                              settings.pdf_pages_per_chunk)
    return Workers(
        line=line,
        ingestor=Ingestor(settings, store, line, groups, transcriber),
        owner=OwnerAssistant(settings, store, line, groups, claude, vault, tasks),
        reminders=ReminderRunner(settings, store, line, groups, tasks),
        vault=vault,
        closers=[line.aclose, store.aclose, claude.aclose],
    )
