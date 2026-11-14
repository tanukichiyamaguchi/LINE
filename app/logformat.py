"""日別ログ（Markdown）の1行の書式と、行の追加・置き換えを行う純粋関数群。

1行の形式：

    - HH:MM 表示名：本文 ^m<メッセージID>

* 本文の改行は <br> に置き換え、1メッセージが必ず1行に収まるようにする。
  （重複判定・送信取消・編集を「行」単位で確実に行うため）
* 編集されたメッセージは本文の末尾に「（編集済み）」を付け、編集イベントの時刻を
  Obsidian の非表示コメント %%edited:<ミリ秒>%% として ^m の直前に持つ。
  （編集イベントは届く順番が前後し得るため、より新しい編集だけを反映する）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .timeutil import fmt_date_with_weekday

ENTRY_RE = re.compile(
    r"^- (?P<time>\d{2}:\d{2}) (?P<rest>.*?)"
    r"(?: %%edited:(?P<edited>\d+)%%)?"
    r" \^m(?P<id>[0-9A-Za-z]+)[ \t]*$"
)

FAILURE_MARK = "[テキスト化失敗："
UNSENT_BODY = "（送信取消）"
EDITED_MARK = "（編集済み）"
NAME_SEPARATOR = "："

_LINE_BREAKS = re.compile(r"\r\n|\r|\n| | |\x85|\x0b|\x0c")
_ATTACH_LINK_RE = re.compile(r"\[\[(LINEログ/添付/[^\]|#]+?)(?:\|[^\]]*)?\]\]")


@dataclass(frozen=True)
class Entry:
    index: int  # ファイル内の行番号（0始まり）
    time: str  # "HH:MM"
    rest: str  # "表示名：本文"
    msg_id: str
    edited_ts: int | None

    @property
    def is_failure(self) -> bool:
        return FAILURE_MARK in self.rest

    @property
    def is_unsent(self) -> bool:
        return self.rest.endswith(NAME_SEPARATOR + UNSENT_BODY)


def one_line(text: str | None) -> str:
    """本文を1行にする（改行は <br>、タブは空白）。"""
    s = _LINE_BREAKS.sub("<br>", text or "")
    s = s.replace("\t", " ")
    # Obsidian のコメント記号 %% が本文に含まれると、以降が非表示になったり
    # ブロックID（^m…）が認識されなくなるため、2文字目を全角にして無害化する
    s = s.replace("%%", "%％")
    return s.strip()


def format_entry(time: str, name: str, body: str, msg_id: str, edited_ts: int | None = None) -> str:
    display = one_line(name).replace("<br>", " ") or "名前不明"
    line = f"- {time} {display}{NAME_SEPARATOR}{one_line(body)}"
    if edited_ts is not None:
        line += f" %%edited:{int(edited_ts)}%%"
    return f"{line} ^m{msg_id}"


def parse_entry(line: str, index: int = 0) -> Entry | None:
    m = ENTRY_RE.match(line)
    if not m:
        return None
    edited = m.group("edited")
    return Entry(
        index=index,
        time=m.group("time"),
        rest=m.group("rest"),
        msg_id=m.group("id"),
        edited_ts=int(edited) if edited else None,
    )


def parse_entries(lines: list[str]) -> list[Entry]:
    out: list[Entry] = []
    for i, line in enumerate(lines):
        e = parse_entry(line, i)
        if e is not None:
            out.append(e)
    return out


def find_entry(text: str | None, msg_id: str) -> Entry | None:
    if not text:
        return None
    for e in parse_entries(_split(text)):
        if e.msg_id == msg_id:
            return e
    return None


def daily_header(title: str, d: date) -> str:
    return f"# {fmt_date_with_weekday(d)} {one_line(title)}\n\n"


def split_name_body(rest: str, known_name: str | None = None) -> tuple[str, str]:
    """「表示名：本文」を表示名と本文に分ける。表示名に「：」が含まれる場合に備え、
    分かっている表示名があればそれを優先する。"""
    if known_name and rest.startswith(known_name + NAME_SEPARATOR):
        return known_name, rest[len(known_name) + len(NAME_SEPARATOR):]
    idx = rest.find(NAME_SEPARATOR)
    if idx >= 0:
        return rest[:idx], rest[idx + len(NAME_SEPARATOR):]
    return rest, ""


def attachment_links(rest: str) -> list[str]:
    """行に含まれる添付ノートへのリンク（Vault 内パス）を返す。"""
    paths = []
    for m in _ATTACH_LINK_RE.finditer(rest):
        p = m.group(1).strip()
        if not p.endswith(".md"):
            p += ".md"
        paths.append(p)
    return paths


def _split(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _join(lines: list[str]) -> str:
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


def _sort_key(time: str, msg_id: str) -> tuple[str, int, str]:
    return (time, int(msg_id) if msg_id.isdigit() else -1, msg_id)


def upsert_entry(
    text: str | None, *, header: str, line: str, time: str, msg_id: str
) -> tuple[str | None, str]:
    """日別ログに1行を書き込む。

    戻り値の status:
      "inserted"          新しく追加した
      "replaced_failure"  「テキスト化失敗」の行を置き換えた（再試行が成功した）
      "duplicate"         同じメッセージIDの行が既にあるので何もしない（new_text は None）
    """
    if text is None or not text.strip():
        return header + line + "\n", "inserted"
    lines = _split(text)
    entries = parse_entries(lines)
    for e in entries:
        if e.msg_id == msg_id:
            if e.is_failure:
                lines[e.index] = line
                return _join(lines), "replaced_failure"
            return None, "duplicate"
    key = _sort_key(time, msg_id)
    earlier = [e for e in entries if _sort_key(e.time, e.msg_id) <= key]
    if earlier:
        insert_at = earlier[-1].index + 1
    elif entries:
        insert_at = entries[0].index
    else:
        insert_at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
    lines.insert(insert_at, line)
    return _join(lines), "inserted"


def mark_unsent(
    text: str | None, msg_id: str, known_name: str | None = None
) -> tuple[str | None, list[str], str]:
    """送信取消：該当行を「（送信取消）」に置き換える。

    戻り値: (new_text, 削除すべき添付ノートのパス, status)
      status: "unsent" | "already" | "not_found"
    """
    if not text:
        return None, [], "not_found"
    lines = _split(text)
    for e in parse_entries(lines):
        if e.msg_id != msg_id:
            continue
        if e.is_unsent:
            return None, [], "already"
        name, _body = split_name_body(e.rest, known_name)
        attachments = attachment_links(e.rest)
        lines[e.index] = format_entry(e.time, name, UNSENT_BODY, msg_id)
        return _join(lines), attachments, "unsent"
    return None, [], "not_found"


def apply_edit(
    text: str | None, msg_id: str, new_body: str, edited_ts: int, known_name: str | None = None
) -> tuple[str | None, str]:
    """編集イベント：該当行の本文を書き換える（より新しい編集のときだけ）。

    status: "edited" | "stale"（既により新しい編集を反映済み） | "unsent" | "not_found"
    """
    if not text:
        return None, "not_found"
    lines = _split(text)
    for e in parse_entries(lines):
        if e.msg_id != msg_id:
            continue
        if e.is_unsent:
            return None, "unsent"
        if e.edited_ts is not None and e.edited_ts >= edited_ts:
            return None, "stale"
        name, _body = split_name_body(e.rest, known_name)
        lines[e.index] = format_entry(e.time, name, new_body + EDITED_MARK, msg_id, edited_ts)
        return _join(lines), "edited"
    return None, "not_found"
