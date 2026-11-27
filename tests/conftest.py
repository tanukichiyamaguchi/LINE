"""テスト共通の偽物（外部サービスの代わり）とフィクスチャ。

外部サービス（LINE・GitHub・Claude・Cloud Tasks）には一切接続しない。
GitHub は Contents API を真似た偽サーバー（httpx2.MockTransport）で置き換え、
本物の GitHubStore のコード（競合時の再試行など）をそのまま通す。
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import unquote

import httpx2
import pytest

from app.config import Settings
from app.github_store import GitHubStore
from app.line_api import LineApiError, LineClient
from app.tasks_client import TasksClient
from app.transcribe import ImageText

REPO = "owner/vault"
OWNER_ID = "U" + "a" * 32
GROUP_ID = "C" + "1" * 32


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(**overrides) -> Settings:
    values = dict(
        line_channel_secret="test-channel-secret",
        line_channel_access_token="test-access-token",
        owner_line_user_id=OWNER_ID,
        anthropic_api_key="test-anthropic-key",
        github_token="test-github-token",
        vault_repo=REPO,
        vault_branch=None,
        gcp_project_id="test-project",
        tasks_location="asia-northeast1",
        ingest_queue="line-ingest",
        ask_queue="line-ask",
        service_url="https://bot.example.run.app",
        tasks_invoker_sa="tasks@test-project.iam.gserviceaccount.com",
        scheduler_invoker_sa="scheduler@test-project.iam.gserviceaccount.com",
        claude_model_transcribe="claude-opus-5-5",
        claude_model_answer="claude-opus-5-5",
        claude_effort_transcribe="medium",
        claude_effort_answer="medium",
        claude_effort_router="low",
        claude_fallbacks=True,
        vault_dir="/tmp/vault-test",
        pdf_pages_per_chunk=20,
        max_attachment_bytes=50 * 1024 * 1024,
        max_extract_chars=1_000_000,
        reminder_catchup_minutes=30,
        reply_deadline_seconds=50,
        ask_time_budget_seconds=300,
        confirm_ttl_minutes=30,
        task_dispatch_deadline_seconds=1800,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# ==================================================================== GitHub（偽サーバー）
class FakeGitHub:
    """GitHub の Contents API を最小限まねた偽サーバー。"""

    def __init__(self, repo: str = REPO) -> None:
        self.repo = repo
        self.files: dict[str, bytes] = {}
        self.forced_put_status: list[int] = []  # 次の PUT で返すステータス（競合の再現用）
        self.writes: list[tuple[str, str]] = []  # (メソッド, パス)
        self.reads: list[str] = []  # GET したファイルのパス（読み込み回数の確認用）

    @staticmethod
    def sha(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def text(self, path: str) -> str | None:
        data = self.files.get(path)
        return data.decode("utf-8") if data is not None else None

    def put_text(self, path: str, text: str) -> None:
        self.files[path] = text.encode("utf-8")

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        raw = request.url.raw_path.decode("ascii").split("?", 1)[0]
        path = unquote(raw)
        contents = f"/repos/{self.repo}/contents/"
        blobs = f"/repos/{self.repo}/git/blobs/"
        if path.startswith(blobs):
            sha = path[len(blobs):]
            for data in self.files.values():
                if self.sha(data) == sha:
                    return httpx2.Response(200, json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
            return httpx2.Response(404, json={"message": "Not Found"})
        if not path.startswith(contents):
            return httpx2.Response(404, json={"message": "Not Found"})
        fpath = path[len(contents):]
        if request.method == "GET":
            self.reads.append(fpath)
            data = self.files.get(fpath)
            if data is None:
                return httpx2.Response(404, json={"message": "Not Found"})
            big = len(data) > 1_000_000
            return httpx2.Response(200, json={
                "type": "file", "path": fpath, "sha": self.sha(data), "size": len(data),
                "encoding": "none" if big else "base64",
                "content": "" if big else base64.b64encode(data).decode(),
            })
        body = json.loads(request.content or b"{}")
        current = self.files.get(fpath)
        if request.method == "PUT":
            if self.forced_put_status:
                status = self.forced_put_status.pop(0)
                return httpx2.Response(status, json={"message": "forced"})
            if current is not None and "sha" not in body:
                return httpx2.Response(422, json={"message": 'Invalid request.\n\n"sha" wasn\'t supplied.'})
            if current is not None and body["sha"] != self.sha(current):
                return httpx2.Response(409, json={"message": f"{fpath} does not match"})
            if current is None and body.get("sha"):
                return httpx2.Response(409, json={"message": "file was deleted"})
            data = base64.b64decode(body["content"])
            self.files[fpath] = data
            self.writes.append(("PUT", fpath))
            return httpx2.Response(201 if current is None else 200, json={"content": {"sha": self.sha(data), "path": fpath}})
        if request.method == "DELETE":
            if current is None:
                return httpx2.Response(404, json={"message": "Not Found"})
            if body.get("sha") != self.sha(current):
                return httpx2.Response(409, json={"message": "sha does not match"})
            del self.files[fpath]
            self.writes.append(("DELETE", fpath))
            return httpx2.Response(200, json={"commit": {}})
        return httpx2.Response(405)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def store(github: FakeGitHub) -> GitHubStore:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(github.handler))
    return GitHubStore("test-github-token", REPO, client=client, sleep=_no_sleep)


# ==================================================================== LINE（偽物）
class FakeLine:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.group_names: dict[str, str] = {}
        self.member_counts: dict[str, int] = {}
        self.contents: dict[str, bytes] = {}
        self.quota_remaining: int | None = None
        self.replies: list[tuple[str, list]] = []
        self.pushes: list[tuple[str, list, str | None]] = []
        self.loading: list[str] = []
        self.push_error: Exception | None = None
        self.reply_error: Exception | None = None
        self._next_id = 700000000000000000

    def _ids(self, n: int) -> list[str]:
        ids = [str(self._next_id + i) for i in range(n)]
        self._next_id += n
        return ids

    async def display_name(self, source: dict) -> str:
        uid = source.get("userId")
        return self.names.get(uid, "不明なユーザー") if uid else "不明なユーザー"

    async def bot_name(self) -> str:
        return "記録ボット"

    async def group_name(self, group_id: str) -> str | None:
        return self.group_names.get(group_id)

    async def group_member_count(self, group_id: str) -> int:
        return self.member_counts.get(group_id, 5)

    async def message_content(self, message: dict, max_bytes: int) -> bytes:
        if message["id"] not in self.contents:
            raise LineApiError("LINEからの取得に失敗しました（HTTP 404）", 404)
        return self.contents[message["id"]]

    async def reply(self, reply_token: str, messages) -> list[str]:
        if self.reply_error:
            raise self.reply_error
        self.replies.append((reply_token, list(messages)))
        return self._ids(len(messages))

    async def push(self, to: str, messages, retry_key: str | None = None):
        if self.push_error:
            raise self.push_error
        self.pushes.append((to, list(messages), retry_key))
        return self._ids(len(messages))

    async def show_loading(self, user_id: str, seconds: int = 60) -> None:
        self.loading.append(user_id)

    async def remaining_quota(self) -> int | None:
        return self.quota_remaining

    quota_for_group = LineClient.quota_for_group


@pytest.fixture
def line() -> FakeLine:
    return FakeLine()


# ==================================================================== Cloud Tasks（偽物）
class FakeTasks(TasksClient):
    def __init__(self) -> None:
        super().__init__("test-project", "asia-northeast1", "https://bot.example.run.app",
                         "tasks@test-project.iam.gserviceaccount.com", "line-ingest", "line-ask")
        self.enqueued: list[tuple[str, str, dict, str | None]] = []
        self._names: set[tuple[str, str]] = set()
        self.fail = False

    async def enqueue(self, queue: str, path: str, payload: dict, task_id: str | None = None) -> bool:
        if self.fail:
            raise RuntimeError("Cloud Tasks unavailable")
        if task_id and (queue, task_id) in self._names:
            return False
        if task_id:
            self._names.add((queue, task_id))
        self.enqueued.append((queue, path, payload, task_id))
        return True


@pytest.fixture
def tasks() -> FakeTasks:
    return FakeTasks()


# ==================================================================== 文字起こし（偽物）
class FakeTranscriber:
    def __init__(self) -> None:
        self.image_calls = 0
        self.pdf_calls = 0
        self.summary_calls = 0
        self.fail_with: Exception | None = None

    async def image(self, data: bytes) -> ImageText:
        self.image_calls += 1
        if self.fail_with:
            raise self.fail_with
        return ImageText(transcription="会議 10時から", description="ホワイトボードの写真", summary=["会議の予定のメモ", "10時開始"])

    async def pdf(self, data: bytes, filename: str) -> str:
        self.pdf_calls += 1
        if self.fail_with:
            raise self.fail_with
        return "--- p.1 ---\n見積書\n合計 10,000円"

    async def summarize(self, text: str, filename: str, kind_label: str) -> list[str]:
        self.summary_calls += 1
        return [f"{filename} の要約", "合計金額の記載あり"]


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()
