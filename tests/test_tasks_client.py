"""Cloud Tasks に積むタスクの中身のテスト（呼び出し先・OIDC トークン・重複したタスク名）。"""

import json

import pytest
from google.api_core import exceptions as gexc
from google.cloud import tasks_v2

from app.tasks_client import TasksClient, task_id_for


class FakeCloudTasks:
    def __init__(self):
        self.created = []
        self.names = set()

    @staticmethod
    def queue_path(project, location, queue):
        return tasks_v2.CloudTasksAsyncClient.queue_path(project, location, queue)

    @staticmethod
    def task_path(project, location, queue, task):
        return tasks_v2.CloudTasksAsyncClient.task_path(project, location, queue, task)

    async def create_task(self, parent, task):
        if task.get("name") in self.names:
            raise gexc.AlreadyExists("task exists")
        self.names.add(task.get("name"))
        self.created.append((parent, task))
        return task


@pytest.fixture
def client():
    fake = FakeCloudTasks()
    return fake, TasksClient("proj", "asia-northeast1", "https://bot.example.run.app/", "tasks@proj.iam.gserviceaccount.com",
                             "line-ingest", "line-ask", dispatch_deadline_seconds=99999, client=fake)


@pytest.mark.anyio
async def test_ingest_task_shape(client):
    fake, tc = client
    assert await tc.enqueue_ingest({"kind": "event", "event": {"type": "message", "x": "日本語"}}, task_id_for("ev1", "ingest"))
    parent, task = fake.created[0]
    assert parent == "projects/proj/locations/asia-northeast1/queues/line-ingest"
    req = task["http_request"]
    assert req["url"] == "https://bot.example.run.app/tasks/ingest"
    assert req["http_method"] == tasks_v2.HttpMethod.POST
    assert req["oidc_token"] == {"service_account_email": "tasks@proj.iam.gserviceaccount.com",
                                 "audience": "https://bot.example.run.app"}
    assert json.loads(req["body"].decode("utf-8"))["event"]["x"] == "日本語"
    assert task["dispatch_deadline"].seconds == 1800  # HTTP タスクの上限（30分）に丸める
    assert task["name"].startswith("projects/proj/locations/asia-northeast1/queues/line-ingest/tasks/")


@pytest.mark.anyio
async def test_duplicate_task_name_is_ignored(client):
    fake, tc = client
    tid = task_id_for("ev2", "ask")
    assert await tc.enqueue_ask({"event": {}}, tid) is True
    assert await tc.enqueue_ask({"event": {}}, tid) is False
    assert len(fake.created) == 1
    assert fake.created[0][1]["http_request"]["url"].endswith("/tasks/ask")


@pytest.mark.anyio
async def test_log_bot_message_goes_to_ingest_queue(client):
    fake, tc = client
    await tc.log_bot_message({"type": "group", "groupId": "Cxxx"}, "999", "お知らせ")
    parent, task = fake.created[0]
    assert parent.endswith("/queues/line-ingest")
    payload = json.loads(task["http_request"]["body"])
    assert payload["kind"] == "bot_message" and payload["messageId"] == "999" and payload["text"] == "お知らせ"


def test_task_id_is_stable_and_valid():
    a = task_id_for("01HXYZ", "ingest")
    assert a == task_id_for("01HXYZ", "ingest") != task_id_for("01HXYZ", "ask")
    assert len(a) == 48 and all(c in "0123456789abcdef" for c in a)
