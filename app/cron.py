"""定期リマインドの送信（/cron/reminders、Cloud Scheduler から5分おき）。

同じ回を二重に送らないために、次の順で処理する。

1. 時刻が来た有効なリマインドを探す（schedule.due_occurrence）。
2. 当月の残り通数を確認する。足りなければ送らず、通知待ちに記録する
   （次にオーナーが1対1で話しかけたときに応答メッセージで知らせる）。
3. 送る前に「最終送信日時」を書き込んで、その回を確保する。GitHub の sha 付き
   更新なので、同時に動いた別の実行とは片方だけが成功する。
4. プッシュ送信する（同じ回には同じリトライキーを付けるので、LINE 側でも
   二重送信が防がれる）。送信に失敗したら「最終送信日時」を元に戻し、次の実行で
   もう一度試す。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from .config import Settings
from .github_store import GitHubStore
from .groups import GroupRegistry, resolve_group
from .line_api import LineApiError, LineClient, OutMessage
from .paths import NOTICES_FILE, REMINDERS_FILE
from .reminders import NOT_SENT, Reminder, find, parse_reminders, set_fields
from .schedule import due_occurrence
from .state import add_notice
from .tasks_client import TasksClient
from .timeutil import fmt_minute, fmt_short, now_jst

log = logging.getLogger(__name__)

RETRY_KEY_NS = uuid.UUID("0c7b54a6-3a3f-4d64-8f0e-6a4f7c2b9d11")


def retry_key(reminder_id: str, occurrence: datetime, group_id: str) -> str:
    return str(uuid.uuid5(RETRY_KEY_NS, f"{reminder_id}|{occurrence.isoformat()}|{group_id}"))


class ReminderRunner:
    def __init__(self, settings: Settings, store: GitHubStore, line: LineClient,
                 groups: GroupRegistry, tasks: TasksClient) -> None:
        self.settings = settings
        self.store = store
        self.line = line
        self.groups = groups
        self.tasks = tasks
        self.catchup = timedelta(minutes=settings.reminder_catchup_minutes)

    async def run(self, now: datetime | None = None) -> dict:
        now = now or now_jst()
        current = await self.store.get(REMINDERS_FILE)
        reminders = parse_reminders(current.text if current else None)
        results = []
        for r in reminders:
            if not r.enabled or not r.is_valid or r.schedule is None:
                continue
            occurrence = due_occurrence(r.schedule, now, r.last_sent, r.updated_at, self.catchup)
            if occurrence is None:
                continue
            try:
                status = await self._send_one(r, occurrence, now)
            except Exception:
                log.exception("リマインド %s の処理に失敗しました", r.rid)
                status = "error"
            results.append({"id": r.rid, "occurrence": occurrence.isoformat(), "status": status})
        return {"checked": len(reminders), "results": results}

    async def _notice(self, key: str, message: str, now: datetime) -> None:
        await self.store.update(
            NOTICES_FILE, lambda t: add_notice(t, key, message, fmt_minute(now)), message="通知待ち: 追加"
        )

    async def _send_one(self, r: Reminder, occurrence: datetime, now: datetime) -> str:
        occ_key = occurrence.strftime("%Y%m%d%H%M")
        when = fmt_short(occurrence)
        matches = resolve_group(await self.groups.list(fresh=True), r.target)
        if len(matches) != 1:
            await self._notice(f"target:{r.rid}:{occ_key}",
                               f"リマインド {r.rid} の送り先「{r.target}」が見つからない（または複数ある）ため、{when}の回を送信できませんでした。",
                               now)
            return "target_not_found"
        group = matches[0]

        try:
            ok, remaining, required = await self.line.quota_for_group(group.group_id)
        except LineApiError:
            log.exception("残り通数を確認できませんでした（%s）", r.rid)
            return "quota_check_failed"  # 次の実行でもう一度確認する
        if not ok:
            await self._notice(f"quota:{r.rid}:{occ_key}",
                               f"今月の残り通数が足りないため、リマインド {r.rid}（{group.name}）の{when}の回を送信しませんでした（残り{remaining}通／必要{required}通）。",
                               now)
            return "quota_insufficient"

        # --- この回を確保する（最終送信日時を書き込む）
        claim_value = fmt_minute(now)
        previous: dict[str, str] = {}

        def claim(text: str | None) -> str | None:
            cur = find(parse_reminders(text), r.rid)
            if cur is None or not cur.enabled or not cur.is_valid or cur.schedule is None:
                return None
            if due_occurrence(cur.schedule, now, cur.last_sent, cur.updated_at, self.catchup) != occurrence:
                return None  # 別の実行が先に送った／内容が変わった
            previous["value"] = cur.fields.get("最終送信日時", NOT_SENT) or NOT_SENT
            assert text is not None
            return set_fields(text, r.rid, {"最終送信日時": claim_value})

        claimed = await self.store.update(REMINDERS_FILE, claim, message=f"リマインド: {r.rid} 送信（{claim_value}）")
        if not claimed.changed:
            return "already_claimed"

        # --- 送信
        try:
            ids = await self.line.push(group.group_id, [OutMessage(r.body)],
                                       retry_key=retry_key(r.rid, occurrence, group.group_id))
        except LineApiError as exc:
            log.error("リマインド %s の送信に失敗しました: %s", r.rid, exc)
            await self._rollback(r.rid, claim_value, previous.get("value", NOT_SENT))
            await self._notice(f"sendfail:{r.rid}:{occ_key}",
                               f"リマインド {r.rid}（{group.name}）の{when}の回の送信に失敗しました（{exc}）。しばらく自動で再試行します。",
                               now)
            return "send_failed"

        if r.schedule is not None and r.schedule.is_once:
            # 1回のみのリマインドは送信後に無効にする
            await self.store.update(
                REMINDERS_FILE,
                lambda t: set_fields(t, r.rid, {"状態": "無効"}) if t and find(parse_reminders(t), r.rid) else None,
                message=f"リマインド: {r.rid} を完了",
            )
        if ids:
            await self.tasks.log_bot_message({"type": "group", "groupId": group.group_id}, ids[0], r.body)
        return "sent"

    async def _rollback(self, rid: str, claim_value: str, previous_value: str) -> None:
        def undo(text: str | None) -> str | None:
            cur = find(parse_reminders(text), rid)
            if cur is None or cur.fields.get("最終送信日時") != claim_value:
                return None
            assert text is not None
            return set_fields(text, rid, {"最終送信日時": previous_value})

        try:
            await self.store.update(REMINDERS_FILE, undo, message=f"リマインド: {rid} 送信失敗のため最終送信日時を戻す")
        except Exception:
            log.exception("リマインド %s の最終送信日時を戻せませんでした", rid)
