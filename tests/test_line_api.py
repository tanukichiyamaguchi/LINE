"""LINE 公式SDK の呼び出し方のテスト（LINE の代わりにローカルの偽サーバーへ送り、要求の中身を確かめる）。"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.line_api import LineApiError, LineClient, OutMessage, fit_messages, split_text

from .conftest import GROUP_ID


class FakeLineServer:
    def __init__(self):
        self.requests = []
        self.push_status = 200
        self.quota = {"type": "limited", "value": 200}
        self.usage = 150

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.text()
        self.requests.append((request.method, request.path, dict(request.headers), json.loads(body) if body else None))
        p = request.path
        if p == "/v2/bot/message/reply":
            return web.json_response({"sentMessages": [{"id": "111", "quoteToken": "q"}]})
        if p == "/v2/bot/message/push":
            if self.push_status != 200:
                return web.json_response({"message": "error"}, status=self.push_status)
            return web.json_response({"sentMessages": [{"id": "222"}]})
        if p == "/v2/bot/chat/loading/start":
            return web.json_response({}, status=202)
        if p == "/v2/bot/message/quota":
            return web.json_response(self.quota)
        if p == "/v2/bot/message/quota/consumption":
            return web.json_response({"totalUsage": self.usage})
        if p == f"/v2/bot/group/{GROUP_ID}/summary":
            return web.json_response({"groupId": GROUP_ID, "groupName": "家族グループ"})
        if p == f"/v2/bot/group/{GROUP_ID}/members/count":
            return web.json_response({"count": 7})
        if p.startswith(f"/v2/bot/group/{GROUP_ID}/member/"):
            return web.json_response({"displayName": "山田", "userId": p.rsplit("/", 1)[1]})
        if p.startswith("/v2/bot/profile/"):
            return web.json_response({"displayName": "佐藤", "userId": p.rsplit("/", 1)[1]})
        if p == "/v2/bot/info":
            return web.json_response({"userId": "Ubot", "basicId": "@bot", "displayName": "記録ボット",
                                      "chatMode": "bot", "markAsReadMode": "auto"})
        return web.json_response({"message": "Not found"}, status=404)


@pytest.fixture
async def line_server():
    fake = FakeLineServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    client = LineClient("test-token")
    client.api.line_base_path = str(server.make_url("")).rstrip("/")
    yield fake, client
    await client.aclose()
    await server.close()


@pytest.mark.anyio
async def test_reply_with_quick_reply(line_server):
    fake, client = line_server
    ids = await client.reply("rtoken", [OutMessage("送りますか？", quick_replies=("はい", "いいえ"))])
    assert ids == ["111"]
    method, path, headers, body = fake.requests[-1]
    assert headers["Authorization"] == "Bearer test-token"
    assert body["replyToken"] == "rtoken"
    msg = body["messages"][0]
    assert msg["type"] == "text" and msg["text"] == "送りますか？"
    assert [i["action"] for i in msg["quickReply"]["items"]] == [
        {"type": "message", "label": "はい", "text": "はい"}, {"type": "message", "label": "いいえ", "text": "いいえ"}]


@pytest.mark.anyio
async def test_push_with_retry_key_and_conflict(line_server):
    fake, client = line_server
    assert await client.push(GROUP_ID, [OutMessage("お知らせ")], retry_key="123e4567-e89b-12d3-a456-426614174000") == ["222"]
    _m, _p, headers, body = fake.requests[-1]
    assert headers["X-Line-Retry-Key"] == "123e4567-e89b-12d3-a456-426614174000"
    assert body["to"] == GROUP_ID and body["messages"][0]["text"] == "お知らせ"
    # 同じリトライキーで受付済み（409）なら送信済みとして扱う
    fake.push_status = 409
    assert await client.push(GROUP_ID, [OutMessage("お知らせ")], retry_key="123e4567-e89b-12d3-a456-426614174000") is None
    fake.push_status = 429
    with pytest.raises(LineApiError) as exc:
        await client.push(GROUP_ID, [OutMessage("x")])
    assert exc.value.status == 429


@pytest.mark.anyio
async def test_loading_animation(line_server):
    fake, client = line_server
    await client.show_loading("Uowner", 90)
    _m, path, _h, body = fake.requests[-1]
    assert path == "/v2/bot/chat/loading/start"
    assert body == {"chatId": "Uowner", "loadingSeconds": 60}  # 上限60秒・5の倍数


@pytest.mark.anyio
async def test_quota(line_server):
    fake, client = line_server
    assert await client.remaining_quota() == 50
    assert await client.quota_for_group(GROUP_ID) == (True, 50, 7)
    fake.usage = 196
    assert await client.quota_for_group(GROUP_ID) == (False, 4, 7)
    fake.quota = {"type": "none"}
    assert await client.remaining_quota() is None
    assert await client.quota_for_group(GROUP_ID) == (True, None, None)


@pytest.mark.anyio
async def test_names_and_group_info_are_cached(line_server):
    fake, client = line_server
    src = {"type": "group", "groupId": GROUP_ID, "userId": "U1"}
    assert await client.display_name(src) == "山田"
    assert await client.display_name(src) == "山田"
    assert sum(1 for r in fake.requests if "/member/" in r[1]) == 1  # 2回目はキャッシュ
    assert await client.display_name({"type": "user", "userId": "U2"}) == "佐藤"
    assert await client.display_name({"type": "group", "groupId": GROUP_ID}) == "不明なユーザー"
    assert await client.group_name(GROUP_ID) == "家族グループ"
    assert await client.group_member_count(GROUP_ID) == 7
    assert await client.bot_name() == "記録ボット"


@pytest.mark.anyio
async def test_unknown_profile_falls_back(line_server):
    fake, client = line_server
    assert await client.group_name("C" + "0" * 32) is None
    name = await client.display_name({"type": "room", "roomId": "R" + "1" * 32, "userId": "Uabcd"})
    assert name == "不明なユーザー(abcd)"


@pytest.mark.anyio
async def test_network_errors_become_line_api_error():
    client = LineClient("test-token")
    client.api.line_base_path = "http://127.0.0.1:9"  # 誰も待ち受けていないポート
    try:
        with pytest.raises(LineApiError):
            await client.push("Ux", [OutMessage("x")])
        with pytest.raises(LineApiError):
            await client.reply("rt", [OutMessage("x")])
        with pytest.raises(LineApiError):
            await client.remaining_quota()
        await client.show_loading("Ux")  # ローディング表示の失敗は無視する
    finally:
        await client.aclose()


def test_split_and_fit_messages():
    long = "あ" * 12000
    parts = split_text(long)
    assert [len(p) for p in parts] == [5000, 5000, 2000]
    msgs = fit_messages([OutMessage("通知"), OutMessage("い" * 26000, quick_replies=("はい",))])
    assert len(msgs) == 5 and msgs[-1].text.startswith("（長すぎるため")
    assert fit_messages([OutMessage("x" * 6000, quick_replies=("はい",))])[-1].quick_replies == ("はい",)
