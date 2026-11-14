"""ボットの内部状態（送信待ちのお知らせ・オーナーへの通知待ち）を Vault に保存する。

Cloud Run のインスタンスは入れ替わるため、状態はメモリではなく
ボット設定/ のファイル（JSON を埋め込んだ Markdown）に置く。
"""

from __future__ import annotations

import json
import re
from typing import Any

PENDING_TITLE = "送信待ち"
PENDING_NOTE = "オーナーの「はい」を待っているお知らせです。ボットが自動で管理するため、編集は不要です。"
NOTICES_TITLE = "通知待ち"
NOTICES_NOTE = "次にオーナーが1対1で話しかけたときに、応答メッセージで知らせる内容です。ボットが自動で管理するため、編集は不要です。"

_BLOCK_RE = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL)


def load_state(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    m = _BLOCK_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group("body"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def dump_state(title: str, note: str, data: dict[str, Any]) -> str:
    body = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    return f"# {title}\n\n{note}\n\n```json\n{body}\n```\n"


# ---------------------------------------------------------------- 通知待ち
def add_notice(text: str | None, key: str, message: str, created: str) -> str | None:
    """通知を追加する（同じ key が既にあれば何もしない）。"""
    data = load_state(text)
    notices = [n for n in data.get("notices", []) if isinstance(n, dict)]
    if any(n.get("key") == key for n in notices):
        return None
    notices.append({"key": key, "text": message, "created": created})
    return dump_state(NOTICES_TITLE, NOTICES_NOTE, {"notices": notices})


def list_notices(text: str | None) -> list[dict[str, str]]:
    return [n for n in load_state(text).get("notices", []) if isinstance(n, dict) and n.get("text")]


def remove_notices(text: str | None, keys: set[str]) -> str | None:
    data = load_state(text)
    notices = [n for n in data.get("notices", []) if isinstance(n, dict)]
    remaining = [n for n in notices if n.get("key") not in keys]
    if len(remaining) == len(notices):
        return None
    return dump_state(NOTICES_TITLE, NOTICES_NOTE, {"notices": remaining})


# ---------------------------------------------------------------- 送信待ち
def get_pending(text: str | None) -> dict[str, Any] | None:
    pending = load_state(text).get("pending")
    return pending if isinstance(pending, dict) and pending.get("id") else None


def set_pending(pending: dict[str, Any] | None) -> str:
    return dump_state(PENDING_TITLE, PENDING_NOTE, {"pending": pending} if pending else {})


def clear_pending_if(text: str | None, pending_id: str) -> str | None:
    """送信待ちが pending_id のものであれば消す（別のものに置き換わっていたら None）。"""
    current = get_pending(text)
    if not current or current.get("id") != pending_id:
        return None
    return set_pending(None)
