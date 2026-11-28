"""Anthropic Claude API の呼び出しをまとめたモジュール。

* モデル名は環境変数で指定（文字起こし用・回答用を分ける）。
* 安全分類器による応答拒否（stop_reason == "refusal"）に備え、サーバー側の
  フォールバック（fallbacks: "default"）を既定で有効にしている。
  （CLAUDE_FALLBACKS=off で無効化。フォールバック非対応のモデルで 400 が返った
  場合は、自動的にフォールバック無しで再試行する）
* 思考（thinking）は指定しない＝各モデルの既定（Claude Opus 5.5・Claude Sonnet 5.5 では
  adaptive thinking）。深さは effort で調整する。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(Exception):
    """Claude API の呼び出しに失敗した（基本的に一時的なエラー）。"""


class RefusalError(ClaudeError):
    """安全上の理由で応答が拒否された。"""

    def __init__(self, category: str | None) -> None:
        super().__init__(f"Claude が応答を拒否しました（{category or '理由不明'}）")
        self.category = category


class MaxTokensError(ClaudeError):
    """出力が max_tokens に達して途中で切れた。"""


def text_after_fallback(message: BetaMessage) -> str:
    """応答の本文テキスト。フォールバックが起きた場合は、最後の切り替え以降の部分だけを使う。"""
    blocks = list(message.content)
    last_fallback = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    return "".join(b.text for b in blocks[last_fallback + 1:] if b.type == "text")


def check_refusal(message: BetaMessage) -> None:
    if message.stop_reason == "refusal":
        category = getattr(message.stop_details, "category", None) if message.stop_details else None
        raise RefusalError(category)


class ClaudeService:
    def __init__(self, api_key: str, use_fallbacks: bool = True, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            max_retries=3,
            timeout=anthropic.Timeout(900.0, connect=10.0),
        )
        self._use_fallbacks = use_fallbacks

    async def aclose(self) -> None:
        await self.client.close()

    def _kwargs(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str | list[dict] | None,
        messages: list[dict],
        effort: str | None,
        tools: list[dict] | None,
        output_format: dict | None,
        cache: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if output_format:
            output_config["format"] = output_format
        if output_config:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools
        if cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if self._use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _maybe_disable_fallbacks(self, exc: anthropic.BadRequestError) -> bool:
        if self._use_fallbacks and "fallback" in str(exc).lower():
            log.warning("このモデルではサーバー側フォールバックが使えないため、無効にして再試行します")
            self._use_fallbacks = False
            return True
        return False

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | list[dict] | None = None,
        max_tokens: int = 16000,
        effort: str | None = None,
        tools: list[dict] | None = None,
        output_format: dict | None = None,
        cache: bool = False,
    ) -> BetaMessage:
        """非ストリーミングで1回呼び出す（max_tokens が 16000 程度までの用途）。"""
        for _ in range(2):
            kwargs = self._kwargs(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                  effort=effort, tools=tools, output_format=output_format, cache=cache)
            try:
                return await self.client.beta.messages.create(**kwargs)
            except anthropic.BadRequestError as exc:
                if self._maybe_disable_fallbacks(exc):
                    continue
                raise ClaudeError(f"Claude API へのリクエストが不正です: {exc.message}") from exc
            except anthropic.APIStatusError as exc:
                raise ClaudeError(f"Claude API がエラーを返しました（HTTP {exc.status_code}）") from exc
            except anthropic.APIConnectionError as exc:
                raise ClaudeError("Claude API に接続できませんでした") from exc
        raise ClaudeError("Claude API の呼び出しに失敗しました")  # pragma: no cover

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | list[dict] | None = None,
        max_tokens: int = 64000,
        effort: str | None = None,
        output_format: dict | None = None,
    ) -> BetaMessage:
        """ストリーミングで呼び出し、最終メッセージを返す（長い出力向け）。"""
        for _ in range(2):
            kwargs = self._kwargs(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                  effort=effort, tools=None, output_format=output_format, cache=False)
            try:
                async with self.client.beta.messages.stream(**kwargs) as stream:
                    return await stream.get_final_message()
            except anthropic.BadRequestError as exc:
                if self._maybe_disable_fallbacks(exc):
                    continue
                raise ClaudeError(f"Claude API へのリクエストが不正です: {exc.message}") from exc
            except anthropic.APIStatusError as exc:
                raise ClaudeError(f"Claude API がエラーを返しました（HTTP {exc.status_code}）") from exc
            except anthropic.APIConnectionError as exc:
                raise ClaudeError("Claude API に接続できませんでした") from exc
        raise ClaudeError("Claude API の呼び出しに失敗しました")  # pragma: no cover

    async def json_call(
        self,
        *,
        model: str,
        system: str,
        content: str | list[dict],
        schema: dict,
        effort: str | None = None,
        max_tokens: int = 16000,
        streaming: bool = False,
    ) -> dict:
        """構造化出力（JSON スキーマ）で1回呼び出し、辞書で返す。"""
        messages = [{"role": "user", "content": content}]
        output_format = {"type": "json_schema", "schema": schema}
        if streaming:
            msg = await self.stream(model=model, messages=messages, system=system,
                                    max_tokens=max_tokens, effort=effort, output_format=output_format)
        else:
            msg = await self.create(model=model, messages=messages, system=system,
                                    max_tokens=max_tokens, effort=effort, output_format=output_format)
        check_refusal(msg)
        if msg.stop_reason == "max_tokens":
            raise MaxTokensError("出力が長すぎて途中で切れました")
        text = text_after_fallback(msg)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClaudeError("Claude の出力を JSON として読めませんでした") from exc
        if not isinstance(data, dict):
            raise ClaudeError("Claude の出力が想定した形式ではありません")
        return data
