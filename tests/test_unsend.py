"""送信取消のテスト：該当行を「（送信取消）」に置き換え、添付ノートがあれば削除する。"""

from datetime import datetime

import pytest

from app.logformat import mark_unsent, parse_entry
from app.timeutil import JST

from .conftest import GROUP_ID
from .helpers import MEMBER_ID, file_event, image_event, make_ingestor, ms, source, text_event, unsend_event

LOG = "LINEログ/家族グループ/2026-11-14.md"
NEXT_LOG = "LINEログ/家族グループ/2026-11-15.md"


def test_mark_unsent_replaces_line_and_returns_attachments():
    text = (
        "# h\n\n"
        "- 09:00 山田：おはよう ^m1\n"
        "- 09:05 山田：[PDF] [[LINEログ/添付/2026-11-14_0905_見積書.pdf.md|見積書.pdf]]<br>要約 ^m2\n"
    )
    new, attachments, status = mark_unsent(text, "2", known_name="山田")
    assert status == "unsent"
    assert attachments == ["LINEログ/添付/2026-11-14_0905_見積書.pdf.md"]
    assert "- 09:05 山田：（送信取消） ^m2" in new
    assert "- 09:00 山田：おはよう ^m1" in new
    assert parse_entry("- 09:05 山田：（送信取消） ^m2").is_unsent


def test_mark_unsent_twice_and_not_found():
    text = "- 09:00 山田：（送信取消） ^m1\n"
    assert mark_unsent(text, "1") == (None, [], "already")
    assert mark_unsent(text, "999") == (None, [], "not_found")
    assert mark_unsent(None, "1") == (None, [], "not_found")


def test_mark_unsent_keeps_edit_free_and_time():
    text = "- 23:58 A：（編集済み）の本文（編集済み） %%edited:5%% ^m3\n"
    new, _, status = mark_unsent(text, "3", known_name="A")
    assert status == "unsent"
    assert new == "- 23:58 A：（送信取消） ^m3\n"


@pytest.fixture
def ing(settings, store, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.names[MEMBER_ID] = "山田"
    return make_ingestor(settings, store, line, transcriber)


@pytest.mark.anyio
async def test_unsend_text_message(ing, github):
    await ing.handle({"kind": "event", "event": text_event("11", "まちがえた", ms(2026, 11, 14, 9, 0))})
    await ing.handle({"kind": "event", "event": text_event("12", "つぎ", ms(2026, 11, 14, 9, 1))})
    status = await ing.handle({"kind": "event", "event": unsend_event("11", ms(2026, 11, 14, 9, 2))})
    assert status == "unsent"
    text = github.text(LOG)
    assert "- 09:00 山田：（送信取消） ^m11" in text
    assert "まちがえた" not in text
    assert "- 09:01 山田：つぎ ^m12" in text


@pytest.mark.anyio
async def test_unsend_deletes_attachment_note(ing, github, line):
    line.contents["21"] = b"img"
    await ing.handle({"kind": "event", "event": image_event("21", ms(2026, 11, 14, 9, 30))})
    note = "LINEログ/添付/2026-11-14_0930_画像.md"
    assert note in github.files
    assert await ing.handle({"kind": "event", "event": unsend_event("21", ms(2026, 11, 14, 9, 31))}) == "unsent"
    assert note not in github.files
    assert "- 09:30 山田：（送信取消） ^m21" in github.text(LOG)


@pytest.mark.anyio
async def test_unsend_deletes_file_note(ing, github, line):
    line.contents["22"] = b"%PDF-1.4"
    await ing.handle({"kind": "event", "event": file_event("22", "見積書.pdf", ms(2026, 11, 14, 9, 40))})
    note = "LINEログ/添付/2026-11-14_0940_見積書.pdf.md"
    assert "message_id: \"22\"" in github.text(note)
    await ing.handle({"kind": "event", "event": unsend_event("22", ms(2026, 11, 14, 9, 41))})
    assert note not in github.files


@pytest.mark.anyio
async def test_unsend_does_not_delete_other_messages_note(ing, github, line):
    line.contents["31"] = b"img"
    await ing.handle({"kind": "event", "event": image_event("31", ms(2026, 11, 14, 9, 50))})
    note = "LINEログ/添付/2026-11-14_0950_画像.md"
    # リンク先のノートが別のメッセージのものに置き換わっていたら消さない
    github.put_text(note, github.text(note).replace('message_id: "31"', 'message_id: "99"'))
    await ing.handle({"kind": "event", "event": unsend_event("31", ms(2026, 11, 14, 9, 51))})
    assert note in github.files


@pytest.mark.anyio
async def test_unsend_after_midnight_finds_previous_day(ing, github):
    await ing.handle({"kind": "event", "event": text_event("41", "夜の投稿", ms(2026, 11, 14, 23, 59))})
    status = await ing.handle({"kind": "event", "event": unsend_event("41", ms(2026, 11, 15, 0, 3))})
    assert status == "unsent"
    assert "- 23:59 山田：（送信取消） ^m41" in github.text(LOG)
    assert "LINEログ/家族グループ/2026-11-15.md" not in github.files


@pytest.mark.anyio
async def test_unsend_before_message_is_recorded_leaves_tombstone(ing, github):
    status = await ing.handle({"kind": "event", "event": unsend_event("51", ms(2026, 11, 14, 13, 0))})
    assert status == "unsent_not_found"
    assert "- 13:00 山田：（送信取消） ^m51" in github.text(LOG)
    # 遅れて元のメッセージの記録処理が来ても、内容は書き込まれない（重複として扱う）
    status = await ing.handle({"kind": "event", "event": text_event("51", "見られたくない", ms(2026, 11, 14, 12, 59))})
    assert status == "duplicate"
    assert "見られたくない" not in github.text(LOG)


@pytest.mark.anyio
async def test_unsend_after_midnight_processed_before_message_keeps_content_out(ing, github):
    # 23:59 の投稿を翌日 0:03 に取り消し、元の投稿の記録処理が取り消しより後になった場合
    status = await ing.handle({"kind": "event", "event": unsend_event("52", ms(2026, 11, 15, 0, 3))})
    assert status == "unsent_not_found"
    assert "- 00:03 山田：（送信取消） ^m52" in github.text(NEXT_LOG)
    status = await ing.handle({"kind": "event", "event": text_event("52", "見られたくない", ms(2026, 11, 14, 23, 59))})
    assert status == "duplicate"
    assert LOG not in github.files  # 前日のログに取り消した内容を書かない
    assert "見られたくない" not in github.text(NEXT_LOG)


@pytest.mark.anyio
async def test_unsend_after_midnight_processed_before_image_skips_transcription(ing, github, line, transcriber):
    line.contents["53"] = b"img"
    await ing.handle({"kind": "event", "event": unsend_event("53", ms(2026, 11, 15, 0, 3))})
    status = await ing.handle({"kind": "event", "event": image_event("53", ms(2026, 11, 14, 23, 59))})
    assert status == "duplicate"
    assert transcriber.image_calls == 0  # テキスト化しない
    assert not any(p.startswith("LINEログ/添付/") for p in github.files)  # 添付ノートも作らない


@pytest.mark.anyio
async def test_message_recorded_on_its_own_day_does_not_read_later_days(settings, store, github, line, transcriber):
    # 届いた日のうちに記録される通常の場合は、翌日以降のログを読みにいかない（GitHub への問い合わせを増やさない）
    line.group_names[GROUP_ID] = "家族グループ"
    line.names[MEMBER_ID] = "山田"
    ing = make_ingestor(settings, store, line, transcriber, now=datetime(2026, 11, 14, 23, 59, 30, tzinfo=JST))
    status = await ing.handle({"kind": "event", "event": text_event("54", "こんばんは", ms(2026, 11, 14, 23, 59))})
    assert status == "inserted"
    assert not any(p.startswith("LINEログ/家族グループ/2026-11-15") for p in github.reads)


@pytest.mark.anyio
async def test_unsend_replaces_failure_line_and_retry_does_not_restore(ing, github, line, transcriber):
    from app.ingest import RetryLater
    from app.transcribe import TranscribeError

    line.contents["61"] = b"img"
    transcriber.fail_with = TranscribeError("一時的なエラー")
    with pytest.raises(RetryLater):
        await ing.handle({"kind": "event", "event": image_event("61", ms(2026, 11, 14, 14, 0))})
    await ing.handle({"kind": "event", "event": unsend_event("61", ms(2026, 11, 14, 14, 1))})
    transcriber.fail_with = None
    assert await ing.handle({"kind": "event", "event": image_event("61", ms(2026, 11, 14, 14, 0))}) == "duplicate"
    assert "- 14:00 山田：（送信取消） ^m61" in github.text(LOG)
    assert transcriber.image_calls == 1


@pytest.mark.anyio
async def test_unsend_in_one_to_one_chat(settings, store, github, line, transcriber):
    user = "U" + "c" * 32
    line.names[user] = "佐藤"
    ing = make_ingestor(settings, store, line, transcriber)
    src = source(kind="user", user_id=user)
    await ing.handle({"kind": "event", "event": text_event("71", "個別の話", ms(2026, 11, 14, 8, 0), src)})
    await ing.handle({"kind": "event", "event": unsend_event("71", ms(2026, 11, 14, 8, 1), src)})
    assert github.text("LINEログ/個別/佐藤/2026-11-14.md").endswith("- 08:00 佐藤：（送信取消） ^m71\n")
