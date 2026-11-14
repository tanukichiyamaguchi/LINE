"""ボット設定/グループ一覧.md の読み書き。

1行1グループで「- グループ名：グループID」の形式。ボットは初めてのグループから
イベントが届いたときに1行追記する。名前は人が書き換えてよく、書き換えた名前が
以降のログ保存先フォルダ名と、お知らせ・リマインドの送り先指定に使われる。
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass

from .github_store import GitHubStore
from .line_api import LineClient
from .paths import GROUPS_FILE

log = logging.getLogger(__name__)

GROUP_ID_RE = re.compile(r"\b(C[0-9a-f]{32})\b")

HEADER = """# グループ一覧

ボットが参加しているLINEグループの一覧です。初めてのグループからイベントが届くと、ボットが自動で1行追記します。

- 書式：`- グループ名：グループID`
- グループ名は書き換えて構いません。書き換えた名前が、以降のログの保存先フォルダ名（LINEログ/<グループ名>/）と、お知らせ・リマインドの送り先指定に使われます。
- グループIDは変更しないでください。

"""


@dataclass(frozen=True)
class Group:
    name: str
    group_id: str


def parse_groups(text: str | None) -> list[Group]:
    groups: list[Group] = []
    if not text:
        return groups
    for line in text.splitlines():
        if not line.lstrip().startswith(("-", "*")):
            continue
        m = GROUP_ID_RE.search(line)
        if not m:
            continue
        name = (line[: m.start()] + line[m.end():]).replace("`", "")
        name = re.sub(r"[（(\[]\s*[）)\]]", "", name)  # ID を除いた後に残る空のかっこ
        name = name.strip().lstrip("-*").strip()
        name = name.strip("：: \t").strip()
        if name:
            groups.append(Group(name=name, group_id=m.group(1)))
    return groups


def unique_name(name: str, group_id: str, existing: list[Group]) -> str:
    """同じ名前のグループが既にあれば、IDの末尾を付けて区別する。"""
    taken = {g.name for g in existing if g.group_id != group_id}
    if name not in taken:
        return name
    return f"{name}（{group_id[-4:]}）"


def append_group(text: str | None, name: str, group_id: str) -> str | None:
    """グループを追記した新しい内容を返す。既に登録済みなら None。"""
    groups = parse_groups(text)
    if any(g.group_id == group_id for g in groups):
        return None
    base = text if (text and text.strip()) else HEADER
    if not base.endswith("\n"):
        base += "\n"
    name = unique_name(name, group_id, groups)
    return base + f"- {name}：{group_id}\n"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold().replace(" ", "")


def resolve_group(groups: list[Group], query: str) -> list[Group]:
    """送り先の指定（名前やID）からグループを探す。完全一致 → 「グループ」を除いた一致 → 部分一致。"""
    q = (query or "").strip()
    if not q:
        return []
    m = GROUP_ID_RE.search(q)
    if m:
        return [g for g in groups if g.group_id == m.group(1)]
    nq = _norm(q)
    exact = [g for g in groups if _norm(g.name) == nq]
    if exact:
        return exact
    trimmed = nq.removesuffix("グループ").removesuffix("のグループ")
    loose = [g for g in groups if _norm(g.name).removesuffix("グループ") == trimmed]
    if loose:
        return loose
    return [g for g in groups if nq in _norm(g.name) or (trimmed and trimmed in _norm(g.name))]


class GroupRegistry:
    """グループ一覧.md を参照・追記する（読み取りは短時間キャッシュする）。"""

    def __init__(self, store: GitHubStore, line: LineClient, ttl_seconds: float = 300) -> None:
        self.store = store
        self.line = line
        self.ttl = ttl_seconds
        self._cache: list[Group] | None = None
        self._cache_at = 0.0

    async def list(self, fresh: bool = False) -> list[Group]:
        if not fresh and self._cache is not None and time.monotonic() - self._cache_at < self.ttl:
            return self._cache
        f = await self.store.get(GROUPS_FILE)
        self._cache = parse_groups(f.text if f else None)
        self._cache_at = time.monotonic()
        return self._cache

    async def ensure(self, group_id: str) -> str:
        """グループの登録名を返す。未登録ならグループ概要APIで名前を取得して追記する。"""
        for g in await self.list():
            if g.group_id == group_id:
                return g.name
        for g in await self.list(fresh=True):
            if g.group_id == group_id:
                return g.name
        name = await self.line.group_name(group_id) or f"グループ_{group_id[-6:]}"
        await self.store.update(
            GROUPS_FILE,
            lambda text: append_group(text, name, group_id),
            message=f"グループ一覧: {name} を追加",
        )
        groups = await self.list(fresh=True)
        for g in groups:
            if g.group_id == group_id:
                log.info("新しいグループを登録しました: %s", group_id)
                return g.name
        return name
