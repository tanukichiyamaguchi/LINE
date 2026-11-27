"""Cloud Tasks へのタスク登録。"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from google.api_core import exceptions as gexc
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

log = logging.getLogger(__name__)


def task_id_for(*parts: str) -> str:
    """同じイベントから同じタスク名を作る（再送されたイベントの重複登録を防ぐ）。
    Cloud Tasks の推奨どおり、連番ではなくハッシュ値を使う。"""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:48]


class TasksClient:
    def __init__(
        self,
        project: str,
        location: str,
        service_url: str,
        invoker_sa: str,
        ingest_queue: str,
        ask_queue: str,
        dispatch_deadline_seconds: int = 1800,
        client: tasks_v2.CloudTasksAsyncClient | None = None,
    ) -> None:
        self.project = project
        self.location = location
        self.service_url = service_url.rstrip("/")
        self.invoker_sa = invoker_sa
        self.ingest_queue = ingest_queue
        self.ask_queue = ask_queue
        self.dispatch_deadline = min(max(dispatch_deadline_seconds, 15), 1800)  # HTTPタスクは15秒〜30分
        self._client = client

    @property
    def client(self) -> tasks_v2.CloudTasksAsyncClient:
        if self._client is None:
            self._client = tasks_v2.CloudTasksAsyncClient()
        return self._client

    async def enqueue(self, queue: str, path: str, payload: dict, task_id: str | None = None) -> bool:
        """タスクを登録する。同じ task_id のタスクが既にあれば登録しない（False を返す）。"""
        parent = self.client.queue_path(self.project, self.location, queue)
        task: dict = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self.service_url}{path}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                # /tasks を呼び出せるのは、このサービスアカウントの OIDC トークンを持つ Cloud Tasks だけ
                "oidc_token": {"service_account_email": self.invoker_sa, "audience": self.service_url},
            },
            "dispatch_deadline": duration_pb2.Duration(seconds=self.dispatch_deadline),
        }
        if task_id:
            task["name"] = self.client.task_path(self.project, self.location, queue, task_id)
        try:
            await self.client.create_task(parent=parent, task=task)
            return True
        except gexc.AlreadyExists:
            log.info("同じタスクが既に登録されています（再送されたイベント）: %s", task_id)
            return False

    async def enqueue_ingest(self, payload: dict, task_id: str | None = None) -> bool:
        return await self.enqueue(self.ingest_queue, "/tasks/ingest", payload, task_id)

    async def enqueue_ask(self, payload: dict, task_id: str | None = None) -> bool:
        return await self.enqueue(self.ask_queue, "/tasks/ask", payload, task_id)

    async def log_bot_message(self, chat: dict, message_id: str, text: str) -> None:
        """ボット自身が送ったお知らせ・リマインドを、記録用キュー経由で日別ログに記録する。"""
        payload = {
            "kind": "bot_message",
            "chat": chat,
            "messageId": message_id,
            "text": text,
            "timestamp": int(time.time() * 1000),
        }
        try:
            await self.enqueue_ingest(payload, task_id_for("bot", message_id))
        except Exception:
            log.exception("ボットの送信メッセージを記録用キューに登録できませんでした（m%s）", message_id)
