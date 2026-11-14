"""Vault 内のパスの組み立てと、書き込み可能範囲の判定。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LOG_ROOT = "LINEログ"
SETTINGS_ROOT = "ボット設定"
INDIVIDUAL_DIR = f"{LOG_ROOT}/個別"
ATTACH_DIR = f"{LOG_ROOT}/添付"

GROUPS_FILE = f"{SETTINGS_ROOT}/グループ一覧.md"
REMINDERS_FILE = f"{SETTINGS_ROOT}/リマインド.md"
PENDING_SEND_FILE = f"{SETTINGS_ROOT}/送信待ち.md"
NOTICES_FILE = f"{SETTINGS_ROOT}/通知待ち.md"

# ボットが書き換えてよいのはこの2つのフォルダの中だけ
WRITABLE_PREFIXES = (f"{LOG_ROOT}/", f"{SETTINGS_ROOT}/")

# 「個別」「添付」はシステム用のフォルダ名なので、同名のグループは別名にする
RESERVED_GROUP_FOLDERS = {"個別", "添付"}

# ファイル名に使えない（または Obsidian のリンクを壊す）文字
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")


def truncate_utf8(text: str, max_bytes: int) -> str:
    """UTF-8 のバイト数が max_bytes 以下になるように末尾を切り詰める。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_component(name: str | None, fallback: str, max_bytes: int = 150) -> str:
    """フォルダ名・ファイル名の1要素として安全な文字列にする。"""
    s = unicodedata.normalize("NFC", name or "")
    s = _FORBIDDEN.sub("_", s)
    s = _SPACES.sub(" ", s).strip()
    s = truncate_utf8(s, max_bytes)
    s = s.strip(" .")  # 先頭・末尾のドットや空白は不可
    return s or fallback


def group_folder_name(registered_name: str) -> str:
    name = sanitize_component(registered_name, "名称未設定グループ")
    if name in RESERVED_GROUP_FOLDERS:
        name = f"{name}（グループ）"
    return name


def user_folder_name(display_name: str) -> str:
    return sanitize_component(display_name, "名前不明")


@dataclass(frozen=True)
class ChatRef:
    """ログの保存先を決めるためのトーク情報。"""

    kind: str  # "group" | "user" | "room"
    chat_id: str  # groupId / userId / roomId
    title: str  # フォルダ名の元になる名前（グループ名・表示名など）

    def log_dir(self) -> str:
        if self.kind == "user":
            return f"{INDIVIDUAL_DIR}/{user_folder_name(self.title)}"
        return f"{LOG_ROOT}/{group_folder_name(self.title)}"

    def daily_log_path(self, date_str: str) -> str:
        return f"{self.log_dir()}/{date_str}.md"


def attachment_note_path(date_str: str, hhmm: str, filename: str, suffix: str = "") -> str:
    """添付の全文ノートのパス：LINEログ/添付/YYYY-MM-DD_HHMM_<ファイル名>.md"""
    base = sanitize_component(filename, "添付", max_bytes=180)
    if suffix:
        base = f"{base}_{suffix}"
    return f"{ATTACH_DIR}/{date_str}_{hhmm.replace(':', '')}_{base}.md"


def is_writable_path(path: str) -> bool:
    """ボットが書き換えてよいパスかどうか（LINEログ/ と ボット設定/ の中だけ）。"""
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        return False
    return path.startswith(WRITABLE_PREFIXES)
