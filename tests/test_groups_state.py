"""グループ一覧.md と内部状態ファイル（送信待ち・通知待ち）のテスト。"""

from app.groups import Group, append_group, parse_groups, resolve_group
from app.state import add_notice, clear_pending_if, get_pending, list_notices, remove_notices, set_pending

G1, G2, G3 = "C" + "a" * 32, "C" + "b" * 32, "C" + "c" * 32


def test_parse_groups_is_tolerant_to_hand_edits():
    text = f"""# グループ一覧
説明文 {G3} （箇条書きでない行は無視）
- 家族グループ：{G1}
* 町内会 (`{G2}`)
- 名前（本家）: {G3}
- IDのない行
"""
    assert parse_groups(text) == [Group("家族グループ", G1), Group("町内会", G2), Group("名前（本家）", G3)]


def test_append_group_and_no_duplicate():
    text = append_group(None, "家族グループ", G1)
    assert text.startswith("# グループ一覧") and text.endswith(f"- 家族グループ：{G1}\n")
    assert append_group(text, "別名", G1) is None
    text2 = append_group(text, "家族グループ", G2)
    assert f"- 家族グループ（bbbb）：{G2}" in text2


def test_resolve_group():
    groups = [Group("家族グループ", G1), Group("町内会", G2), Group("町内会役員", G3)]
    assert resolve_group(groups, "家族グループ") == [groups[0]]
    assert resolve_group(groups, "家族") == [groups[0]]  # 「グループ」を省略してもよい
    assert resolve_group(groups, "ｶｿﾞｸ") == []
    assert resolve_group(groups, "町内会") == [groups[1]]  # 完全一致を優先
    assert resolve_group(groups, "役員") == [groups[2]]
    assert resolve_group(groups, f"ID {G2}") == [groups[1]]
    assert resolve_group(groups, "") == []


def test_notices_add_dedupe_remove():
    t = add_notice(None, "k1", "残り通数が足りません", "2026-11-14 09:00")
    assert add_notice(t, "k1", "重複", "2026-11-14 09:05") is None
    t = add_notice(t, "k2", "送り先が見つかりません", "2026-11-14 09:05")
    assert [n["key"] for n in list_notices(t)] == ["k1", "k2"]
    t = remove_notices(t, {"k1"})
    assert [n["key"] for n in list_notices(t)] == ["k2"]
    assert remove_notices(t, {"zzz"}) is None
    assert list_notices("壊れたファイル") == []


def test_pending_set_get_clear():
    t = set_pending({"id": "p1", "group_id": G1, "text": "明日は休み"})
    assert get_pending(t)["text"] == "明日は休み"
    assert clear_pending_if(t, "other") is None
    cleared = clear_pending_if(t, "p1")
    assert get_pending(cleared) is None
    assert get_pending(None) is None
