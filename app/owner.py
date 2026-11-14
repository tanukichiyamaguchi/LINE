"""質問回答とオーナーからの指示の処理（/tasks/ask）。

* 対象は OWNER_LINE_USER_ID との1対1だけ。それ以外には返信しない。
* 受け付けたらローディング表示を出す（処理が長引けば表示を延長する）。
* 受信から50秒以内なら応答メッセージ、超えたらプッシュメッセージで返す。
* 「通知待ち」（リマインドを通数不足で送れなかった等）があれば、応答メッセージで知らせる。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
import uuid
from datetime import timedelta

from .agent import QuestionAgent
from .claude import ClaudeError, ClaudeService, RefusalError
from .config import Settings
from .github_store import GitHubStore
from .groups import Group, GroupRegistry, resolve_group
from .line_api import LineApiError, LineClient, OutMessage
from .paths import NOTICES_FILE, PENDING_SEND_FILE, REMINDERS_FILE
from .reminders import (
    Reminder,
    add_reminder,
    find,
    new_fields,
    parse_reminders,
    set_fields,
)
from .schedule import SUPPORTED_HELP, ScheduleError, parse_schedule
from .state import (
    clear_pending_if,
    get_pending,
    list_notices,
    remove_notices,
    set_pending,
)
from .tasks_client import TasksClient
from .timeutil import (
    fmt_date_with_weekday,
    fmt_minute,
    fmt_short,
    now_jst,
    parse_minute,
)
from .vault import VaultError, VaultMirror

log = logging.getLogger(__name__)

RETRY_KEY_NS = uuid.UUID("6f2b8d53-3c1e-4b8e-9a51-0d2f6c7e9a10")
REPLY_SAFETY_SECONDS = 2.0

INTENTS = ["question", "announce", "reminder_add", "reminder_update", "reminder_stop",
           "reminder_resume", "reminder_list", "other"]

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "group": {"type": "string"},
        "text": {"type": "string"},
        "schedule": {"type": "string"},
        "reminder_id": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["intent", "group", "text", "schedule", "reminder_id", "reply"],
    "additionalProperties": False,
}

ROUTER_SYSTEM = f"""あなたは、LINE公式アカウントのオーナーから1対1で届いたメッセージの意図を判定し、処理に必要な項目を取り出す担当です。
このボットにできること：(1) Obsidian Vault に記録したLINEのやり取りやノートを調べて質問に答える、(2) 登録済みのグループにお知らせを送る（送る前に確認する）、(3) グループへの定期リマインドの追加・変更・停止・再開・一覧表示。

intent の選び方：
- question：Vault の記録（LINEのやり取り、添付ファイル、ノート）を調べて答えるべき質問・調べもの。迷ったら question。
- announce：「○○グループに『…』と送って」のように、グループへのメッセージ送信の依頼。
- reminder_add：新しいリマインドの登録の依頼。
- reminder_update：既存のリマインドの送り先・スケジュール・本文の変更の依頼。
- reminder_stop：既存のリマインドの停止（無効化）の依頼。削除の依頼もこれに含める。
- reminder_resume：停止中のリマインドの再開（有効化）の依頼。
- reminder_list：リマインドの一覧表示の依頼。
- other：あいさつ・お礼・使い方の質問など、調べものも操作も要らないもの。

各項目（当てはまらない項目は空文字）：
- group：送り先のグループ。グループ一覧にある名前に合わせる（一覧に無ければ書かれたとおり）。
- text：announce では送る文面（『』や「」で示されていればその中身をそのまま。示されていなければ依頼の趣旨に沿った自然な文面）。reminder_add／reminder_update ではリマインドで送る文面（変更しない場合は空文字）。
- schedule：reminder_add／reminder_update のスケジュール。次の書式に変換する（変更しない場合は空文字）。{SUPPORTED_HELP}。「明日の9時」のような相対的な日時は、現在日時から計算して「YYYY-MM-DD HH:MM」にする。
- reminder_id：reminder_update／reminder_stop／reminder_resume の対象のID（例：R2）。リマインド一覧から特定できなければ空文字。
- reply：other のときだけ、オーナーへの短い返答（使い方を聞かれたら、できることを簡潔に説明）。"""


def _norm_reply(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").strip().lower()
    return s.strip(" 。、.,!！?？")


_YES = {"はい", "yes", "y", "ok", "おk", "送信", "送って", "送信して", "お願いします", "おねがいします", "うん", "承認", "送る", "了解"}
_NO = {"いいえ", "no", "n", "キャンセル", "やめる", "やめて", "中止", "送らない", "取り消し", "取消", "いや", "だめ", "ダメ"}


def is_yes(text: str) -> bool:
    s = _norm_reply(text)
    return s in _YES or bool(re.fullmatch(r"(はい|yes|ok)[、。!！,.\s].{0,12}", s))


def is_no(text: str) -> bool:
    s = _norm_reply(text)
    return s in _NO or bool(re.fullmatch(r"(いいえ|no)[、。!！,.\s].{0,12}", s))


def format_reminder_list(reminders: list[Reminder], now) -> str:
    if not reminders:
        return "登録されているリマインドはありません。"
    lines = [f"登録されているリマインド（{len(reminders)}件）"]
    for r in reminders:
        if not r.is_valid:
            lines.append(f"\n{r.rid}［書式エラー］{r.target or '（送り先なし）'}\n　{'／'.join(r.errors)}")
            continue
        state = "有効" if r.enabled else "無効"
        nxt = r.schedule.next_after(now) if (r.enabled and r.schedule) else None
        body = r.body.replace("\n", " ")
        body = body if len(body) <= 40 else body[:39] + "…"
        detail = f"　{r.schedule_text}"
        if nxt:
            detail += f" → 次回 {fmt_short(nxt)}"
        elif r.enabled and r.schedule and r.schedule.is_once:
            detail += "（送信済み／期限切れ）"
        last = f"　最終送信：{fmt_short(r.last_sent)}" if r.last_sent else ""
        lines.append(f"\n{r.rid}［{state}］{r.target}\n{detail}\n　「{body}」" + (f"\n{last}" if last else ""))
    return "\n".join(lines)


def describe_reminder(rid: str, target: str, schedule_text: str, body: str, nxt) -> str:
    out = f"ID：{rid}\n送り先：{target}\nスケジュール：{schedule_text}\n本文：\n{body}"
    if nxt:
        out += f"\n次回：{fmt_short(nxt)}"
    return out


class OwnerAssistant:
    def __init__(self, settings: Settings, store: GitHubStore, line: LineClient, groups: GroupRegistry,
                 claude: ClaudeService, vault: VaultMirror, tasks: TasksClient) -> None:
        self.settings = settings
        self.store = store
        self.line = line
        self.groups = groups
        self.claude = claude
        self.vault = vault
        self.tasks = tasks
        self.agent = QuestionAgent(claude, settings.claude_model_answer, settings.claude_effort_answer,
                                   settings.ask_time_budget_seconds)

    # ================================================================ 入口
    async def handle(self, event: dict) -> str:
        source = event.get("source") or {}
        owner = self.settings.owner_line_user_id
        if source.get("type") != "user" or source.get("userId") != owner:
            return "ignored"  # オーナーとの1対1以外には返信しない
        message = event.get("message") or {}
        reply_token = event.get("replyToken")
        deadline = float(event.get("timestamp", time.time() * 1000)) / 1000.0 + self.settings.reply_deadline_seconds

        notices_file = await self.store.get(NOTICES_FILE)
        notices = list_notices(notices_file.text if notices_file else None)

        if message.get("type") != "text":
            # 文字以外（スタンプなど）は回答の対象外。通知待ちがあれば知らせる
            if notices:
                await self._send([self._notice_message(notices)], reply_token, deadline)
                await self._clear_notices(notices)
                return "notices_only"
            return "ignored"

        await self.line.show_loading(owner, 60)
        keepalive = asyncio.create_task(self._keep_loading(owner))
        try:
            work = asyncio.create_task(self._process(message.get("text", "")))
            if notices:
                remaining = deadline - time.time() - REPLY_SAFETY_SECONDS
                try:
                    result = await asyncio.wait_for(asyncio.shield(work), timeout=max(0.0, remaining))
                    await self._send([self._notice_message(notices), *result], reply_token, deadline)
                except TimeoutError:
                    # 期限内に回答が間に合わない：通知は応答メッセージで先に送り、回答は後でプッシュする
                    await self._send([self._notice_message(notices),
                                      OutMessage("（ご依頼の内容をまとめています。できあがり次第お送りします）")],
                                     reply_token, deadline)
                    result = await work
                    await self._send(result, None, deadline)
                await self._clear_notices(notices)
            else:
                result = await work
                await self._send(result, reply_token, deadline)
        finally:
            keepalive.cancel()
        return "answered"

    async def _keep_loading(self, user_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(50)
                await self.line.show_loading(user_id, 60)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _notice_message(notices: list[dict]) -> OutMessage:
        return OutMessage("📢 お知らせ\n" + "\n".join(f"・{n['text']}" for n in notices))

    async def _clear_notices(self, notices: list[dict]) -> None:
        keys = {n.get("key") for n in notices}
        try:
            await self.store.update(NOTICES_FILE, lambda t: remove_notices(t, keys), message="通知待ち: 配信済みを削除")
        except Exception:
            log.exception("通知待ちの削除に失敗しました")

    async def _send(self, messages: list[OutMessage], reply_token: str | None, deadline: float) -> str:
        """受信から一定時間内なら応答メッセージ、超えていたらプッシュメッセージで返す。"""
        owner = self.settings.owner_line_user_id
        if reply_token and time.time() <= deadline - 1.0:
            try:
                await self.line.reply(reply_token, messages)
                return "reply"
            except LineApiError as exc:
                log.warning("応答メッセージを送れなかったためプッシュに切り替えます: %s", exc)
        try:
            await self.line.push(owner, messages)
            return "push"
        except LineApiError:
            log.exception("オーナーへのプッシュメッセージの送信に失敗しました")
            return "failed"

    # ================================================================ 振り分け
    async def _process(self, text: str) -> list[OutMessage]:
        try:
            return await self._process_inner(text)
        except Exception as exc:
            log.exception("オーナーのメッセージの処理に失敗しました")
            return [OutMessage(f"エラーが発生したため処理できませんでした（{type(exc).__name__}）。時間をおいてもう一度お試しください。")]

    async def _process_inner(self, text: str) -> list[OutMessage]:
        prefix: list[OutMessage] = []
        pending_file = await self.store.get(PENDING_SEND_FILE)
        pending = get_pending(pending_file.text if pending_file else None)
        if pending:
            expires = parse_minute(pending.get("expires"))
            expired = expires is not None and now_jst() > expires
            if is_yes(text):
                if expired:
                    await self._drop_pending(pending)
                    return [OutMessage(f"確認の期限（{self.settings.confirm_ttl_minutes}分）が過ぎたため送信しませんでした。もう一度指示してください。")]
                return await self._confirm_send(pending)
            if is_no(text):
                await self._drop_pending(pending)
                return [OutMessage("お知らせの送信を取り消しました。")]
            await self._drop_pending(pending)
            prefix.append(OutMessage("（確認待ちだったお知らせの送信は取り消しました）"))
        elif is_yes(text) or is_no(text):
            return [OutMessage("確認待ちのお知らせはありません。")]

        route = await self._route(text)
        intent = route.get("intent", "question")
        if intent == "announce":
            return prefix + await self._announce(route)
        if intent == "reminder_add":
            return prefix + await self._reminder_add(route)
        if intent in ("reminder_update", "reminder_stop", "reminder_resume"):
            return prefix + await self._reminder_change(intent, route)
        if intent == "reminder_list":
            return prefix + await self._reminder_list()
        if intent == "other" and route.get("reply"):
            return prefix + [OutMessage(route["reply"])]
        return prefix + await self._question(text)

    async def _route(self, text: str) -> dict:
        now = now_jst()
        groups = await self.groups.list(fresh=True)
        reminders_file = await self.store.get(REMINDERS_FILE)
        reminders = parse_reminders(reminders_file.text if reminders_file else None)
        group_lines = "\n".join(f"- {g.name}" for g in groups) or "（まだありません）"
        reminder_lines = "\n".join(
            f"- {r.rid}［{'有効' if r.enabled else '無効'}］送り先：{r.target}／スケジュール：{r.schedule_text}／本文：{r.body[:60]}"
            for r in reminders
        ) or "（まだありません）"
        content = (
            f"現在日時：{fmt_date_with_weekday(now)} {now:%H:%M}（日本時間）\n\n"
            f"グループ一覧：\n{group_lines}\n\nリマインド一覧：\n{reminder_lines}\n\n"
            f"オーナーからのメッセージ：\n<message>\n{text}\n</message>"
        )
        try:
            return await self.claude.json_call(
                model=self.settings.claude_model_answer, system=ROUTER_SYSTEM, content=content,
                schema=ROUTER_SCHEMA, effort=self.settings.claude_effort_router, max_tokens=8000,
            )
        except RefusalError:
            raise
        except ClaudeError:
            log.exception("意図の判定に失敗したため、質問として扱います")
            return {"intent": "question"}

    # ================================================================ 質問回答
    async def _question(self, text: str) -> list[OutMessage]:
        try:
            await self.vault.refresh()
        except VaultError:
            log.exception("Vault の最新化に失敗しました")
            return [OutMessage("Vault を読み込めなかったため回答できませんでした。時間をおいてもう一度お試しください。")]
        try:
            answer = await self.agent.answer(text, self.vault.tools)
        except RefusalError:
            return [OutMessage("この質問には安全上の理由でお答えできませんでした。")]
        except ClaudeError as exc:
            log.exception("回答の生成に失敗しました")
            return [OutMessage(f"回答の生成に失敗しました（{exc}）。時間をおいてもう一度お試しください。")]
        return [OutMessage(answer.format())]

    # ================================================================ お知らせ
    async def _pick_group(self, query: str) -> tuple[Group | None, str | None]:
        groups = await self.groups.list(fresh=True)
        if not groups:
            return None, ("送り先にできるグループがまだありません。ボットをグループに招待し、"
                          "グループで誰かが発言すると「ボット設定/グループ一覧.md」に登録されます。")
        matches = resolve_group(groups, query)
        names = "、".join(g.name for g in groups)
        if not query.strip():
            return None, f"送り先のグループを指定してください。登録済みのグループ：{names}"
        if not matches:
            return None, f"「{query}」というグループが見つかりません。登録済みのグループ：{names}"
        if len(matches) > 1:
            return None, f"「{query}」に当てはまるグループが複数あります：{'、'.join(g.name for g in matches)}。どれか1つを指定してください。"
        return matches[0], None

    async def _announce(self, route: dict) -> list[OutMessage]:
        group, error = await self._pick_group(route.get("group", ""))
        if error:
            return [OutMessage(error)]
        assert group is not None
        body = (route.get("text") or "").strip()
        if not body:
            return [OutMessage(f"「{group.name}」に送る文面を指定してください。例：{group.name}に『明日は10時集合です』と送って")]
        now = now_jst()
        pending = {
            "id": uuid.uuid4().hex,
            "group_id": group.group_id,
            "group_name": group.name,
            "text": body,
            "created": fmt_minute(now),
            "expires": fmt_minute(now + timedelta(minutes=self.settings.confirm_ttl_minutes)),
        }
        await self.store.update(PENDING_SEND_FILE, lambda _t: set_pending(pending), message="送信待ち: お知らせの確認")
        return [OutMessage(
            f"次の内容で送信しますか？\n\n送り先：{group.name}\n本文：\n{body}\n\n"
            f"「はい」で送信、「いいえ」で取り消します（{self.settings.confirm_ttl_minutes}分以内）。",
            quick_replies=("はい", "いいえ"),
        )]

    async def _drop_pending(self, pending: dict) -> None:
        await self.store.update(PENDING_SEND_FILE, lambda t: clear_pending_if(t, pending["id"]),
                                message="送信待ち: 取り消し")

    async def _confirm_send(self, pending: dict) -> list[OutMessage]:
        # 先に送信待ちを消す（同じお知らせを二重に送らないため）
        claimed = await self.store.update(PENDING_SEND_FILE, lambda t: clear_pending_if(t, pending["id"]),
                                          message="送信待ち: 送信")
        if not claimed.changed:
            return [OutMessage("このお知らせは既に処理済みです。")]
        group_id, name, body = pending["group_id"], pending.get("group_name", ""), pending["text"]
        try:
            ok, remaining, required = await self.line.quota_for_group(group_id)
        except LineApiError as exc:
            return [OutMessage(f"送信できる通数を確認できなかったため送信しませんでした（{exc}）。")]
        if not ok:
            return [OutMessage(f"今月の残り通数が足りないため、「{name}」へのお知らせを送信しませんでした（残り{remaining}通／必要{required}通）。")]
        try:
            ids = await self.line.push(group_id, [OutMessage(body)],
                                       retry_key=str(uuid.uuid5(RETRY_KEY_NS, f"announce|{pending['id']}")))
        except LineApiError as exc:
            return [OutMessage(f"「{name}」への送信に失敗しました（{exc}）。")]
        if ids:
            await self.tasks.log_bot_message({"type": "group", "groupId": group_id}, ids[0], body)
        return [OutMessage(f"「{name}」に送信しました。")]

    # ================================================================ リマインド
    async def _reminder_add(self, route: dict) -> list[OutMessage]:
        now = now_jst()
        try:
            schedule = parse_schedule(route.get("schedule", ""))
        except ScheduleError as exc:
            return [OutMessage(f"スケジュールを読み取れませんでした。{exc}")]
        if schedule.is_once and schedule.once_at is not None and schedule.once_at <= now:
            return [OutMessage(f"指定の日時（{schedule.canonical()}）は過ぎています。未来の日時を指定してください。")]
        group, error = await self._pick_group(route.get("group", ""))
        if error:
            return [OutMessage(error)]
        assert group is not None
        body = (route.get("text") or "").strip()
        if not body:
            return [OutMessage("リマインドで送る文面を指定してください。")]
        fields = new_fields(group.name, schedule, body, now)
        holder: dict = {}

        def mutate(text: str | None) -> str:
            new_text, rid = add_reminder(text, fields)
            holder["rid"] = rid
            return new_text

        await self.store.update(REMINDERS_FILE, mutate, message="リマインド: 追加")
        return [OutMessage("リマインドを登録しました。\n\n" + describe_reminder(
            holder["rid"], group.name, schedule.canonical(), body, schedule.next_after(now)))]

    async def _reminder_change(self, intent: str, route: dict) -> list[OutMessage]:
        now = now_jst()
        current = await self.store.get(REMINDERS_FILE)
        reminders = parse_reminders(current.text if current else None)
        rid = (route.get("reminder_id") or "").strip()
        target = find(reminders, rid) if rid else None
        if target is None:
            listing = format_reminder_list(reminders, now)
            return [OutMessage(f"対象のリマインドを特定できませんでした。IDで指定してください（例：R1を停止して）。\n\n{listing}")]
        updates: dict[str, str] = {}
        if intent == "reminder_stop":
            updates["状態"] = "無効"
        elif intent == "reminder_resume":
            updates["状態"] = "有効"
        else:
            if route.get("schedule"):
                try:
                    schedule = parse_schedule(route["schedule"])
                except ScheduleError as exc:
                    return [OutMessage(f"スケジュールを読み取れませんでした。{exc}")]
                if schedule.is_once and schedule.once_at is not None and schedule.once_at <= now:
                    return [OutMessage(f"指定の日時（{schedule.canonical()}）は過ぎています。")]
                updates["スケジュール"] = schedule.canonical()
            if route.get("group"):
                group, error = await self._pick_group(route["group"])
                if error:
                    return [OutMessage(error)]
                assert group is not None
                updates["送り先"] = group.name
            if (route.get("text") or "").strip():
                updates["本文"] = route["text"].strip()
            if not updates:
                return [OutMessage(f"{target.rid} をどう変更するか読み取れませんでした（送り先・スケジュール・本文のどれを変えるか教えてください）。")]
        updates["更新日時"] = fmt_minute(now)

        def mutate(text: str | None) -> str | None:
            if text is None or find(parse_reminders(text), target.rid) is None:
                return None
            return set_fields(text, target.rid, updates)

        result = await self.store.update(REMINDERS_FILE, mutate, message=f"リマインド: {target.rid} を変更")
        if not result.changed and result.text is not None and find(parse_reminders(result.text), target.rid) is None:
            return [OutMessage(f"{target.rid} が見つかりませんでした（ファイルが編集された可能性があります）。")]
        updated = find(parse_reminders(result.text), target.rid) if result.text else None
        if updated is None:
            return [OutMessage("変更しました。")]
        verb = {"reminder_stop": "停止しました", "reminder_resume": "再開しました"}.get(intent, "変更しました")
        nxt = updated.schedule.next_after(now) if (updated.schedule and updated.enabled) else None
        return [OutMessage(f"リマインド {updated.rid} を{verb}。\n\n" + describe_reminder(
            updated.rid, updated.target, updated.schedule_text, updated.body, nxt))]

    async def _reminder_list(self) -> list[OutMessage]:
        current = await self.store.get(REMINDERS_FILE)
        reminders = parse_reminders(current.text if current else None)
        return [OutMessage(format_reminder_list(reminders, now_jst()))]
