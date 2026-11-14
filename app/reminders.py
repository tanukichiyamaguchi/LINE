"""ボット設定/リマインド.md の読み書き。

人が直接編集しても壊れにくいよう、次の方針で扱う。

* 1件ごとに「## ID」の見出し、その下に「- 項目名: 値」の箇条書き。
* 「:」「：」のどちらでもよく、項目名の別名（送信先・内容など）も受け付ける。
* ボットが変更するときは、該当する項目の行だけを書き換える（他の件や、
  人が書き足したメモ・見出しの後ろの説明などはそのまま残す）。
* 書式が読めない件は送信せず、一覧表示でエラーとして知らせる。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from .schedule import Schedule, ScheduleError, parse_schedule
from .timeutil import fmt_minute, parse_minute

HEADER = """# リマインド

> [!info] このファイルについて
> ボットが定期的にLINEグループへ送るリマインドの一覧です。オーナーがLINEの1対1で追加・変更・停止・一覧表示できるほか、このファイルを直接編集しても構いません。
> 1件ごとに「## ID」の見出しを置き、その下に「- 項目名: 値」を並べます。項目は 送り先／スケジュール／本文／状態／最終送信日時／更新日時 です。
> 送り先は「ボット設定/グループ一覧.md」にあるグループ名を書きます。
> スケジュールの例：毎日 08:00／平日 07:30／毎週 月,木 09:00／毎月 1,15日 10:00／毎月 末日 18:00／毎月 第2火曜 19:00／毎年 12-25 09:00／2026-12-01 09:00（1回のみ）。時刻は日本時間、祝日は考慮しません。
> 本文を複数行にするときは、2行目以降の行頭に半角スペースを2つ入れます。
> 状態は「有効」か「無効」。最終送信日時と更新日時はボットが記録します（更新日時より前の予定時刻の回は送りません）。

"""

FIELD_ORDER = ["送り先", "スケジュール", "本文", "状態", "最終送信日時", "更新日時"]
_ALIASES = {
    "送り先": {"送り先", "送信先", "宛先", "グループ", "送り先グループ", "送信先グループ"},
    "スケジュール": {"スケジュール", "日時", "予定", "タイミング", "送信日時"},
    "本文": {"本文", "内容", "メッセージ", "文面", "テキスト"},
    "状態": {"状態", "有効/無効", "有効", "ステータス"},
    "最終送信日時": {"最終送信日時", "最終送信", "前回送信", "前回送信日時"},
    "更新日時": {"更新日時", "更新", "登録日時", "変更日時"},
}
_KEY_LOOKUP = {unicodedata.normalize("NFKC", a): k for k, aliases in _ALIASES.items() for a in aliases}

_HEADING_RE = re.compile(r"^##\s+(?P<text>\S.*?)\s*$")
_FIELD_RE = re.compile(r"^[-*]\s+(?P<key>[^:：\n]+?)\s*[:：][ \t]?(?P<value>.*)$")
_CONT_RE = re.compile(r"^(?:  |\t)")
_ON = {"有効", "on", "オン", "true", "yes", "はい", "1", "稼働", "稼働中"}
_OFF = {"無効", "off", "オフ", "false", "no", "いいえ", "0", "停止", "停止中", "一時停止"}

NOT_SENT = "未送信"


def canonical_key(key: str) -> str | None:
    return _KEY_LOOKUP.get(unicodedata.normalize("NFKC", key).strip())


def parse_enabled(value: str) -> bool | None:
    v = unicodedata.normalize("NFKC", value).strip().lower()
    if v == "":
        return True  # 状態の行が無ければ有効とみなす
    if v in _ON:
        return True
    if v in _OFF:
        return False
    return None


@dataclass
class Reminder:
    rid: str
    title: str
    start: int  # 見出し行の行番号
    end: int  # 次の見出しの行番号（含まない）
    fields: dict[str, str] = field(default_factory=dict)
    field_lines: dict[str, tuple[int, int]] = field(default_factory=dict)  # 項目 → (開始行, 終了行(含まない))
    errors: list[str] = field(default_factory=list)
    schedule: Schedule | None = None
    enabled: bool = False
    last_sent: datetime | None = None
    updated_at: datetime | None = None

    @property
    def target(self) -> str:
        return self.fields.get("送り先", "").strip()

    @property
    def body(self) -> str:
        return self.fields.get("本文", "").strip()

    @property
    def schedule_text(self) -> str:
        return self.fields.get("スケジュール", "").strip()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _split(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _join(lines: list[str]) -> str:
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


def parse_reminders(text: str | None) -> list[Reminder]:
    if not text:
        return []
    lines = _split(text)
    heads = [(i, m.group("text")) for i, line in enumerate(lines) if (m := _HEADING_RE.match(line))]
    reminders: list[Reminder] = []
    for _n, (start, head) in enumerate(heads):
        # 次の見出し（# から始まる行）までを1件とみなす
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("#"):
                end = j
                break
        rid, _, title = head.partition(" ")
        r = Reminder(rid=rid.strip(), title=title.strip(), start=start, end=end)
        j = start + 1
        while j < end:
            m = _FIELD_RE.match(lines[j])
            key = canonical_key(m.group("key")) if m else None
            if not m or key is None:
                j += 1
                continue
            value_lines = [m.group("value")]
            k = j + 1
            while k < end and _CONT_RE.match(lines[k]):
                value_lines.append(lines[k].strip())
                k += 1
            while len(value_lines) > 1 and not value_lines[-1]:
                value_lines.pop()
            if key not in r.fields:  # 同じ項目が2回あれば最初を使う
                r.fields[key] = "\n".join(value_lines).strip() if key == "本文" else value_lines[0].strip()
                r.field_lines[key] = (j, k)
            j = k
        if not any(key in r.fields for key in ("送り先", "スケジュール", "本文")):
            continue  # リマインドの項目が無い見出しは説明文などとみなす
        _validate(r)
        reminders.append(r)
    return reminders


def _validate(r: Reminder) -> None:
    if not r.rid:
        r.errors.append("IDがありません")
    if not r.target:
        r.errors.append("送り先がありません")
    if not r.body:
        r.errors.append("本文がありません")
    if not r.schedule_text:
        r.errors.append("スケジュールがありません")
    else:
        try:
            r.schedule = parse_schedule(r.schedule_text)
        except ScheduleError as exc:
            r.errors.append(str(exc))
    enabled = parse_enabled(r.fields.get("状態", ""))
    if enabled is None:
        r.errors.append(f"状態「{r.fields.get('状態')}」は「有効」か「無効」で書いてください")
        enabled = False
    r.enabled = enabled
    last = r.fields.get("最終送信日時", "")
    r.last_sent = parse_minute(last)
    if last.strip() and r.last_sent is None and last.strip() not in {"-", "ー", "なし", NOT_SENT}:
        r.errors.append(f"最終送信日時「{last}」を読めません（YYYY-MM-DD HH:MM）")
    upd = r.fields.get("更新日時", "")
    r.updated_at = parse_minute(upd)


def find(reminders: list[Reminder], rid: str) -> Reminder | None:
    norm = unicodedata.normalize("NFKC", rid or "").strip().upper()
    for r in reminders:
        if unicodedata.normalize("NFKC", r.rid).strip().upper() == norm:
            return r
    return None


def next_id(reminders: list[Reminder]) -> str:
    nums = [int(m.group(1)) for r in reminders if (m := re.fullmatch(r"[Rr](\d+)", r.rid))]
    return f"R{max(nums, default=0) + 1}"


def render_field(key: str, value: str) -> list[str]:
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    if key == "本文":
        first, *rest = value.split("\n") if value else [""]
        return [f"- {key}: {first}".rstrip()] + [f"  {line}".rstrip() if line else "  " for line in rest]
    return [f"- {key}: {value.replace(chr(10), ' ').strip()}".rstrip()]


def render_section(rid: str, fields: dict[str, str], title: str = "") -> list[str]:
    lines = [f"## {rid}" + (f" {title}" if title else "")]
    for key in FIELD_ORDER:
        if key in fields:
            lines += render_field(key, fields[key])
    return lines


def add_reminder(text: str | None, fields: dict[str, str]) -> tuple[str, str]:
    """新しいリマインドを末尾に追加する。戻り値：(新しい内容, 付けたID)"""
    base = text if (text and text.strip()) else HEADER
    reminders = parse_reminders(base)
    rid = next_id(reminders)
    lines = _split(base)
    while lines and lines[-1] == "":
        lines.pop()
    lines += [""] + render_section(rid, fields) + [""]
    return _join(lines), rid


def set_fields(text: str, rid: str, updates: dict[str, str]) -> str:
    """指定したリマインドの項目だけを書き換える（無い項目は追加する）。"""
    lines = _split(text)
    r = find(parse_reminders(text), rid)
    if r is None:
        raise KeyError(rid)
    # 後ろの行から置き換えると行番号がずれない
    replacements: list[tuple[int, int, list[str]]] = []
    missing: list[str] = []
    for key in FIELD_ORDER:
        if key not in updates:
            continue
        if key in r.field_lines:
            a, b = r.field_lines[key]
            replacements.append((a, b, render_field(key, updates[key])))
        else:
            missing.append(key)
    for a, b, new in sorted(replacements, key=lambda x: x[0], reverse=True):
        lines[a:b] = new
    if missing:
        # 置き換え後の内容で、その件の最後の項目行の直後に追加する
        r2 = find(parse_reminders(_join(lines)), rid)
        assert r2 is not None
        insert_at = max((b for _, b in r2.field_lines.values()), default=r2.start + 1)
        add: list[str] = []
        for key in missing:
            add += render_field(key, updates[key])
        lines[insert_at:insert_at] = add
    return _join(lines)


def new_fields(target: str, schedule: Schedule, body: str, now: datetime) -> dict[str, str]:
    return {
        "送り先": target,
        "スケジュール": schedule.canonical(),
        "本文": body,
        "状態": "有効",
        "最終送信日時": NOT_SENT,
        "更新日時": fmt_minute(now),
    }
