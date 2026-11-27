"""重複防止のテスト：同じメッセージIDが日別ログにあれば書き込まない。"""

import pytest

from app.ingest import RetryLater
from app.logformat import parse_entry, upsert_entry
from app.transcribe import TranscribeError

from .conftest import GROUP_ID
from .helpers import MEMBER_ID, image_event, make_ingestor, ms, text_event

LOG = "LINEログ/家族グループ/2026-11-14.md"


def entries(text: str) -> list:
    return [e for e in (parse_entry(line) for line in text.splitlines()) if e]


def test_upsert_returns_none_for_existing_message_id():
    text = "- 09:00 A：a ^m1\n"
    assert upsert_entry(text, header="", line="- 09:00 A：別の内容 ^m1", time="09:00", msg_id="1") == (None, "duplicate")


def test_upsert_replaces_failure_line():
    text = "- 09:00 A：[画像] [テキスト化失敗：一時的なエラー] ^m1\n"
    new, status = upsert_entry(text, header="", line="- 09:00 A：[画像] 成功 ^m1", time="09:00", msg_id="1")
    assert status == "replaced_failure"
    assert new == "- 09:00 A：[画像] 成功 ^m1\n"


@pytest.mark.anyio
async def test_same_event_twice_is_recorded_once(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.names[MEMBER_ID] = "山田"
    ing = make_ingestor(settings, store, line, transcriber)
    event = text_event("1001", "こんにちは", ms(2026, 11, 14, 9, 30))
    assert await ing.handle({"kind": "event", "event": event}) == "inserted"
    first = github.text(LOG)
    assert await ing.handle({"kind": "event", "event": event}) == "duplicate"
    assert github.text(LOG) == first
    assert [e.msg_id for e in entries(first)] == ["1001"]
    assert first == "# 2026-11-14（土） 家族グループ\n\n- 09:30 山田：こんにちは ^m1001\n"


@pytest.mark.anyio
async def test_redelivered_event_with_new_webhook_id_is_not_duplicated(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    ing = make_ingestor(settings, store, line, transcriber)
    ts = ms(2026, 11, 14, 9, 30)
    await ing.handle({"kind": "event", "event": text_event("1001", "やあ", ts)})
    redelivered = text_event("1001", "やあ", ts)
    redelivered["deliveryContext"] = {"isRedelivery": True}
    assert await ing.handle({"kind": "event", "event": redelivered}) == "duplicate"
    assert len(entries(github.text(LOG))) == 1


@pytest.mark.anyio
async def test_failed_transcription_is_recorded_then_replaced_on_retry(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.names[MEMBER_ID] = "山田"
    line.contents["2001"] = b"not-really-an-image"
    ing = make_ingestor(settings, store, line, transcriber)
    event = image_event("2001", ms(2026, 11, 14, 10, 0))

    transcriber.fail_with = TranscribeError("Claude API が混み合っています")
    with pytest.raises(RetryLater):
        await ing.handle({"kind": "event", "event": event})
    failed = github.text(LOG)
    assert "- 10:00 山田：[画像] [テキスト化失敗：Claude API が混み合っています] ^m2001" in failed

    # Cloud Tasks の再試行で成功すると、失敗の行が置き換わる（行は増えない）
    transcriber.fail_with = None
    assert await ing.handle({"kind": "event", "event": event}) == "replaced_failure"
    text = github.text(LOG)
    assert len(entries(text)) == 1
    assert "テキスト化失敗" not in text
    assert "[[LINEログ/添付/2026-11-14_1000_画像.md|画像]]" in text


@pytest.mark.anyio
async def test_permanent_failure_is_recorded_without_retry(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.contents["2002"] = b"x"
    transcriber.fail_with = TranscribeError("画像として読めませんでした", permanent=True)
    ing = make_ingestor(settings, store, line, transcriber)
    status = await ing.handle({"kind": "event", "event": image_event("2002", ms(2026, 11, 14, 10, 0))})
    assert status == "inserted"
    assert "[テキスト化失敗：画像として読めませんでした]" in github.text(LOG)


@pytest.mark.anyio
async def test_retry_reuses_existing_attachment_note_without_transcribing_again(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.contents["2003"] = b"img"
    ing = make_ingestor(settings, store, line, transcriber)
    event = image_event("2003", ms(2026, 11, 14, 11, 15))
    await ing.handle({"kind": "event", "event": event})
    assert transcriber.image_calls == 1
    # 日別ログの書き込みだけ失われた状況を再現する（添付ノートは残っている）
    github.files.pop(LOG)
    await ing.handle({"kind": "event", "event": event})
    assert transcriber.image_calls == 1  # 2回目は文字起こしをしない
    assert "<br>会議の予定のメモ<br>10時開始" in github.text(LOG)


@pytest.mark.anyio
async def test_duplicate_check_happens_before_expensive_transcription(settings, store, github, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.contents["2004"] = b"img"
    ing = make_ingestor(settings, store, line, transcriber)
    event = image_event("2004", ms(2026, 11, 14, 12, 0))
    await ing.handle({"kind": "event", "event": event})
    await ing.handle({"kind": "event", "event": event})
    assert transcriber.image_calls == 1
