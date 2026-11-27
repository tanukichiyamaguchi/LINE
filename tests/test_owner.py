"""オーナーとの1対1の処理のテスト（質問回答・お知らせ・リマインド操作・応答とプッシュの切り替え・通知待ち）。"""

import asyncio
import time
from pathlib import Path

import pytest

from app.agent import Answer
from app.groups import GroupRegistry
from app.owner import OwnerAssistant, is_no, is_yes
from app.paths import GROUPS_FILE, NOTICES_FILE, PENDING_SEND_FILE, REMINDERS_FILE
from app.reminders import find, parse_reminders
from app.state import add_notice, get_pending, list_notices, set_pending
from app.vault import VaultTools

from .conftest import GROUP_ID, OWNER_ID, make_settings
from .helpers import owner_text_event, source, text_event


class FakeRouter:
    """意図の判定（Claude）の代わり。メッセージに応じて決まった結果を返す。"""

    def __init__(self):
        self.routes = {}
        self.calls = []

    async def json_call(self, *, model, system, content, schema, effort=None, max_tokens=16000, streaming=False):
        self.calls.append(content)
        for key, route in self.routes.items():
            if key in content:
                return {"intent": "question", "group": "", "text": "", "schedule": "", "reminder_id": "", "reply": "", **route}
        return {"intent": "question", "group": "", "text": "", "schedule": "", "reminder_id": "", "reply": ""}


class FakeVault:
    def __init__(self, root):
        self.tools = VaultTools(root)
        self.refreshed = 0

    async def refresh(self):
        self.refreshed += 1


class FakeAgent:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.questions = []

    async def answer(self, question, tools):
        self.questions.append(question)
        if self.delay:
            await asyncio.sleep(self.delay)
        return Answer(found=True, text="10時集合です。", sources=[("LINEログ/家族グループ/2026-11-13.md", "2026-11-13")])


def now_ms(offset_seconds=0.0):
    return int((time.time() + offset_seconds) * 1000)


@pytest.fixture
def router():
    return FakeRouter()


@pytest.fixture
def make_owner(store, github, line, tasks, router, tmp_path: Path):
    github.put_text(GROUPS_FILE, f"# グループ一覧\n\n- 家族グループ：{GROUP_ID}\n")

    def _make(**overrides):
        settings = make_settings(**overrides)
        owner = OwnerAssistant(settings, store, line, GroupRegistry(store, line), router, FakeVault(tmp_path), tasks)
        owner.agent = FakeAgent()
        return owner

    return _make


def texts(messages):
    return [m.text for m in messages]


# ---------------------------------------------------------------- 対象の判定
@pytest.mark.anyio
async def test_non_owner_is_ignored(make_owner, line):
    owner = make_owner()
    other = text_event("1", "質問", now_ms(), source(kind="user", user_id="U" + "e" * 32))
    assert await owner.handle(other) == "ignored"
    group = text_event("2", "質問", now_ms(), source(kind="group", user_id=OWNER_ID))
    assert await owner.handle(group) == "ignored"
    assert line.replies == [] and line.pushes == [] and line.loading == []


# ---------------------------------------------------------------- 質問回答と 50 秒の判定
@pytest.mark.anyio
async def test_question_within_deadline_uses_reply(make_owner, line):
    owner = make_owner()
    assert await owner.handle(owner_text_event("BBQは何時集合？", now_ms())) == "answered"
    assert line.loading == [OWNER_ID]  # 受け付けたらローディング表示
    assert len(line.replies) == 1 and line.pushes == []
    token, messages = line.replies[0]
    assert token == "reply-token"
    assert texts(messages) == ["10時集合です。\n\n📝 根拠\n・LINEログ/家族グループ/2026-11-13.md（2026-11-13）"]
    assert owner.agent.questions == ["BBQは何時集合？"]
    assert owner.vault.refreshed == 1  # 質問のたびに Vault を最新化


@pytest.mark.anyio
async def test_question_after_deadline_uses_push(make_owner, line):
    owner = make_owner()
    await owner.handle(owner_text_event("BBQは何時集合？", now_ms(-55)))
    assert line.replies == []
    assert [to for to, _m, _k in line.pushes] == [OWNER_ID]


@pytest.mark.anyio
async def test_reply_failure_falls_back_to_push(make_owner, line):
    from app.line_api import LineApiError

    line.reply_error = LineApiError("Invalid reply token", 400)
    await make_owner().handle(owner_text_event("質問", now_ms()))
    assert len(line.pushes) == 1


# ---------------------------------------------------------------- お知らせ
@pytest.mark.anyio
async def test_announce_confirm_then_send(make_owner, router, line, github, tasks):
    owner = make_owner()
    router.routes["家族グループに"] = {"intent": "announce", "group": "家族グループ", "text": "明日は10時集合です"}
    await owner.handle(owner_text_event("家族グループに『明日は10時集合です』と送って", now_ms()))
    confirm = line.replies[-1][1][0]
    assert "送り先：家族グループ" in confirm.text and "明日は10時集合です" in confirm.text
    assert confirm.quick_replies == ("はい", "いいえ")
    assert line.pushes == []  # まだ送らない
    assert get_pending(github.text(PENDING_SEND_FILE))["group_id"] == GROUP_ID

    await owner.handle(owner_text_event("はい", now_ms(), msg_id="501"))
    assert len(line.pushes) == 1
    to, messages, retry_key = line.pushes[0]
    assert to == GROUP_ID and texts(messages) == ["明日は10時集合です"] and retry_key
    assert texts(line.replies[-1][1]) == ["「家族グループ」に送信しました。"]
    assert get_pending(github.text(PENDING_SEND_FILE)) is None
    logged = [p for _q, _path, p, _id in tasks.enqueued if p.get("kind") == "bot_message"]
    assert logged and logged[0]["text"] == "明日は10時集合です"

    # もう一度「はい」と言っても二重には送らない
    await owner.handle(owner_text_event("はい", now_ms(), msg_id="502"))
    assert len(line.pushes) == 1
    assert texts(line.replies[-1][1]) == ["確認待ちのお知らせはありません。"]


@pytest.mark.anyio
async def test_announce_quota_insufficient(make_owner, router, line, github):
    owner = make_owner()
    line.quota_remaining = 2
    line.member_counts[GROUP_ID] = 4
    router.routes["送って"] = {"intent": "announce", "group": "家族", "text": "こんにちは"}
    await owner.handle(owner_text_event("家族にこんにちはと送って", now_ms()))
    await owner.handle(owner_text_event("はい", now_ms(), msg_id="501"))
    assert line.pushes == []
    assert "残り2通／必要4通" in line.replies[-1][1][0].text


@pytest.mark.anyio
async def test_announce_cancel_and_expiry(make_owner, router, line, github):
    owner = make_owner()
    router.routes["送って"] = {"intent": "announce", "group": "家族グループ", "text": "x"}
    await owner.handle(owner_text_event("家族グループにxと送って", now_ms()))
    await owner.handle(owner_text_event("いいえ", now_ms(), msg_id="501"))
    assert texts(line.replies[-1][1]) == ["お知らせの送信を取り消しました。"]
    # 期限切れ
    github.put_text(PENDING_SEND_FILE, set_pending({"id": "old", "group_id": GROUP_ID, "group_name": "家族グループ",
                                                    "text": "古い", "created": "2026-01-01 09:00",
                                                    "expires": "2026-01-01 09:30"}))
    await owner.handle(owner_text_event("はい", now_ms(), msg_id="502"))
    assert line.pushes == []
    assert "期限" in line.replies[-1][1][0].text


@pytest.mark.anyio
async def test_other_message_while_pending_cancels_it(make_owner, router, line, github):
    owner = make_owner()
    router.routes["送って"] = {"intent": "announce", "group": "家族グループ", "text": "x"}
    await owner.handle(owner_text_event("家族グループにxと送って", now_ms()))
    await owner.handle(owner_text_event("昨日の話の続きは？", now_ms(), msg_id="501"))
    msgs = texts(line.replies[-1][1])
    assert msgs[0] == "（確認待ちだったお知らせの送信は取り消しました）"
    assert get_pending(github.text(PENDING_SEND_FILE)) is None


@pytest.mark.anyio
async def test_announce_unknown_group(make_owner, router, line):
    owner = make_owner()
    router.routes["送って"] = {"intent": "announce", "group": "会社", "text": "x"}
    await owner.handle(owner_text_event("会社にxと送って", now_ms()))
    assert "「会社」というグループが見つかりません" in line.replies[-1][1][0].text


@pytest.mark.parametrize("text", ["はい", "はい。", "はい、送ってください", "ＯＫ", "送信"])
def test_yes_words(text):
    assert is_yes(text)


@pytest.mark.parametrize("text", ["はいたつの状況は？", "いいえ", "はいはい何でしょう、ところで先週の会議の結論はどうなりましたか"])
def test_not_yes_words(text):
    assert not is_yes(text)


def test_no_words():
    assert is_no("いいえ") and is_no("キャンセル") and not is_no("いいえ、それより先週の会議の資料を全部まとめて教えて")


# ---------------------------------------------------------------- リマインド
@pytest.mark.anyio
async def test_reminder_add_list_stop_resume(make_owner, router, line, github):
    owner = make_owner()
    router.routes["ゴミ出し"] = {"intent": "reminder_add", "group": "家族グループ", "schedule": "毎週 月,木 07:00",
                                 "text": "今日はゴミ出しの日です"}
    await owner.handle(owner_text_event("毎週月木の7時に家族グループへゴミ出しのリマインドを", now_ms()))
    reply = line.replies[-1][1][0].text
    assert reply.startswith("リマインドを登録しました。") and "ID：R1" in reply and "次回：" in reply
    r1 = find(parse_reminders(github.text(REMINDERS_FILE)), "R1")
    assert r1.target == "家族グループ" and r1.schedule_text == "毎週 月,木 07:00" and r1.enabled
    assert r1.updated_at is not None and r1.last_sent is None

    router.routes = {"一覧": {"intent": "reminder_list"}}
    await owner.handle(owner_text_event("リマインドの一覧", now_ms(), msg_id="501"))
    assert "R1［有効］家族グループ" in line.replies[-1][1][0].text

    router.routes = {"止めて": {"intent": "reminder_stop", "reminder_id": "R1"}}
    await owner.handle(owner_text_event("ゴミ出しのを止めて", now_ms(), msg_id="502"))
    assert find(parse_reminders(github.text(REMINDERS_FILE)), "R1").enabled is False
    assert "停止しました" in line.replies[-1][1][0].text

    router.routes = {"再開": {"intent": "reminder_resume", "reminder_id": "r1"}}
    await owner.handle(owner_text_event("R1を再開", now_ms(), msg_id="503"))
    assert find(parse_reminders(github.text(REMINDERS_FILE)), "R1").enabled is True

    router.routes = {"変更": {"intent": "reminder_update", "reminder_id": "R1", "schedule": "毎日 06:30"}}
    await owner.handle(owner_text_event("R1を毎日6時半に変更", now_ms(), msg_id="504"))
    r1 = find(parse_reminders(github.text(REMINDERS_FILE)), "R1")
    assert r1.schedule_text == "毎日 06:30" and r1.body == "今日はゴミ出しの日です"


@pytest.mark.anyio
async def test_reminder_add_with_bad_schedule(make_owner, router, line, github):
    owner = make_owner()
    router.routes["隔週"] = {"intent": "reminder_add", "group": "家族グループ", "schedule": "隔週 月 9:00", "text": "x"}
    await owner.handle(owner_text_event("隔週月曜にリマインド", now_ms()))
    assert "スケジュールを読み取れませんでした" in line.replies[-1][1][0].text
    assert REMINDERS_FILE not in github.files


@pytest.mark.anyio
async def test_reminder_change_unknown_id_lists_reminders(make_owner, router, line):
    owner = make_owner()
    router.routes["止めて"] = {"intent": "reminder_stop", "reminder_id": ""}
    await owner.handle(owner_text_event("あのリマインドを止めて", now_ms()))
    assert "特定できませんでした" in line.replies[-1][1][0].text


@pytest.mark.anyio
async def test_other_intent_replies_directly(make_owner, router, line):
    owner = make_owner()
    router.routes["ありがとう"] = {"intent": "other", "reply": "どういたしまして。"}
    await owner.handle(owner_text_event("ありがとう", now_ms()))
    assert texts(line.replies[-1][1]) == ["どういたしまして。"]
    assert owner.agent.questions == []


# ---------------------------------------------------------------- 通知待ち
@pytest.mark.anyio
async def test_notices_are_delivered_with_reply_and_cleared(make_owner, line, github):
    github.put_text(NOTICES_FILE, add_notice(None, "quota:R1:202611140900", "R1 を送れませんでした（残り0通）", "2026-11-14 09:00"))
    owner = make_owner()
    await owner.handle(owner_text_event("質問", now_ms()))
    token, messages = line.replies[-1]
    assert messages[0].text.startswith("📢 お知らせ\n・R1 を送れませんでした")
    assert messages[1].text.startswith("10時集合です。")
    assert list_notices(github.text(NOTICES_FILE)) == []


@pytest.mark.anyio
async def test_notices_reply_first_when_answer_is_slow(make_owner, line, github):
    github.put_text(NOTICES_FILE, add_notice(None, "k", "通数が足りません", "2026-11-14 09:00"))
    owner = make_owner(reply_deadline_seconds=3)
    owner.agent = FakeAgent(delay=1.5)
    await owner.handle(owner_text_event("時間のかかる質問", now_ms()))
    # 期限内に通知を応答メッセージで送り、回答は後からプッシュする
    assert len(line.replies) == 1
    reply_texts = texts(line.replies[0][1])
    assert reply_texts[0].startswith("📢 お知らせ") and "まとめています" in reply_texts[1]
    assert len(line.pushes) == 1 and line.pushes[0][1][0].text.startswith("10時集合です。")
    assert list_notices(github.text(NOTICES_FILE)) == []


@pytest.mark.anyio
async def test_sticker_from_owner_delivers_notices_only(make_owner, line, github):
    from .helpers import base_event

    github.put_text(NOTICES_FILE, add_notice(None, "k", "お知らせです", "2026-11-14 09:00"))
    owner = make_owner()
    ev = base_event("message", now_ms(), source(kind="user", user_id=OWNER_ID), replyToken="rt",
                    message={"type": "sticker", "id": "9", "packageId": "1", "stickerId": "1"})
    assert await owner.handle(ev) == "notices_only"
    assert texts(line.replies[-1][1]) == ["📢 お知らせ\n・お知らせです"]
    ev2 = dict(ev, replyToken="rt2")
    assert await owner.handle(ev2) == "ignored"


def _line_is_down(line):
    """応答メッセージもプッシュも失敗する状態（例：返信の期限切れと、月の通数の上限）。"""
    from app.line_api import LineApiError

    line.reply_error = LineApiError("Invalid reply token", 400)
    line.push_error = LineApiError("You have reached your monthly limit.", 429)


@pytest.mark.anyio
async def test_notices_are_kept_when_delivery_fails(make_owner, line, github):
    from .helpers import base_event

    github.put_text(NOTICES_FILE, add_notice(None, "k", "R1 を送れませんでした（残り0通）", "2026-11-14 09:00"))
    owner = make_owner()
    _line_is_down(line)
    sticker = base_event("message", now_ms(), source(kind="user", user_id=OWNER_ID), replyToken="rt",
                         message={"type": "sticker", "id": "9", "packageId": "1", "stickerId": "1"})
    assert await owner.handle(sticker) == "notices_failed"
    await owner.handle(owner_text_event("質問", now_ms()))
    assert line.replies == [] and line.pushes == []
    assert len(list_notices(github.text(NOTICES_FILE))) == 1  # 届けられなかった通知は消さない
    # LINE 側が回復したら、次に話しかけたときに届けてから消す
    line.reply_error = line.push_error = None
    await owner.handle(owner_text_event("もう一度", now_ms(), msg_id="501"))
    assert line.replies[-1][1][0].text.startswith("📢 お知らせ\n・R1 を送れませんでした")
    assert list_notices(github.text(NOTICES_FILE)) == []


@pytest.mark.anyio
async def test_notices_are_kept_when_slow_answer_delivery_fails(make_owner, line, github):
    github.put_text(NOTICES_FILE, add_notice(None, "k", "通数が足りません", "2026-11-14 09:00"))
    owner = make_owner(reply_deadline_seconds=3)
    owner.agent = FakeAgent(delay=1.5)
    _line_is_down(line)
    await owner.handle(owner_text_event("時間のかかる質問", now_ms()))
    assert len(list_notices(github.text(NOTICES_FILE))) == 1


@pytest.mark.anyio
async def test_notices_delivered_by_push_fallback_are_cleared(make_owner, line, github):
    from app.line_api import LineApiError

    github.put_text(NOTICES_FILE, add_notice(None, "k", "通数が足りません", "2026-11-14 09:00"))
    owner = make_owner()
    line.reply_error = LineApiError("Invalid reply token", 400)
    await owner.handle(owner_text_event("質問", now_ms()))
    assert line.pushes[-1][1][0].text.startswith("📢 お知らせ\n・通数が足りません")
    assert list_notices(github.text(NOTICES_FILE)) == []
