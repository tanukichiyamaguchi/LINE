"""記録処理（/tasks/ingest）：LINE のイベントを日別ログに書き込む。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Settings
from .errors import RetryLater
from .extract import KIND_LABEL, decode_text, extract_office, file_kind
from .github_store import GitHubStore, RemoteFile
from .groups import GroupRegistry
from .line_api import LineClient
from .logformat import (
    EDITED_MARK,
    FAILURE_MARK,
    UNSENT_BODY,
    apply_edit,
    attachment_links,
    daily_header,
    find_entry,
    format_entry,
    mark_unsent,
    upsert_entry,
)
from .paths import ChatRef, attachment_note_path
from .timeutil import fmt_date, fmt_hhmm, from_epoch_ms
from .transcribe import ImageText, Transcriber, classify_failure

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 2  # 送信取消・編集で、元のメッセージを探しにいく日数（当日を含めず）
MARKERS = {"video": "[動画]", "audio": "[音声]", "sticker": "[スタンプ]", "location": "[位置情報]"}


@dataclass(frozen=True)
class Failure:
    reason: str
    permanent: bool


# ==================================================================== 添付ノート
_FM_LINE = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$")


def note_frontmatter(text: str | None) -> dict:
    """添付ノート先頭の frontmatter（値は JSON 形式で書いている）を読む。"""
    if not text or not text.startswith("---"):
        return {}
    out: dict = {}
    for line in text.split("\n")[1:]:
        if line.strip() == "---":
            break
        m = _FM_LINE.match(line)
        if not m:
            continue
        try:
            out[m.group("key")] = json.loads(m.group("value"))
        except json.JSONDecodeError:
            out[m.group("key")] = m.group("value").strip().strip('"')
    return out


def note_message_id(text: str | None) -> str | None:
    value = note_frontmatter(text).get("message_id")
    return str(value) if value else None


def render_attachment_note(
    *, kind_label: str, filename: str, chat: ChatRef, sender: str, dt: datetime, message_id: str,
    log_path: str, summary: list[str], full_text: str = "", image: ImageText | None = None,
    truncated: bool = False,
) -> str:
    date_str, hhmm = fmt_date(dt), fmt_hhmm(dt)
    frontmatter = {
        "type": "LINE添付",
        "kind": kind_label,
        "file_name": filename,
        "chat": chat.title,
        "sender": sender,
        "date": date_str,
        "time": hhmm,
        "message_id": message_id,
        "summary": summary,
    }
    lines = ["---"]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in frontmatter.items()]
    lines += ["---", f"# {filename}", ""]
    log_link = log_path.removesuffix(".md")
    lines += [f"- 送信元：[[{log_link}|{chat.title} {date_str}]]（{hhmm} {sender}）", ""]
    lines += ["## 要約"] + [f"- {s}" for s in summary] + [""]
    if image is not None:
        lines += ["## 写っている文字（書き起こし）", image.transcription or "（文字は写っていません）", ""]
        lines += ["## 画像の説明", image.description or "（説明なし）", ""]
    else:
        lines += ["## 全文", full_text.strip() or "（テキストはありませんでした）", ""]
        if truncated:
            lines += ["（文字数の上限を超えたため、ここで省略しました）", ""]
    return "\n".join(lines)


def _alias(text: str) -> str:
    return re.sub(r"[\[\]|#^]", "_", text).strip() or "添付"


class Ingestor:
    def __init__(self, settings: Settings, store: GitHubStore, line: LineClient,
                 groups: GroupRegistry, transcriber: Transcriber) -> None:
        self.settings = settings
        self.store = store
        self.line = line
        self.groups = groups
        self.transcriber = transcriber

    # ---------------------------------------------------------------- 入口
    async def handle(self, payload: dict) -> str:
        kind = payload.get("kind")
        if kind == "bot_message":
            return await self.handle_bot_message(payload)
        if kind != "event":
            log.error("不明なタスクです: %s", kind)
            return "ignored"
        event = payload.get("event") or {}
        etype = event.get("type")
        source = event.get("source") or {}
        if etype == "leave":
            return "ignored"
        chat = await self.chat_for_source(source)
        if chat is None:
            return "ignored"
        if etype == "message":
            return await self.handle_message(event, chat)
        if etype == "unsend":
            return await self.handle_unsend(event, chat)
        if etype == "messageEdited":
            return await self.handle_edit(event, chat)
        return "registered" if chat.kind == "group" else "ignored"

    async def chat_for_source(self, source: dict) -> ChatRef | None:
        stype = source.get("type")
        if stype == "group" and source.get("groupId"):
            name = await self.groups.ensure(source["groupId"])
            return ChatRef("group", source["groupId"], name)
        if stype == "user" and source.get("userId"):
            name = await self.line.display_name(source)
            return ChatRef("user", source["userId"], name)
        if stype == "room" and source.get("roomId"):
            return ChatRef("room", source["roomId"], f"複数人トーク_{source['roomId'][-8:]}")
        return None

    async def _write_line(self, chat: ChatRef, dt: datetime, msg_id: str, line: str) -> str:
        date_str, hhmm = fmt_date(dt), fmt_hhmm(dt)
        path = chat.daily_log_path(date_str)
        header = daily_header(chat.title, dt.date())
        status = {"value": "duplicate"}

        def mutate(text: str | None) -> str | None:
            new_text, st = upsert_entry(text, header=header, line=line, time=hhmm, msg_id=msg_id)
            status["value"] = st
            return new_text

        await self.store.update(path, mutate, message=f"LINEログ: {chat.title} {date_str} m{msg_id}")
        return status["value"]

    # ---------------------------------------------------------------- メッセージ
    async def handle_message(self, event: dict, chat: ChatRef) -> str:
        msg = event.get("message") or {}
        msg_id = str(msg.get("id", ""))
        if not msg_id:
            return "ignored"
        dt = from_epoch_ms(event["timestamp"])
        path = chat.daily_log_path(fmt_date(dt))
        current = await self.store.get(path)
        existing = find_entry(current.text if current else None, msg_id)
        if existing is not None and not existing.is_failure:
            return "duplicate"  # 同じメッセージIDが既に記録されている

        sender = await self.line.display_name(event.get("source") or {})
        body, failure = await self._message_body(msg, chat, sender, dt)
        line = format_entry(fmt_hhmm(dt), sender, body, msg_id)
        status = await self._write_line(chat, dt, msg_id, line)
        if failure is not None and not failure.permanent and status != "duplicate":
            raise RetryLater(failure.reason)
        return status

    async def _message_body(self, msg: dict, chat: ChatRef, sender: str, dt: datetime) -> tuple[str, Failure | None]:
        mtype = msg.get("type")
        if mtype == "text":
            return msg.get("text", ""), None
        if mtype in MARKERS:
            return MARKERS[mtype], None
        if mtype == "image":
            image_set = msg.get("imageSet") or {}
            index, total = image_set.get("index"), image_set.get("total")
            label = f"画像 {index}/{total}" if index and total else "画像"
            filename = f"画像{index}" if index else "画像"
            return await self._attachment(msg, chat, sender, dt, filename=filename, kind="image", label=label)
        if mtype == "file":
            filename = msg.get("fileName") or "ファイル"
            kind = file_kind(filename)
            return await self._attachment(msg, chat, sender, dt, filename=filename, kind=kind,
                                          label=KIND_LABEL[kind], size=msg.get("fileSize"))
        return f"[{mtype or '不明な種類'}]", None

    async def _attachment(self, msg: dict, chat: ChatRef, sender: str, dt: datetime, *,
                          filename: str, kind: str, label: str, size: int | None = None) -> tuple[str, Failure | None]:
        msg_id = str(msg["id"])
        shown = "" if kind == "image" else f" {filename}"
        if kind == "other":
            return f"[ファイル]{shown}（テキスト化の対象外の形式）", None
        limit = self.settings.max_attachment_bytes
        if size is not None and int(size) > limit:
            return f"[{label}]{shown}（{limit // (1024 * 1024)}MBを超えるためテキスト化しません）", None

        date_str, hhmm = fmt_date(dt), fmt_hhmm(dt)
        note_path, existing_note = await self._choose_note_path(date_str, hhmm, filename, msg_id)
        alias = _alias("画像" if kind == "image" else filename)
        if existing_note is not None:
            # 前回の試行で添付ノートは書けている：要約を再利用する（再度テキスト化しない）
            summary = note_frontmatter(existing_note.text).get("summary") or []
            return self._attachment_body(label, note_path, alias, [str(s) for s in summary]), None
        try:
            data = await self.line.message_content(msg, limit)
            log_path = chat.daily_log_path(date_str)
            common = dict(kind_label=label.split(" ")[0], filename=filename if kind != "image" else label,
                          chat=chat, sender=sender, dt=dt, message_id=msg_id, log_path=log_path)
            if kind == "image":
                result = await self.transcriber.image(data)
                summary = result.summary or ["（要約なし）"]
                note = render_attachment_note(summary=summary, image=result, **common)
            else:
                if kind == "pdf":
                    full = await self.transcriber.pdf(data, filename)
                elif kind == "text":
                    full = decode_text(data)
                else:
                    full = await extract_office(data, filename)
                truncated = len(full) > self.settings.max_extract_chars
                if truncated:
                    full = full[: self.settings.max_extract_chars]
                summary = await self.transcriber.summarize(full, filename, label)
                note = render_attachment_note(summary=summary, full_text=full, truncated=truncated, **common)
            note_path = await self._write_note(note_path, note, date_str, hhmm, filename, msg_id)
            return self._attachment_body(label, note_path, alias, summary), None
        except Exception as exc:  # テキスト化の失敗は行に記録して、再試行に任せる
            reason, permanent = classify_failure(exc)
            log.warning("テキスト化に失敗しました（m%s, %s）: %s", msg_id, kind, reason, exc_info=not permanent)
            return f"[{label}]{shown} {FAILURE_MARK}{reason}]", Failure(reason, permanent)

    @staticmethod
    def _attachment_body(label: str, note_path: str, alias: str, summary: list[str]) -> str:
        link = f"[[{note_path}|{alias}]]"
        return f"[{label}] {link}" + "".join(f"<br>{s}" for s in summary if s)

    async def _choose_note_path(self, date_str: str, hhmm: str, filename: str,
                                msg_id: str) -> tuple[str, RemoteFile | None]:
        """添付ノートのパスを決める。同名の別メッセージのノートがあれば末尾にIDを付ける。
        同じメッセージのノートが既にあれば、そのパスと内容を返す。"""
        for suffix in ("", f"m{msg_id}"):
            path = attachment_note_path(date_str, hhmm, filename, suffix)
            current = await self.store.get(path)
            if current is None:
                return path, None
            if note_message_id(current.text) == msg_id:
                return path, current
        return attachment_note_path(date_str, hhmm, filename, f"m{msg_id}"), None

    async def _write_note(self, path: str, note: str, date_str: str, hhmm: str, filename: str, msg_id: str) -> str:
        def mutate(text: str | None) -> str | None:
            if text is None or note_message_id(text) == msg_id:
                return note
            return None  # 別のメッセージのノートが先にできていた

        result = await self.store.update(path, mutate, message=f"LINEログ: 添付 {filename} m{msg_id}")
        if result.changed or (result.text is not None and note_message_id(result.text) == msg_id):
            return path
        alt = attachment_note_path(date_str, hhmm, filename, f"m{msg_id}")
        await self.store.update(alt, lambda _t: note, message=f"LINEログ: 添付 {filename} m{msg_id}")
        return alt

    # ---------------------------------------------------------------- 送信取消
    async def handle_unsend(self, event: dict, chat: ChatRef) -> str:
        msg_id = str((event.get("unsend") or {}).get("messageId", ""))
        if not msg_id:
            return "ignored"
        dt = from_epoch_ms(event["timestamp"])
        known_name = await self.line.display_name(event.get("source") or {})
        for k in range(LOOKBACK_DAYS + 1):
            date_str = fmt_date(dt - timedelta(days=k))
            path = chat.daily_log_path(date_str)
            current = await self.store.get(path)
            entry = find_entry(current.text if current else None, msg_id)
            if entry is None:
                continue
            if entry.is_unsent:
                return "already"
            # 先に添付ノートを消す（途中で失敗しても再試行で同じ処理をやり直せる）
            for note in attachment_links(entry.rest):
                await self.store.delete_if(
                    note, lambda text: note_message_id(text) in (msg_id, None),
                    message=f"LINEログ: 送信取消 m{msg_id} の添付を削除",
                )

            def mutate(text: str | None) -> str | None:
                return mark_unsent(text, msg_id, known_name)[0]

            await self.store.update(path, mutate, message=f"LINEログ: 送信取消 {chat.title} {date_str} m{msg_id}")
            return "unsent"
        # 元のメッセージが記録されていない場合も、送信取消があったことを残す
        # （後から元のメッセージの記録処理が来ても、重複として書き込まれない）
        line = format_entry(fmt_hhmm(dt), known_name, UNSENT_BODY, msg_id)
        await self._write_line(chat, dt, msg_id, line)
        return "unsent_not_found"

    # ---------------------------------------------------------------- 編集
    async def handle_edit(self, event: dict, chat: ChatRef) -> str:
        msg = event.get("message") or {}
        msg_id = str(msg.get("id", ""))
        if not msg_id or msg.get("type") not in (None, "text"):
            return "ignored"
        new_text = msg.get("text", "")
        ts = int(event["timestamp"])
        dt = from_epoch_ms(ts)
        known_name = await self.line.display_name(event.get("source") or {})
        for k in range(LOOKBACK_DAYS + 1):
            date_str = fmt_date(dt - timedelta(days=k))
            path = chat.daily_log_path(date_str)
            current = await self.store.get(path)
            if find_entry(current.text if current else None, msg_id) is None:
                continue
            return await self._apply_edit(path, chat, date_str, msg_id, new_text, ts, known_name)
        line = format_entry(fmt_hhmm(dt), known_name, new_text + EDITED_MARK, msg_id, edited_ts=ts)
        await self._write_line(chat, dt, msg_id, line)
        return "edited_not_found"

    async def _apply_edit(self, path: str, chat: ChatRef, date_str: str, msg_id: str,
                          new_text: str, ts: int, known_name: str) -> str:
        status = {"value": "not_found"}

        def mutate(text: str | None) -> str | None:
            new, st = apply_edit(text, msg_id, new_text, ts, known_name)
            status["value"] = st
            return new

        await self.store.update(path, mutate, message=f"LINEログ: 編集 {chat.title} {date_str} m{msg_id}")
        return status["value"]

    # ---------------------------------------------------------------- ボット自身の送信
    async def handle_bot_message(self, payload: dict) -> str:
        chat_info = payload.get("chat") or {}
        chat = await self.chat_for_source(chat_info)
        if chat is None:
            return "ignored"
        msg_id = str(payload.get("messageId", ""))
        if not msg_id:
            return "ignored"
        dt = from_epoch_ms(payload["timestamp"])
        name = await self.line.bot_name()
        line = format_entry(fmt_hhmm(dt), name, payload.get("text", ""), msg_id)
        return await self._write_line(chat, dt, msg_id, line)
