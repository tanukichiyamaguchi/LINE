"""/cron/reminders の送信処理のテスト（二重送信の防止・通数確認・失敗時の巻き戻し）。"""

import asyncio
from datetime import datetime

import pytest

from app.cron import ReminderRunner
from app.groups import GroupRegistry
from app.line_api import LineApiError
from app.paths import GROUPS_FILE, NOTICES_FILE, REMINDERS_FILE
from app.reminders import find, parse_reminders
from app.state import list_notices
from app.timeutil import JST

from .conftest import GROUP_ID

REMINDERS = """# リマインド

## R1
- 送り先: 家族グループ
- スケジュール: 毎日 09:00
- 本文: おはようございます
- 状態: 有効
- 最終送信日時: 2026-11-13 09:00
- 更新日時: 2026-11-01 12:00

## R2
- 送り先: 家族グループ
- スケジュール: 2026-11-14 09:00
- 本文: 1回だけのお知らせ
- 状態: 有効
- 最終送信日時: 未送信
- 更新日時: 2026-11-01 12:00

## R3
- 送り先: 家族グループ
- スケジュール: 毎日 09:00
- 本文: 停止中
- 状態: 無効
"""


@pytest.fixture
def runner(settings, store, github, line, tasks):
    github.put_text(GROUPS_FILE, f"# グループ一覧\n\n- 家族グループ：{GROUP_ID}\n")
    github.put_text(REMINDERS_FILE, REMINDERS)
    return ReminderRunner(settings, store, line, GroupRegistry(store, line), tasks)


def at(h, mi, d=14):
    return datetime(2026, 11, d, h, mi, tzinfo=JST)


@pytest.mark.anyio
async def test_sends_due_reminders_once(runner, github, line, tasks):
    result = await runner.run(at(9, 0))
    statuses = {r["id"]: r["status"] for r in result["results"]}
    assert statuses == {"R1": "sent", "R2": "sent"}
    assert [m[1][0].text for m in line.pushes] == ["おはようございます", "1回だけのお知らせ"]
    assert all(to == GROUP_ID for to, _m, _k in line.pushes)

    rs = parse_reminders(github.text(REMINDERS_FILE))
    assert find(rs, "R1").last_sent == at(9, 0)
    assert find(rs, "R2").enabled is False  # 1回のみは送信後に無効
    assert find(rs, "R3").last_sent is None  # 無効なものは送らない

    # 5分後の実行では同じ回を送らない
    result = await runner.run(at(9, 5))
    assert result["results"] == []
    assert len(line.pushes) == 2

    # 送ったメッセージは記録用キュー経由で日別ログに記録される
    logged = [p for q, _path, p, _id in tasks.enqueued if p.get("kind") == "bot_message"]
    assert [p["text"] for p in logged] == ["おはようございます", "1回だけのお知らせ"]
    assert logged[0]["chat"] == {"type": "group", "groupId": GROUP_ID}


@pytest.mark.anyio
async def test_retry_key_is_stable_per_occurrence(runner, line, github):
    await runner.run(at(9, 0))
    key_day1 = line.pushes[0][2]
    text = github.text(REMINDERS_FILE).replace("- 最終送信日時: 2026-11-14 09:00", "- 最終送信日時: 2026-11-13 09:00", 1)
    github.put_text(REMINDERS_FILE, text)
    await runner.run(at(9, 3))
    assert line.pushes[-1][2] == key_day1  # 同じ回なら同じリトライキー（LINE側でも二重送信を防ぐ）
    await runner.run(at(9, 0, d=15))
    assert line.pushes[-1][2] != key_day1


@pytest.mark.anyio
async def test_concurrent_runs_send_only_once(runner, line):
    await asyncio.gather(runner.run(at(9, 0)), runner.run(at(9, 1)))
    texts = [m[1][0].text for m in line.pushes]
    assert texts.count("おはようございます") == 1
    assert texts.count("1回だけのお知らせ") == 1


@pytest.mark.anyio
async def test_quota_insufficient_skips_and_notifies_owner_once(runner, github, line):
    line.quota_remaining = 3
    line.member_counts[GROUP_ID] = 5
    result = await runner.run(at(9, 0))
    assert {r["status"] for r in result["results"]} == {"quota_insufficient"}
    assert line.pushes == []
    assert find(parse_reminders(github.text(REMINDERS_FILE)), "R1").last_sent == at(9, 0, d=13)
    notices = list_notices(github.text(NOTICES_FILE))
    assert len(notices) == 2 and "残り3通／必要5通" in notices[0]["text"]
    await runner.run(at(9, 5))  # 同じ回の通知は重ねない
    assert len(list_notices(github.text(NOTICES_FILE))) == 2


@pytest.mark.anyio
async def test_unlimited_quota_sends(runner, line):
    line.quota_remaining = None
    await runner.run(at(9, 0))
    assert len(line.pushes) == 2


@pytest.mark.anyio
async def test_send_failure_rolls_back_and_retries_next_run(runner, github, line):
    line.push_error = LineApiError("プッシュメッセージの送信に失敗しました（HTTP 500）", 500)
    result = await runner.run(at(9, 0))
    assert {r["status"] for r in result["results"]} == {"send_failed"}
    rs = parse_reminders(github.text(REMINDERS_FILE))
    assert find(rs, "R1").last_sent == at(9, 0, d=13)  # 元に戻っている
    assert "- 最終送信日時: 未送信" in github.text(REMINDERS_FILE)
    line.push_error = None
    result = await runner.run(at(9, 5))
    assert {r["status"] for r in result["results"]} == {"sent"}


@pytest.mark.anyio
async def test_unknown_target_notifies(runner, github):
    github.put_text(REMINDERS_FILE, REMINDERS.replace("- 送り先: 家族グループ\n- スケジュール: 毎日 09:00\n- 本文: おはよう",
                                                      "- 送り先: 存在しないグループ\n- スケジュール: 毎日 09:00\n- 本文: おはよう"))
    result = await runner.run(at(9, 0))
    statuses = {r["id"]: r["status"] for r in result["results"]}
    assert statuses["R1"] == "target_not_found"
    assert "存在しないグループ" in list_notices(github.text(NOTICES_FILE))[0]["text"]


@pytest.mark.anyio
async def test_human_edit_of_last_sent_is_respected(runner, github, line):
    github.put_text(REMINDERS_FILE, REMINDERS.replace("- 最終送信日時: 2026-11-13 09:00", "- 最終送信日時: 2026-11-14 09:00"))
    result = await runner.run(at(9, 0))
    assert [r["id"] for r in result["results"]] == ["R2"]
