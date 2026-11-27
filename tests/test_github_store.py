"""GitHub Contents API への書き込みのテスト（競合時の再取得・再試行、書き込み範囲の制限）。"""

import pytest

from app.github_store import ForbiddenPathError, GitHubError
from app.paths import is_writable_path

PATH = "LINEログ/家族グループ/2026-11-14.md"


@pytest.mark.anyio
async def test_create_and_update(store, github):
    r = await store.update(PATH, lambda t: (t or "") + "a\n", message="m")
    assert r.changed and github.text(PATH) == "a\n"
    r = await store.update(PATH, lambda t: t + "b\n", message="m")
    assert github.text(PATH) == "a\nb\n"


@pytest.mark.anyio
async def test_no_change_means_no_commit(store, github):
    github.put_text(PATH, "x\n")
    r = await store.update(PATH, lambda t: None, message="m")
    assert not r.changed and github.writes == []
    r = await store.update(PATH, lambda t: t, message="m")
    assert not r.changed and github.writes == []


@pytest.mark.anyio
async def test_conflict_409_refetches_and_retries(store, github):
    github.put_text(PATH, "base\n")
    calls = []

    def mutate(text):
        calls.append(text)
        if len(calls) == 1:
            # 取得から書き込みまでの間に、別の処理が書き込んだ状況を再現する
            github.put_text(PATH, "base\nother\n")
        return text + "mine\n"

    r = await store.update(PATH, mutate, message="m")
    assert r.changed
    assert github.text(PATH) == "base\nother\nmine\n"  # 取得し直した内容に追記されている
    assert calls == ["base\n", "base\nother\n"]


@pytest.mark.anyio
async def test_conflict_422_missing_sha_when_file_appeared(store, github):
    calls = []

    def mutate(text):
        calls.append(text)
        if len(calls) == 1:
            github.put_text(PATH, "created by someone else\n")
        return (text or "") + "mine\n"

    await store.update(PATH, mutate, message="m")
    assert github.text(PATH) == "created by someone else\nmine\n"


@pytest.mark.anyio
async def test_forced_409_then_success(store, github):
    github.forced_put_status = [409, 409]
    await store.update(PATH, lambda t: "ok\n", message="m")
    assert github.text(PATH) == "ok\n"


@pytest.mark.anyio
async def test_gives_up_after_too_many_conflicts(store, github):
    github.forced_put_status = [409] * 20
    with pytest.raises(GitHubError):
        await store.update(PATH, lambda t: "x\n", message="m")


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["README.md", "ノート/日記.md", "LINEログ", "LINEログ/../README.md",
                                  "/LINEログ/a.md", "ボット設定", ".github/workflows/x.yml", "LINEログ//a.md"])
async def test_refuses_to_write_outside_allowed_folders(store, github, path):
    with pytest.raises(ForbiddenPathError):
        await store.update(path, lambda t: "x", message="m")
    with pytest.raises(ForbiddenPathError):
        await store.put(path, "x", None, "m")
    with pytest.raises(ForbiddenPathError):
        await store.delete(path, "sha", "m")
    assert github.writes == []


def test_writable_paths():
    assert is_writable_path("LINEログ/家族/2026-11-14.md")
    assert is_writable_path("ボット設定/リマインド.md")
    assert not is_writable_path("LINEログ2/x.md")
    assert not is_writable_path("ボット設定/../x.md")


@pytest.mark.anyio
async def test_reads_large_file_via_blob_api(store, github):
    big = "あ" * 400_000  # UTF-8 で 1MB を超える
    github.put_text(PATH, big)
    f = await store.get(PATH)
    assert f is not None and f.text == big


@pytest.mark.anyio
async def test_delete_if(store, github):
    github.put_text("LINEログ/添付/a.md", "message_id: 1")
    assert not await store.delete_if("LINEログ/添付/a.md", lambda t: "2" in t, message="m")
    assert await store.delete_if("LINEログ/添付/a.md", lambda t: "1" in t, message="m")
    assert "LINEログ/添付/a.md" not in github.files
    assert not await store.delete_if("LINEログ/添付/a.md", lambda t: True, message="m")


@pytest.mark.anyio
async def test_missing_file_returns_none(store):
    assert await store.get("LINEログ/なし.md") is None
