"""Vault の浅い複製（git clone --depth 1）と、質問回答用の3つの道具。

* 起動時に浅く複製し、質問のたびに git fetch --depth 1 で最新化する。
* 道具：全文検索（search_vault）・ファイル読み取り（read_file）・ファイル一覧（list_files）
  いずれも複製したディレクトリの外や .git を読めないようにしている。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".canvas"}
MAX_FILE_BYTES_FOR_SEARCH = 20 * 1024 * 1024
_EDIT_COMMENT = re.compile(r" ?%%edited:\d+%%")


class VaultError(Exception):
    pass


def _norm(s: str) -> str:
    """検索用の正規化（全角半角の統一・大文字小文字の無視）。"""
    return unicodedata.normalize("NFKC", s).casefold()


def _clean_line(line: str, limit: int = 300) -> str:
    line = _EDIT_COMMENT.sub("", line.rstrip("\n"))
    return line if len(line) <= limit else line[:limit] + "…"


@dataclass
class VaultTools:
    """複製済みの Vault ディレクトリに対する読み取り専用の道具。"""

    root: Path

    # ---------------------------------------------------------------- 共通
    def _resolve(self, rel: str) -> Path:
        rel = unicodedata.normalize("NFC", (rel or "").strip())
        rel = rel.strip().strip("`").strip()
        if rel.startswith("[[") and rel.endswith("]]"):
            rel = rel[2:-2].split("|", 1)[0]
        rel = rel.lstrip("/")
        while rel.startswith("./"):
            rel = rel[2:]
        root = self.root.resolve()
        target = (root / rel).resolve() if rel else root
        if target != root and root not in target.parents:
            raise VaultError("Vault の外は読めません")
        relparts = target.relative_to(root).parts
        if relparts and relparts[0] == ".git":
            raise VaultError(".git は読めません")
        return target

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    def _iter_text_files(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in filenames:
                if name.startswith("."):
                    continue
                p = Path(dirpath) / name
                if p.suffix.lower() in TEXT_SUFFIXES:
                    yield p

    def exists(self, rel: str) -> bool:
        try:
            return self._resolve(rel).is_file()
        except VaultError:
            return False

    # ---------------------------------------------------------------- 全文検索
    def search(self, query: str, path_prefix: str = "", regex: bool = False,
               max_files: int = 30, max_lines_per_file: int = 5, max_chars: int = 12000) -> str:
        query = (query or "").strip()
        if not query:
            return "エラー: 検索語が空です。"
        try:
            base = self._resolve(path_prefix)
        except VaultError as exc:
            return f"エラー: {exc}"
        if not base.exists():
            return f"エラー: フォルダ「{path_prefix}」はありません。list_files で確認してください。"

        if regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as exc:
                return f"エラー: 正規表現が不正です: {exc}"

            def file_ok(norm_text: str, text: str) -> bool:
                return pattern.search(text) is not None

            def line_score(line: str) -> int:
                return 1 if pattern.search(line) else 0
        else:
            terms = [_norm(t) for t in query.split() if t.strip()]

            def file_ok(norm_text: str, text: str) -> bool:
                return all(t in norm_text for t in terms)

            def line_score(line: str) -> int:
                nl = _norm(line)
                return sum(1 for t in terms if t in nl)

        files = [base] if base.is_file() else list(self._iter_text_files(base))
        hits: list[tuple[str, list[tuple[int, str]], int]] = []
        for p in files:
            try:
                if p.stat().st_size > MAX_FILE_BYTES_FOR_SEARCH:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not file_ok(_norm(text) if not regex else "", text):
                continue
            scored = []
            for i, line in enumerate(text.splitlines(), start=1):
                s = line_score(line)
                if s:
                    scored.append((s, i, line))
            best = sorted(scored, key=lambda x: (-x[0], x[1]))[:max_lines_per_file]
            best.sort(key=lambda x: x[1])
            hits.append((self._rel(p), [(i, ln) for _, i, ln in best], len(scored)))

        if not hits:
            where = f"（{path_prefix} 内）" if path_prefix else ""
            return f"「{query}」に一致するノートは見つかりませんでした{where}。"
        # 日付入りのファイル名が多いので、パスの降順（新しい日付が先）に並べる
        hits.sort(key=lambda h: h[0], reverse=True)
        out = [f"「{query}」に一致: {len(hits)} ファイル" + (f"（先頭 {max_files} 件を表示）" if len(hits) > max_files else "")]
        used = len(out[0])
        for rel, lines, total in hits[:max_files]:
            block = [f"## {rel}（該当 {total} 行）"]
            block += [f"L{i}: {_clean_line(ln)}" for i, ln in lines]
            chunk = "\n".join(block)
            if used + len(chunk) > max_chars:
                out.append("…（文字数の上限に達したため以降は省略。path_prefix で絞り込んでください）")
                break
            out.append(chunk)
            used += len(chunk)
        return "\n".join(out)

    # ---------------------------------------------------------------- 読み取り
    def read_file(self, path: str, start_line: int = 1, max_lines: int = 300, max_chars: int = 30000) -> str:
        try:
            p = self._resolve(path)
        except VaultError as exc:
            return f"エラー: {exc}"
        if not p.is_file():
            return f"エラー: ファイル「{path}」はありません。search_vault や list_files で正しいパスを確認してください。"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"エラー: 読み取れませんでした: {exc}"
        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        count = max(1, min(int(max_lines or 300), 400))
        if start > total and total > 0:
            return f"エラー: {self._rel(p)} は全 {total} 行です。start_line を小さくしてください。"
        selected = lines[start - 1:start - 1 + count]
        body: list[str] = []
        used = 0
        last = start - 1
        for offset, ln in enumerate(selected):
            row = f"{start + offset}: {_clean_line(ln, limit=2000)}"
            if used + len(row) > max_chars:
                break
            body.append(row)
            used += len(row) + 1
            last = start + offset
        header = f"# {self._rel(p)}（全 {total} 行中 {start}〜{last} 行）"
        tail = f"\n…続きは start_line={last + 1} で読めます。" if last < total else ""
        return header + "\n" + "\n".join(body) + tail

    # ---------------------------------------------------------------- 一覧
    def list_files(self, path: str = "", recursive: bool = False, max_entries: int = 300) -> str:
        try:
            base = self._resolve(path)
        except VaultError as exc:
            return f"エラー: {exc}"
        if not base.exists():
            return f"エラー: 「{path}」はありません。"
        if base.is_file():
            return f"[file] {self._rel(base)}（{base.stat().st_size:,} バイト）"
        rel_base = self._rel(base) if base != self.root.resolve() else ""
        entries: list[str] = []
        if recursive:
            for p in self._iter_all(base):
                entries.append(f"[file] {self._rel(p)}")
                if len(entries) >= max_entries:
                    break
        else:
            for child in sorted(base.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    entries.append(f"[dir]  {child.name}/")
                else:
                    entries.append(f"[file] {child.name}（{child.stat().st_size:,} バイト）")
                if len(entries) >= max_entries:
                    break
        title = f"# {rel_base or '(Vault の最上位)'} の一覧" + ("（配下すべて）" if recursive else "")
        if not entries:
            return title + "\n（空です）"
        more = f"\n…{max_entries} 件で打ち切りました。" if len(entries) >= max_entries else ""
        return title + "\n" + "\n".join(entries) + more

    def _iter_all(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if not name.startswith("."):
                    yield Path(dirpath) / name


class VaultMirror:
    """Vault リポジトリの浅い複製を管理する。"""

    def __init__(self, repo: str, token: str, directory: str, branch: str | None = None) -> None:
        self.repo = repo
        self.branch = branch
        self.dir = Path(directory)
        self._token = token
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._last_error: str | None = None

    @property
    def tools(self) -> VaultTools:
        return VaultTools(self.dir)

    def _env(self) -> dict[str, str]:
        # トークンはコマンドライン引数に出さず、環境変数経由の git 設定で渡す
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        })
        return env

    async def _git(self, *args: str, cwd: Path | None = None, timeout: float = 300) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd) if cwd else None,
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise VaultError(f"git {args[0]} がタイムアウトしました") from None
        if proc.returncode != 0:
            msg = err.decode("utf-8", errors="replace").replace(self._token, "***")[-500:]
            raise VaultError(f"git {args[0]} に失敗しました: {msg.strip()}")
        return out.decode("utf-8", errors="replace")

    async def ensure_cloned(self) -> None:
        """未複製なら浅く複製する（起動時にバックグラウンドで呼ぶ）。"""
        if self._ready.is_set():
            return
        async with self._lock:
            if self._ready.is_set():
                return
            if (self.dir / ".git").is_dir():
                self._ready.set()
                return
            if self.dir.exists():
                shutil.rmtree(self.dir, ignore_errors=True)
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--depth", "1", "--single-branch", "--no-tags"]
            if self.branch:
                args += ["--branch", self.branch]
            args += [f"https://github.com/{self.repo}.git", str(self.dir)]
            try:
                await self._git(*args, timeout=600)
            except VaultError as exc:
                self._last_error = str(exc)
                log.error("Vault の複製に失敗しました: %s", exc)
                raise
            self._last_error = None
            self._ready.set()
            log.info("Vault を浅く複製しました: %s", self.repo)

    async def refresh(self) -> None:
        """質問のたびに最新化する（失敗した場合は複製し直す）。"""
        await self.ensure_cloned()
        async with self._lock:
            ref = self.branch or "HEAD"
            try:
                await self._git("fetch", "--depth", "1", "--no-tags", "origin", ref, cwd=self.dir, timeout=180)
                await self._git("reset", "--hard", "FETCH_HEAD", cwd=self.dir, timeout=120)
                await self._git("clean", "-fdq", cwd=self.dir, timeout=120)
                return
            except VaultError as exc:
                log.warning("Vault の最新化に失敗したため複製し直します: %s", exc)
                self._ready.clear()
                shutil.rmtree(self.dir, ignore_errors=True)
        await self.ensure_cloned()
