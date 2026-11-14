"""質問回答エージェントのテスト（Claude は台本どおりに応答する偽物、道具は本物の VaultTools）。"""

import json
from pathlib import Path

import pytest
from anthropic.types.beta import BetaMessage

from app.agent import TOOLS, QuestionAgent, build_answer
from app.claude import RefusalError
from app.vault import VaultTools


def msg(content, stop_reason):
    return BetaMessage.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None, "stop_details": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def tool_use(id_, name, inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


class ScriptedClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


@pytest.fixture
def tools(tmp_path: Path) -> VaultTools:
    p = tmp_path / "LINEログ/家族グループ/2026-11-13.md"
    p.parent.mkdir(parents=True)
    p.write_text("# 2026-11-13（金） 家族グループ\n\n- 09:00 山田：BBQは10時集合 ^m1\n", encoding="utf-8")
    return VaultTools(tmp_path)


@pytest.mark.anyio
async def test_agent_uses_tools_and_cites_existing_sources(tools):
    final = json.dumps({
        "found": True,
        "answer": "BBQは10時集合です。",
        "sources": [
            {"path": "LINEログ/家族グループ/2026-11-13.md", "date": "2026-11-13"},
            {"path": "LINEログ/家族グループ/2026-11-13", "date": ""},  # 拡張子なし（同じノート）
            {"path": "LINEログ/存在しない.md", "date": "2026-01-01"},  # 実在しない根拠は表示しない
        ],
    }, ensure_ascii=False)
    claude = ScriptedClaude([
        msg([{"type": "thinking", "thinking": "", "signature": "sig"},
             tool_use("t1", "search_vault", {"query": "BBQ", "path_prefix": "", "regex": False}),
             tool_use("t2", "list_files", {"path": "LINEログ", "recursive": True})], "tool_use"),
        msg([tool_use("t3", "read_file", {"path": "LINEログ/家族グループ/2026-11-13.md", "start_line": 1, "max_lines": 50})], "tool_use"),
        msg([{"type": "text", "text": final}], "end_turn"),
    ])
    agent = QuestionAgent(claude, "claude-opus-5-5", "medium", time_budget_seconds=300)
    answer = await agent.answer("BBQは何時集合？", tools)

    assert answer.found and answer.text == "BBQは10時集合です。"
    assert answer.sources == [("LINEログ/家族グループ/2026-11-13.md", "2026-11-13")]
    assert answer.unverified_sources == 1
    out = answer.format()
    assert out.endswith("📝 根拠\n・LINEログ/家族グループ/2026-11-13.md（2026-11-13）")

    first, second, third = claude.calls
    assert first["tools"] == TOOLS and first["cache"] is True
    assert first["output_format"]["type"] == "json_schema"
    assert "現在日時：" in first["messages"][0]["content"] and "BBQは何時集合？" in first["messages"][0]["content"]
    # 並列の道具呼び出しの結果は1つの user メッセージにまとめて返す
    results = second["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "BBQは10時集合" in results[0]["content"]
    # 応答（思考ブロックを含む）は変更せずに履歴へ追加している
    assert second["messages"][1]["content"][0].type == "thinking"
    assert "1: # 2026-11-13（金） 家族グループ" in third["messages"][4]["content"][0]["content"]


@pytest.mark.anyio
async def test_not_found_answer(tools):
    final = json.dumps({"found": False, "answer": "見つかりませんでした。「旅行」「宿」で検索しました。", "sources": []}, ensure_ascii=False)
    claude = ScriptedClaude([msg([{"type": "text", "text": final}], "end_turn")])
    answer = await QuestionAgent(claude, "m", None, 300).answer("旅行の宿は？", tools)
    assert not answer.found
    assert answer.format() == "見つかりませんでした。「旅行」「宿」で検索しました。"


@pytest.mark.anyio
async def test_found_without_valid_sources_adds_caution(tools):
    final = json.dumps({"found": True, "answer": "たぶん10時", "sources": [{"path": "なし.md", "date": ""}]}, ensure_ascii=False)
    claude = ScriptedClaude([msg([{"type": "text", "text": final}], "end_turn")])
    answer = await QuestionAgent(claude, "m", None, 300).answer("?", tools)
    assert "根拠にしたノートを確認できませんでした" in answer.format()


@pytest.mark.anyio
async def test_refusal_is_raised(tools):
    claude = ScriptedClaude([msg([], "refusal")])
    with pytest.raises(RefusalError):
        await QuestionAgent(claude, "m", None, 300).answer("?", tools)


@pytest.mark.anyio
async def test_time_budget_asks_to_wrap_up(tools):
    final = json.dumps({"found": True, "answer": "10時", "sources": []}, ensure_ascii=False)
    claude = ScriptedClaude([
        msg([tool_use("t1", "search_vault", {"query": "BBQ", "path_prefix": "", "regex": False})], "tool_use"),
        msg([{"type": "text", "text": final}], "end_turn"),
    ])
    await QuestionAgent(claude, "m", None, time_budget_seconds=0).answer("?", tools)
    last_user = claude.calls[1]["messages"][2]["content"]
    assert last_user[-1]["type"] == "text" and "回答してください" in last_user[-1]["text"]


def test_build_answer_takes_date_from_filename(tools):
    a = build_answer(tools, {"found": True, "answer": "x", "sources": [{"path": "LINEログ/家族グループ/2026-11-13.md", "date": "不明"}]})
    assert a.sources == [("LINEログ/家族グループ/2026-11-13.md", "2026-11-13")]
