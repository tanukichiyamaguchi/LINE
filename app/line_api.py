"""LINE Messaging API（公式SDK）の呼び出しをまとめたモジュール。"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp
import httpx2
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    MessageAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.messaging.exceptions import ApiException

# 通信そのものの失敗（タイムアウト・接続エラー）
NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError)

log = logging.getLogger(__name__)

TEXT_LIMIT = 5000  # テキストメッセージ1件の最大文字数
MAX_MESSAGES = 5  # 1回の応答・プッシュで送れるメッセージ数


class LineApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic() + self.ttl, value)


@dataclass(frozen=True)
class OutMessage:
    """送信するテキストメッセージ。quick_replies を付けるとボタンが出る。"""

    text: str
    quick_replies: tuple[str, ...] = ()

    def to_sdk(self) -> TextMessage:
        qr = None
        if self.quick_replies:
            qr = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label=label[:20], text=label))
                for label in self.quick_replies
            ])
        return TextMessage(text=self.text[:TEXT_LIMIT], quick_reply=qr)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """長い文章を LINE の文字数上限以内に分割する（なるべく改行位置で切る）。"""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks or [""]


def fit_messages(messages: Sequence[OutMessage]) -> list[OutMessage]:
    """長文を分割したうえで最大5件に収める（収まらない分は最後の1件に省略表示）。"""
    out: list[OutMessage] = []
    for m in messages:
        parts = split_text(m.text)
        for i, part in enumerate(parts):
            out.append(OutMessage(part, m.quick_replies if i == len(parts) - 1 else ()))
    if len(out) > MAX_MESSAGES:
        out = out[: MAX_MESSAGES - 1] + [OutMessage("（長すぎるため、以降は省略しました）")]
    return out


class LineClient:
    def __init__(self, channel_access_token: str) -> None:
        self._api_client = AsyncApiClient(Configuration(access_token=channel_access_token))
        self.api = AsyncMessagingApi(self._api_client)
        self.blob = AsyncMessagingApiBlob(self._api_client)
        self._profiles = _TTLCache(6 * 3600)
        self._bot_name = _TTLCache(3600)

    async def aclose(self) -> None:
        await self._api_client.close()

    # ---------------------------------------------------------------- 表示名
    async def display_name(self, source: dict) -> str:
        """イベントの送信者の表示名（プロフィール取得APIの結果をキャッシュ）。"""
        user_id = source.get("userId")
        if not user_id:
            # グループでは LINE for iOS / Android 以外から送ると userId が含まれない
            return "不明なユーザー"
        kind = source.get("type")
        scope = source.get("groupId") or source.get("roomId") or ""
        key = (kind, scope, user_id)
        cached = self._profiles.get(key)
        if cached:
            return cached
        name = None
        try:
            if kind == "group":
                prof = await self.api.get_group_member_profile(source["groupId"], user_id)
                name = prof.display_name
            elif kind == "room":
                prof = await self.api.get_room_member_profile(source["roomId"], user_id)
                name = prof.display_name
            else:
                prof = await self.api.get_profile(user_id)
                name = prof.display_name
        except ApiException as exc:
            if exc.status and exc.status >= 500:
                raise LineApiError("プロフィールの取得に失敗しました", exc.status) from exc
            log.warning("プロフィールを取得できませんでした（HTTP %s）", exc.status)
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"プロフィールの取得に失敗しました（{type(exc).__name__}）") from exc
        if not name:
            name = f"不明なユーザー({user_id[-4:]})"
        self._profiles.set(key, name)
        return name

    async def bot_name(self) -> str:
        cached = self._bot_name.get("bot")
        if cached:
            return cached
        try:
            info = await self.api.get_bot_info()
        except (ApiException, *NETWORK_ERRORS) as exc:
            raise LineApiError("ボットの情報を取得できませんでした") from exc
        self._bot_name.set("bot", info.display_name)
        return info.display_name

    # ---------------------------------------------------------------- グループ
    async def group_name(self, group_id: str) -> str | None:
        """グループ概要APIでグループ名を取得する。取得できなければ None。"""
        try:
            summary = await self.api.get_group_summary(group_id)
            return summary.group_name
        except ApiException as exc:
            if exc.status and exc.status >= 500:
                raise LineApiError("グループ概要の取得に失敗しました", exc.status) from exc
            log.warning("グループ概要を取得できませんでした（HTTP %s）", exc.status)
            return None
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"グループ概要の取得に失敗しました（{type(exc).__name__}）") from exc

    async def group_member_count(self, group_id: str) -> int:
        """グループの人数（LINE公式アカウント自身は含まない）。"""
        try:
            res = await self.api.get_group_member_count(group_id)
            return int(res.count)
        except ApiException as exc:
            raise LineApiError("グループの人数を取得できませんでした", exc.status) from exc
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"グループの人数を取得できませんでした（{type(exc).__name__}）") from exc

    # ---------------------------------------------------------------- コンテンツ
    async def message_content(self, message: dict, max_bytes: int) -> bytes:
        """画像・ファイルの実体を取得する。"""
        provider = (message.get("contentProvider") or {}).get("type", "line")
        if provider == "external":
            url = message["contentProvider"].get("originalContentUrl")
            if not url:
                raise LineApiError("外部コンテンツのURLがありません")
            async with httpx2.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise LineApiError(f"外部コンテンツの取得に失敗しました（HTTP {resp.status_code}）", resp.status_code)
                    data = bytearray()
                    async for chunk in resp.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise LineApiError("ファイルサイズが上限を超えています")
                    return bytes(data)
        try:
            data = await self.blob.get_message_content(message["id"])
        except ApiException as exc:
            raise LineApiError(f"LINEからの取得に失敗しました（HTTP {exc.status}）", exc.status) from exc
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"LINEからの取得に失敗しました（{type(exc).__name__}）") from exc
        return bytes(data)

    # ---------------------------------------------------------------- 送信
    async def reply(self, reply_token: str, messages: Sequence[OutMessage]) -> list[str]:
        msgs = fit_messages(messages)
        try:
            res = await self.api.reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[m.to_sdk() for m in msgs])
            )
        except ApiException as exc:
            raise LineApiError(f"応答メッセージの送信に失敗しました（HTTP {exc.status}）", exc.status) from exc
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"応答メッセージの送信に失敗しました（{type(exc).__name__}）") from exc
        return [s.id for s in (res.sent_messages or [])]

    async def push(self, to: str, messages: Sequence[OutMessage], retry_key: str | None = None) -> list[str] | None:
        """プッシュメッセージを送る。

        retry_key を付けると、同じキーでの再送は LINE 側で重複として拒否される（HTTP 409）。
        その場合は既に送信済みとみなして None を返す。
        """
        msgs = fit_messages(messages)
        try:
            res = await self.api.push_message(
                PushMessageRequest(to=to, messages=[m.to_sdk() for m in msgs]),
                x_line_retry_key=retry_key,
            )
        except ApiException as exc:
            if exc.status == 409 and retry_key:
                log.info("同じリトライキーで送信済みのため、再送しませんでした")
                return None
            raise LineApiError(f"プッシュメッセージの送信に失敗しました（HTTP {exc.status}）", exc.status) from exc
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"プッシュメッセージの送信に失敗しました（{type(exc).__name__}）") from exc
        return [s.id for s in (res.sent_messages or [])]

    async def show_loading(self, user_id: str, seconds: int = 60) -> None:
        """1対1のトークにローディング表示を出す（5〜60秒、5の倍数）。"""
        seconds = max(5, min(60, (seconds // 5) * 5))
        try:
            await self.api.show_loading_animation(
                ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=seconds)
            )
        except ApiException as exc:
            log.warning("ローディング表示に失敗しました（HTTP %s）", exc.status)
        except NETWORK_ERRORS as exc:
            log.warning("ローディング表示に失敗しました（%s）", type(exc).__name__)

    async def remaining_quota(self) -> int | None:
        """当月の残り通数。上限が設定されていない（無制限）なら None。"""
        try:
            quota = await self.api.get_message_quota()
            quota_type = getattr(quota.type, "value", quota.type)
            if quota_type == "none" or quota.value is None:
                return None
            usage = await self.api.get_message_quota_consumption()
        except ApiException as exc:
            raise LineApiError(f"送信可能な通数の確認に失敗しました（HTTP {exc.status}）", exc.status) from exc
        except NETWORK_ERRORS as exc:
            raise LineApiError(f"送信可能な通数の確認に失敗しました（{type(exc).__name__}）") from exc
        return int(quota.value) - int(usage.total_usage)

    async def quota_for_group(self, group_id: str) -> tuple[bool, int | None, int | None]:
        """グループへのプッシュに必要な通数が当月の残りにあるか。

        グループ宛てのプッシュは、グループの人数分が通数としてカウントされるため、
        必要数 = グループの人数（LINE公式アカウント自身は含まない）とする。
        戻り値：(送ってよいか, 残り通数, 必要数)。上限なしのプランでは (True, None, None)。
        """
        remaining = await self.remaining_quota()
        if remaining is None:
            return True, None, None
        required = await self.group_member_count(group_id)
        return remaining >= required, remaining, required
