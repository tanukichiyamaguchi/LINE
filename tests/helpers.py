"""テストで使うイベントの組み立てなど。"""

from __future__ import annotations

import itertools
from datetime import datetime

from app.groups import GroupRegistry
from app.ingest import Ingestor
from app.timeutil import JST

from .conftest import GROUP_ID, OWNER_ID

MEMBER_ID = "U" + "b" * 32
_counter = itertools.count(1)


def ms(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=JST).timestamp() * 1000)


def source(kind: str = "group", user_id: str | None = MEMBER_ID, group_id: str = GROUP_ID) -> dict:
    if kind == "group":
        src = {"type": "group", "groupId": group_id}
    elif kind == "room":
        src = {"type": "room", "roomId": "R" + "9" * 32}
    else:
        src = {"type": "user"}
    if user_id:
        src["userId"] = user_id
    return src


def base_event(etype: str, ts: int, src: dict, **extra) -> dict:
    ev = {
        "type": etype,
        "timestamp": ts,
        "source": src,
        "mode": "active",
        "webhookEventId": f"01TESTEVENT{next(_counter):010d}",
        "deliveryContext": {"isRedelivery": False},
    }
    ev.update(extra)
    return ev


def text_event(msg_id: str, text: str, ts: int, src: dict | None = None) -> dict:
    return base_event("message", ts, src or source(), replyToken="reply-token",
                      message={"type": "text", "id": msg_id, "text": text, "quoteToken": "q"})


def image_event(msg_id: str, ts: int, src: dict | None = None, image_set: dict | None = None) -> dict:
    message = {"type": "image", "id": msg_id, "contentProvider": {"type": "line"}, "quoteToken": "q"}
    if image_set:
        message["imageSet"] = image_set
    return base_event("message", ts, src or source(), replyToken="reply-token", message=message)


def file_event(msg_id: str, filename: str, ts: int, size: int = 1000, src: dict | None = None) -> dict:
    return base_event("message", ts, src or source(), replyToken="reply-token",
                      message={"type": "file", "id": msg_id, "fileName": filename, "fileSize": size})


def unsend_event(msg_id: str, ts: int, src: dict | None = None) -> dict:
    return base_event("unsend", ts, src or source(), unsend={"messageId": msg_id})


def edit_event(msg_id: str, text: str, ts: int, src: dict | None = None) -> dict:
    return base_event("messageEdited", ts, src or source(), replyToken="reply-token",
                      message={"type": "text", "id": msg_id, "text": text, "quoteToken": "q"})


def owner_text_event(text: str, ts: int, msg_id: str = "500") -> dict:
    return text_event(msg_id, text, ts, source(kind="user", user_id=OWNER_ID))


def make_ingestor(settings, store, line, transcriber) -> Ingestor:
    return Ingestor(settings, store, line, GroupRegistry(store, line), transcriber)
