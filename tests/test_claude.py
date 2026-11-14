"""Claude API 呼び出しのテスト（実際の API には接続しない。httpx2.MockTransport で応答を作る）。"""

import json

import anthropic
import httpx2
import pytest

from app.claude import FALLBACK_BETA, ClaudeError, ClaudeService, MaxTokensError, RefusalError


def response(content, stop_reason="end_turn", stop_details=None):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "stop_details": stop_details, "usage": {"input_tokens": 1, "output_tokens": 1}}


def make_service(handler, use_fallbacks=True):
    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return ClaudeService("test", use_fallbacks=use_fallbacks, client=client)


@pytest.mark.anyio
async def test_request_shape_with_fallbacks_effort_and_schema():
    seen = []

    def handler(request):
        seen.append((request.headers, json.loads(request.content)))
        return httpx2.Response(200, json=response([{"type": "text", "text": '{"a": "x"}'}]))

    svc = make_service(handler)
    data = await svc.json_call(model="claude-opus-5-5", system="sys", content="hi",
                               schema={"type": "object", "properties": {"a": {"type": "string"}},
                                       "required": ["a"], "additionalProperties": False},
                               effort="low")
    assert data == {"a": "x"}
    headers, body = seen[0]
    assert headers["anthropic-beta"] == FALLBACK_BETA
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body  # 思考はモデルの既定（Opus 5.5 は常に adaptive）
    assert "temperature" not in body


@pytest.mark.anyio
async def test_fallbacks_are_disabled_when_model_rejects_them():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                         "message": "fallbacks is not supported for this model"}})
        return httpx2.Response(200, json=response([{"type": "text", "text": "ok"}]))

    svc = make_service(handler)
    msg = await svc.create(model="claude-haiku-5-5", messages=[{"role": "user", "content": "x"}])
    assert msg.content[0].text == "ok"
    assert "fallbacks" in bodies[0] and "fallbacks" not in bodies[1]
    await svc.create(model="claude-haiku-5-5", messages=[{"role": "user", "content": "x"}])
    assert "fallbacks" not in bodies[2]  # 以降は付けない


@pytest.mark.anyio
async def test_refusal_is_raised():
    def handler(request):
        return httpx2.Response(200, json=response([], stop_reason="refusal",
                                                  stop_details={"type": "refusal", "category": "cyber", "explanation": None}))

    svc = make_service(handler)
    with pytest.raises(RefusalError) as exc:
        await svc.json_call(model="m", system="s", content="c", schema={"type": "object"})
    assert exc.value.category == "cyber"


@pytest.mark.anyio
async def test_max_tokens_is_reported():
    def handler(request):
        return httpx2.Response(200, json=response([{"type": "text", "text": '{"a": "trunc'}], stop_reason="max_tokens"))

    with pytest.raises(MaxTokensError):
        await make_service(handler).json_call(model="m", system="s", content="c", schema={"type": "object"})


@pytest.mark.anyio
async def test_server_error_becomes_claude_error():
    def handler(request):
        return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    with pytest.raises(ClaudeError):
        await make_service(handler).create(model="m", messages=[{"role": "user", "content": "x"}])


@pytest.mark.anyio
async def test_fallback_disabled_by_setting():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=response([{"type": "text", "text": "ok"}]))

    await make_service(handler, use_fallbacks=False).create(model="m", messages=[{"role": "user", "content": "x"}])
    assert "fallbacks" not in bodies[0]
