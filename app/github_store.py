"""GitHub の Contents API で Vault リポジトリのファイルを読み書きする。

* 書き込みは「取得 → 変更 → sha 付きで保存」。他の書き込みと競合して 409
  （または sha 不一致の 422）になったら、取得し直して再試行する。
* ボットが書き換えてよいのは LINEログ/ と ボット設定/ の中だけ。それ以外への
  書き込み・削除は、このクラスが必ず拒否する。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx2

from .paths import is_writable_path

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """GitHub API のエラー（基本的に Cloud Tasks の再試行に任せる）。"""


class ConflictError(GitHubError):
    """sha の不一致（他の書き込みと競合した）。"""


class ForbiddenPathError(GitHubError):
    """LINEログ/ と ボット設定/ 以外への書き込みをしようとした。"""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    text: str
    sha: str


@dataclass(frozen=True)
class UpdateResult:
    changed: bool
    text: str | None
    sha: str | None


Mutator = Callable[[str | None], "str | None | Awaitable[str | None]"]


class GitHubStore:
    def __init__(
        self,
        token: str,
        repo: str,
        branch: str | None = None,
        client: httpx2.AsyncClient | None = None,
        max_conflict_retries: int = 8,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._repo = repo
        self._branch = branch
        self._max_conflict_retries = max_conflict_retries
        self._sleep = sleep
        self._client = client or httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=10.0))
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.object+json",
            "User-Agent": "line-vault-bot",
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ 低レベル
    def _url(self, path: str) -> str:
        return f"{API_BASE}/repos/{self._repo}/contents/{quote(path, safe='/')}"

    async def _request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        """一時的なエラー（5xx・通信エラー・レート制限）は数回まで待って再試行する。"""
        delay = 1.0
        for attempt in range(4):
            try:
                resp = await self._client.request(method, url, headers=self._headers, **kwargs)
            except httpx2.TransportError as exc:
                if attempt == 3:
                    raise GitHubError(f"GitHub への接続に失敗しました: {exc}") from exc
                await self._sleep(delay + random.random())
                delay *= 2
                continue
            if resp.status_code >= 500 or resp.status_code == 429 or _is_rate_limited(resp):
                if attempt == 3:
                    raise GitHubError(f"GitHub API が一時的に利用できません（HTTP {resp.status_code}）")
                wait = _retry_after(resp) or delay
                await self._sleep(min(wait, 30.0) + random.random())
                delay *= 2
                continue
            return resp
        raise GitHubError("GitHub API の再試行回数を超えました")  # pragma: no cover

    # ------------------------------------------------------------------ 読み取り
    async def get(self, path: str) -> RemoteFile | None:
        params = {"ref": self._branch} if self._branch else None
        resp = await self._request("GET", self._url(path), params=params)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubError(f"GitHub からの取得に失敗しました（HTTP {resp.status_code}）: {path}")
        data = resp.json()
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError(f"ファイルではありません: {path}")
        sha = data["sha"]
        if data.get("encoding") == "base64" and data.get("content") is not None:
            raw = base64.b64decode(data["content"])
        else:
            # 1MB を超えるファイルは content が空になるので、blob API で取り直す
            raw = await self._get_blob(sha)
        return RemoteFile(path=path, text=raw.decode("utf-8-sig"), sha=sha)

    async def _get_blob(self, sha: str) -> bytes:
        url = f"{API_BASE}/repos/{self._repo}/git/blobs/{sha}"
        resp = await self._request("GET", url)
        if resp.status_code != 200:
            raise GitHubError(f"blob の取得に失敗しました（HTTP {resp.status_code}）")
        data = resp.json()
        return base64.b64decode(data.get("content") or "")

    # ------------------------------------------------------------------ 書き込み
    def _check(self, path: str) -> None:
        if not is_writable_path(path):
            raise ForbiddenPathError(f"このパスには書き込めません（LINEログ/ と ボット設定/ のみ）: {path}")

    async def put(self, path: str, text: str, sha: str | None, message: str) -> str:
        self._check(path)
        body: dict = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        if self._branch:
            body["branch"] = self._branch
        resp = await self._request("PUT", self._url(path), json=body)
        if resp.status_code in (200, 201):
            return resp.json()["content"]["sha"]
        _raise_write_error(resp, path)
        raise AssertionError("unreachable")  # pragma: no cover

    async def delete(self, path: str, sha: str, message: str) -> None:
        self._check(path)
        body: dict = {"message": message, "sha": sha}
        if self._branch:
            body["branch"] = self._branch
        resp = await self._request("DELETE", self._url(path), json=body)
        if resp.status_code in (200, 204, 404):
            return
        _raise_write_error(resp, path)

    async def update(self, path: str, mutate: Mutator, message: str) -> UpdateResult:
        """ファイルを取得して mutate(現在の内容 or None) で変更し、保存する。

        mutate が None を返したら書き込まない（変更なし）。
        競合したら取得し直して mutate からやり直す。
        """
        self._check(path)
        for attempt in range(self._max_conflict_retries):
            current = await self.get(path)
            result = mutate(current.text if current else None)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                return UpdateResult(changed=False, text=current.text if current else None, sha=current.sha if current else None)
            if current is not None and result == current.text:
                return UpdateResult(changed=False, text=current.text, sha=current.sha)
            try:
                new_sha = await self.put(path, result, current.sha if current else None, message)
                return UpdateResult(changed=True, text=result, sha=new_sha)
            except ConflictError:
                wait = min(0.5 * (2**attempt), 8.0) + random.random()
                log.info("GitHub 書き込みが競合したため取得し直します: %s（%d回目）", path, attempt + 1)
                await self._sleep(wait)
        raise GitHubError(f"書き込みの競合が解消しませんでした: {path}")

    async def delete_if(self, path: str, predicate: Callable[[str], bool], message: str) -> bool:
        """predicate(内容) が True のときだけファイルを削除する。削除したら True。"""
        self._check(path)
        for attempt in range(self._max_conflict_retries):
            current = await self.get(path)
            if current is None or not predicate(current.text):
                return False
            try:
                await self.delete(path, current.sha, message)
                return True
            except ConflictError:
                await self._sleep(min(0.5 * (2**attempt), 8.0) + random.random())
        raise GitHubError(f"削除の競合が解消しませんでした: {path}")


def _is_rate_limited(resp: httpx2.Response) -> bool:
    if resp.status_code != 403:
        return False
    if resp.headers.get("x-ratelimit-remaining") == "0" or resp.headers.get("retry-after"):
        return True
    try:
        msg = str(resp.json().get("message", "")).lower()
    except ValueError:
        return False
    return "rate limit" in msg


def _retry_after(resp: httpx2.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value and value.isdigit():
        return float(value)
    return None


def _raise_write_error(resp: httpx2.Response, path: str) -> None:
    try:
        message = str(resp.json().get("message", ""))
    except ValueError:
        message = resp.text[:200]
    if resp.status_code == 409:
        raise ConflictError(f"競合しました: {path}: {message}")
    if resp.status_code == 422 and "sha" in message.lower():
        # 既にファイルがあるのに sha を付けなかった／sha が古い
        raise ConflictError(f"sha が一致しません: {path}: {message}")
    raise GitHubError(f"GitHub への書き込みに失敗しました（HTTP {resp.status_code}）: {path}: {message}")
