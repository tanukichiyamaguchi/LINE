"""質問回答用の3つの道具（全文検索・ファイル読み取り・ファイル一覧）のテスト。"""

from pathlib import Path

import pytest

from app.vault import VaultTools


@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    files = {
        "LINEログ/家族グループ/2026-11-13.md": "# 2026-11-13（金） 家族グループ\n\n- 09:00 山田：明日のＢＢＱは10時集合 ^m1\n- 09:05 佐藤：了解です ^m2\n",
        "LINEログ/家族グループ/2026-11-14.md": "# 2026-11-14（土） 家族グループ\n\n- 10:00 山田：BBQの買い出しは佐藤さん ^m3\n- 10:01 鈴木：修正（編集済み） %%edited:123%% ^m4\n",
        "LINEログ/添付/2026-11-14_1000_見積書.pdf.md": "---\nmessage_id: \"5\"\n---\n# 見積書.pdf\n\n## 全文\n合計 12,345円\n",
        "メモ/買い物.md": "牛肉\n玉ねぎ\n",
        ".git/config": "[remote] token=SECRET",
        ".obsidian/workspace.json": "{\"BBQ\": 1}",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    long = "\n".join(f"行{i}" for i in range(1, 1001))
    (tmp_path / "メモ/長いノート.md").write_text(long, encoding="utf-8")
    return VaultTools(tmp_path)


def test_search_is_case_and_width_insensitive(vault):
    out = vault.search("bbq")
    assert "LINEログ/家族グループ/2026-11-13.md" in out and "LINEログ/家族グループ/2026-11-14.md" in out
    assert "L3: - 09:00 山田：明日のＢＢＱは10時集合 ^m1" in out


def test_search_newest_file_first(vault):
    out = vault.search("BBQ")
    assert out.index("2026-11-14.md") < out.index("2026-11-13.md")


def test_search_and_terms_and_prefix(vault):
    # スペース区切りの語は「同じノートにすべて含まれる」ものを探す（行は別でもよい）
    out = vault.search("BBQ 了解")
    assert "2026-11-13.md" in out and "2026-11-14.md" not in out
    out = vault.search("BBQ 買い出し")
    assert "2026-11-14.md" in out and "2026-11-13.md" not in out
    assert "見積書" not in vault.search("BBQ", path_prefix="LINEログ/添付/")
    assert "12,345円" in vault.search("合計", path_prefix="LINEログ/添付")


def test_search_regex(vault):
    out = vault.search(r"\d{1,2}時集合", regex=True)
    assert "2026-11-13.md" in out
    assert "エラー" in vault.search("(", regex=True)


def test_search_skips_hidden_and_git(vault):
    assert "SECRET" not in vault.search("token")
    assert ".obsidian" not in vault.search("BBQ")


def test_search_hides_edit_comment(vault):
    assert "%%edited" not in vault.search("修正")


def test_search_not_found(vault):
    assert "見つかりませんでした" in vault.search("存在しない語")


def test_read_file_with_line_numbers_and_paging(vault):
    out = vault.read_file("メモ/長いノート.md", start_line=1, max_lines=10)
    assert out.startswith("# メモ/長いノート.md（全 1000 行中 1〜10 行）")
    assert "\n10: 行10" in out and "start_line=11" in out
    out = vault.read_file("メモ/長いノート.md", start_line=995, max_lines=400)
    assert "1000: 行1000" in out and "start_line=" not in out


def test_read_file_accepts_wikilink_and_leading_slash(vault):
    assert "合計 12,345円" in vault.read_file("[[LINEログ/添付/2026-11-14_1000_見積書.pdf.md|見積書]]")
    assert "牛肉" in vault.read_file("/メモ/買い物.md")


@pytest.mark.parametrize("path", ["../etc/passwd", "メモ/../../x", ".git/config", "/../x"])
def test_read_file_blocks_escape_and_git(vault, path):
    assert vault.read_file(path).startswith("エラー")


def test_read_missing_file(vault):
    assert "ありません" in vault.read_file("メモ/なし.md")


def test_list_files(vault):
    top = vault.list_files("")
    assert "[dir]  LINEログ/" in top and "[dir]  メモ/" in top
    assert ".git" not in top and ".obsidian" not in top
    rec = vault.list_files("LINEログ", recursive=True)
    assert "[file] LINEログ/家族グループ/2026-11-13.md" in rec
    assert vault.list_files("../").startswith("エラー")


def test_exists(vault):
    assert vault.exists("メモ/買い物.md")
    assert not vault.exists("メモ/なし.md")
    assert not vault.exists("../x")
