"""記録処理のテスト（保存先・種類ごとの記録内容・添付ノート・編集・ボットの送信・グループ登録）。"""

import pytest

from app.ingest import note_frontmatter
from app.paths import GROUPS_FILE

from .conftest import GROUP_ID, OWNER_ID
from .helpers import (
    MEMBER_ID,
    base_event,
    edit_event,
    file_event,
    image_event,
    make_ingestor,
    ms,
    source,
    text_event,
)

LOG = "LINEログ/家族グループ/2026-11-14.md"


@pytest.fixture
def ing(settings, store, line, transcriber):
    line.group_names[GROUP_ID] = "家族グループ"
    line.names[MEMBER_ID] = "山田"
    line.names[OWNER_ID] = "オーナー"
    return make_ingestor(settings, store, line, transcriber)


async def handle(ing, event):
    return await ing.handle({"kind": "event", "event": event})


@pytest.mark.anyio
async def test_first_group_event_registers_group(ing, github):
    await handle(ing, text_event("1", "はじめまして", ms(2026, 11, 14, 9, 0)))
    groups = github.text(GROUPS_FILE)
    assert f"- 家族グループ：{GROUP_ID}" in groups
    # 2回目以降は追記しない
    await handle(ing, text_event("2", "2回目", ms(2026, 11, 14, 9, 1)))
    assert github.text(GROUPS_FILE).count(GROUP_ID) == 1


@pytest.mark.anyio
async def test_join_event_registers_group_without_log(ing, github):
    status = await handle(ing, base_event("join", ms(2026, 11, 14, 9, 0), source(user_id=None)))
    assert status == "registered"
    assert GROUP_ID in github.text(GROUPS_FILE)
    assert LOG not in github.files


@pytest.mark.anyio
async def test_registered_name_is_used_even_after_rename(ing, github, line):
    await handle(ing, text_event("1", "a", ms(2026, 11, 14, 9, 0)))
    line.group_names[GROUP_ID] = "改名後のグループ"  # LINE 上で改名されても
    ing.groups._cache = None
    await handle(ing, text_event("2", "b", ms(2026, 11, 14, 9, 1)))
    assert "^m2" in github.text(LOG)  # 登録時の名前のフォルダに記録し続ける
    # グループ一覧.md を手で書き換えると、その名前が以降の保存先になる
    github.put_text(GROUPS_FILE, github.text(GROUPS_FILE).replace("家族グループ：", "家族：", 1))
    ing.groups._cache = None
    await handle(ing, text_event("3", "c", ms(2026, 11, 14, 9, 2)))
    assert "^m3" in github.text("LINEログ/家族/2026-11-14.md")


@pytest.mark.anyio
async def test_reserved_and_duplicate_group_names(settings, store, github, line, transcriber):
    g1, g2 = "C" + "2" * 32, "C" + "3" * 32
    line.group_names.update({g1: "個別", g2: "個別"})
    ing = make_ingestor(settings, store, line, transcriber)
    await handle(ing, text_event("1", "a", ms(2026, 11, 14, 9, 0), source(group_id=g1)))
    await handle(ing, text_event("2", "b", ms(2026, 11, 14, 9, 0), source(group_id=g2)))
    assert "LINEログ/個別（グループ）/2026-11-14.md" in github.files
    assert "LINEログ/個別（3333）（グループ）/2026-11-14.md" not in github.files
    assert f"- 個別（3333）：{g2}" in github.text(GROUPS_FILE)
    assert "LINEログ/個別（3333）/2026-11-14.md" in github.files


@pytest.mark.anyio
async def test_one_to_one_path_uses_display_name(ing, github):
    await handle(ing, text_event("5", "個別の相談", ms(2026, 11, 14, 22, 15), source(kind="user", user_id=OWNER_ID)))
    assert github.text("LINEログ/個別/オーナー/2026-11-14.md") == "# 2026-11-14（土） オーナー\n\n- 22:15 オーナー：個別の相談 ^m5\n"


@pytest.mark.anyio
async def test_date_and_time_are_japan_time(ing, github):
    # UTC では 11/14 15:30 だが、日本時間では 11/15 00:30
    await handle(ing, text_event("6", "深夜", ms(2026, 11, 15, 0, 30)))
    assert "- 00:30 山田：深夜 ^m6" in github.text("LINEログ/家族グループ/2026-11-15.md")


@pytest.mark.anyio
async def test_unknown_sender_in_group(ing, github):
    await handle(ing, text_event("7", "PC版から", ms(2026, 11, 14, 9, 0), source(user_id=None)))
    assert "- 09:00 不明なユーザー：PC版から ^m7" in github.text(LOG)


@pytest.mark.anyio
@pytest.mark.parametrize("mtype, marker", [("video", "[動画]"), ("audio", "[音声]"), ("sticker", "[スタンプ]"), ("location", "[位置情報]")])
async def test_media_markers_only(ing, github, line, mtype, marker):
    message = {"type": mtype, "id": "8"}
    await handle(ing, base_event("message", ms(2026, 11, 14, 9, 0), source(), replyToken="r", message=message))
    assert f"- 09:00 山田：{marker} ^m8" in github.text(LOG)


@pytest.mark.anyio
async def test_image_note_and_log_line(ing, github, line):
    line.contents["9"] = b"img"
    await handle(ing, image_event("9", ms(2026, 11, 14, 9, 30), image_set={"id": "set1", "index": 2, "total": 3}))
    note_path = "LINEログ/添付/2026-11-14_0930_画像2.md"
    note = github.text(note_path)
    fm = note_frontmatter(note)
    assert fm["message_id"] == "9" and fm["chat"] == "家族グループ" and fm["sender"] == "山田"
    assert fm["summary"] == ["会議の予定のメモ", "10時開始"]
    assert "## 写っている文字（書き起こし）\n会議 10時から" in note
    assert "## 画像の説明\nホワイトボードの写真" in note
    assert "[[LINEログ/家族グループ/2026-11-14|家族グループ 2026-11-14]]" in note
    assert (f"- 09:30 山田：[画像 2/3] [[{note_path}|画像]]<br>会議の予定のメモ<br>10時開始 ^m9") in github.text(LOG)


@pytest.mark.anyio
async def test_same_minute_attachments_get_unique_note_paths(ing, github, line):
    line.contents.update({"10": b"a", "11": b"b"})
    await handle(ing, image_event("10", ms(2026, 11, 14, 9, 30)))
    await handle(ing, image_event("11", ms(2026, 11, 14, 9, 30, 30)))
    assert "LINEログ/添付/2026-11-14_0930_画像.md" in github.files
    assert note_frontmatter(github.text("LINEログ/添付/2026-11-14_0930_画像_m11.md"))["message_id"] == "11"
    assert "[[LINEログ/添付/2026-11-14_0930_画像_m11.md|画像]]" in github.text(LOG)


@pytest.mark.anyio
async def test_pdf_file(ing, github, line, transcriber):
    line.contents["12"] = b"%PDF"
    await handle(ing, file_event("12", "見積書.pdf", ms(2026, 11, 14, 10, 5)))
    note = github.text("LINEログ/添付/2026-11-14_1005_見積書.pdf.md")
    assert "## 全文\n--- p.1 ---\n見積書\n合計 10,000円" in note
    assert transcriber.pdf_calls == 1 and transcriber.summary_calls == 1
    assert "- 10:05 山田：[PDF] [[LINEログ/添付/2026-11-14_1005_見積書.pdf.md|見積書.pdf]]<br>見積書.pdf の要約<br>合計金額の記載あり ^m12" in github.text(LOG)


@pytest.mark.anyio
async def test_docx_file_is_extracted_with_library(ing, github, line):
    from .test_extract import make_docx

    line.contents["13"] = make_docx()
    await handle(ing, file_event("13", "議事録.docx", ms(2026, 11, 14, 10, 6)))
    note = github.text("LINEログ/添付/2026-11-14_1006_議事録.docx.md")
    assert "出席者：山田、佐藤" in note
    assert "[Word] [[LINEログ/添付/2026-11-14_1006_議事録.docx.md|議事録.docx]]" in github.text(LOG)


@pytest.mark.anyio
async def test_text_file_in_shift_jis(ing, github, line):
    line.contents["14"] = "名前,点数\n山田,90\n".encode("cp932")
    await handle(ing, file_event("14", "点数.csv", ms(2026, 11, 14, 10, 7)))
    assert "山田,90" in github.text("LINEログ/添付/2026-11-14_1007_点数.csv.md")


@pytest.mark.anyio
async def test_unsupported_and_too_large_files(ing, github, line, settings):
    await handle(ing, file_event("15", "写真.zip", ms(2026, 11, 14, 10, 8)))
    await handle(ing, file_event("16", "巨大.pdf", ms(2026, 11, 14, 10, 9), size=settings.max_attachment_bytes + 1))
    log = github.text(LOG)
    assert "- 10:08 山田：[ファイル] 写真.zip（テキスト化の対象外の形式） ^m15" in log
    assert "- 10:09 山田：[PDF] 巨大.pdf（50MBを超えるためテキスト化しません） ^m16" in log
    assert not any(p.startswith("LINEログ/添付/") for p in github.files)


@pytest.mark.anyio
async def test_fetch_failure_is_recorded_as_text_failure(ing, github):
    from app.ingest import RetryLater

    with pytest.raises(RetryLater):
        await handle(ing, file_event("17", "届かない.pdf", ms(2026, 11, 14, 10, 10)))
    assert "[PDF] 届かない.pdf [テキスト化失敗：LINEからの取得に失敗しました（HTTP 404）] ^m17" in github.text(LOG)


@pytest.mark.anyio
async def test_filename_with_forbidden_characters(ing, github, line):
    line.contents["18"] = b"hello"
    await handle(ing, file_event("18", "a/b:c#d[1].txt", ms(2026, 11, 14, 10, 11)))
    assert "LINEログ/添付/2026-11-14_1011_a_b_c_d_1_.txt.md" in github.files


# ---------------------------------------------------------------- 編集
@pytest.mark.anyio
async def test_edit_rewrites_line(ing, github):
    await handle(ing, text_event("20", "明日は9時集合", ms(2026, 11, 14, 9, 0)))
    status = await handle(ing, edit_event("20", "明日は10時集合", ms(2026, 11, 14, 9, 3)))
    assert status == "edited"
    assert f"- 09:00 山田：明日は10時集合（編集済み） %%edited:{ms(2026, 11, 14, 9, 3)}%% ^m20" in github.text(LOG)


@pytest.mark.anyio
async def test_edits_arriving_out_of_order_keep_latest(ing, github):
    await handle(ing, text_event("21", "v1", ms(2026, 11, 14, 9, 0)))
    await handle(ing, edit_event("21", "v3", ms(2026, 11, 14, 9, 5)))
    assert await handle(ing, edit_event("21", "v2", ms(2026, 11, 14, 9, 4))) == "stale"
    assert "山田：v3（編集済み）" in github.text(LOG) and "v2" not in github.text(LOG)


@pytest.mark.anyio
async def test_edit_of_unrecorded_message_is_inserted(ing, github):
    status = await handle(ing, edit_event("22", "編集後の本文", ms(2026, 11, 14, 9, 7)))
    assert status == "edited_not_found"
    assert "- 09:07 山田：編集後の本文（編集済み）" in github.text(LOG)
    # その後に元のメッセージの記録処理が来ても、編集後の内容を上書きしない
    assert await handle(ing, text_event("22", "編集前", ms(2026, 11, 14, 9, 6))) == "duplicate"


# ---------------------------------------------------------------- ボット自身の送信
@pytest.mark.anyio
async def test_bot_message_is_logged(ing, github):
    payload = {"kind": "bot_message", "chat": {"type": "group", "groupId": GROUP_ID},
               "messageId": "30", "text": "お知らせ\n明日は休みです", "timestamp": ms(2026, 11, 14, 18, 0)}
    assert await ing.handle(payload) == "inserted"
    assert "- 18:00 記録ボット：お知らせ<br>明日は休みです ^m30" in github.text(LOG)
    assert await ing.handle(payload) == "duplicate"


@pytest.mark.anyio
async def test_leave_event_is_ignored(ing, github):
    assert await handle(ing, base_event("leave", ms(2026, 11, 14, 9, 0), source(user_id=None))) == "ignored"
    assert github.files == {}
