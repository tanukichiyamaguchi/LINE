"""Word・Excel・PowerPoint・テキストファイルからのテキスト抽出。

* .docx / .xlsx / .pptx はライブラリ（python-docx / openpyxl / python-pptx）で抽出する。
* 旧形式（.doc / .xls / .ppt）やマクロ付き・ODF形式は、LibreOffice で新形式に
  変換してから抽出する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from datetime import date, datetime, time
from pathlib import Path

log = logging.getLogger(__name__)


class ExtractError(Exception):
    """抽出に失敗した。permanent=True のものは再試行しても直らない。"""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


WORD_EXT = {".docx"}
EXCEL_EXT = {".xlsx", ".xlsm"}
POWERPOINT_EXT = {".pptx"}
# LibreOffice で変換してから抽出する形式 → 変換先の拡張子
CONVERT_EXT = {
    ".doc": "docx", ".docm": "docx", ".dot": "docx", ".dotx": "docx", ".rtf": "docx", ".odt": "docx",
    ".xls": "xlsx", ".xlt": "xlsx", ".xltx": "xlsx", ".ods": "xlsx",
    ".ppt": "pptx", ".pptm": "pptx", ".pps": "pptx", ".ppsx": "pptx", ".pot": "pptx", ".potx": "pptx", ".odp": "pptx",
}
TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".markdown", ".json", ".xml", ".log", ".yaml", ".yml", ".html", ".htm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}


def file_kind(filename: str) -> str:
    """ファイル名から種類を判定する: pdf / word / excel / powerpoint / text / image / other"""
    ext = Path(filename).suffix.lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    if ext in WORD_EXT or CONVERT_EXT.get(ext) == "docx":
        return "word"
    if ext in EXCEL_EXT or CONVERT_EXT.get(ext) == "xlsx":
        return "excel"
    if ext in POWERPOINT_EXT or CONVERT_EXT.get(ext) == "pptx":
        return "powerpoint"
    return "other"


KIND_LABEL = {
    "pdf": "PDF", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
    "text": "テキスト", "image": "画像", "other": "ファイル",
}


# ---------------------------------------------------------------- テキストファイル
def decode_text(data: bytes) -> str:
    """文字コードを推定してデコードする（UTF-8 → UTF-16(BOM) → CP932 → EUC-JP の順）。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- Word
def extract_docx(path: str | Path) -> str:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(str(path))
    except Exception as exc:  # 壊れたファイルなど
        raise ExtractError(f"Word ファイルを読めませんでした（{type(exc).__name__}）", permanent=True) from exc

    out: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            level = _heading_level(style)
            out.append(("#" * level + " " + text) if level else text)
        elif child.tag == qn("w:tbl"):
            out.append(_table_to_markdown([[c.text for c in row.cells] for row in Table(child, doc).rows]))
    # テキストボックス内の文字（本文の段落には含まれない）
    boxes = []
    for box in body.iter(qn("w:txbxContent")):
        text = "".join(t.text or "" for t in box.iter(qn("w:t"))).strip()
        if text:
            boxes.append(text)
    if boxes:
        out.append("## テキストボックス")
        out.extend(boxes)
    return "\n\n".join(s for s in out if s)


def _heading_level(style_name: str) -> int:
    name = style_name.lower()
    for prefix in ("heading ", "見出し "):
        if name.startswith(prefix):
            rest = name[len(prefix):].strip()
            if rest.isdigit():
                return max(1, min(6, int(rest)))
    if name == "title":
        return 1
    return 0


# ---------------------------------------------------------------- Excel
def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S") if (value.hour or value.minute or value.second) else value.strftime("%Y-%m-%d")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _table_to_markdown(rows: list[list[str]], header: list[str] | None = None) -> str:
    rows = [[(c or "").replace("\n", " ").replace("|", "\\|").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = header or rows[0]
    body = rows if header else rows[1:]
    head = (head + [""] * width)[:width]
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def extract_xlsx(path: str | Path, max_columns: int = 60) -> str:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractError(f"Excel ファイルを読めませんでした（{type(exc).__name__}）", permanent=True) from exc
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                cells = [_cell_str(v) for v in row[:max_columns]]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    rows.append(cells)
            out.append(f"## シート: {ws.title}")
            if not rows:
                out.append("（空のシート）")
                continue
            width = max(len(r) for r in rows)
            header = [_col_letter(i + 1) for i in range(width)]
            out.append(_table_to_markdown(rows, header=header))
    finally:
        wb.close()
    return "\n\n".join(out)


# ---------------------------------------------------------------- PowerPoint
def extract_pptx(path: str | Path) -> str:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    try:
        prs = Presentation(str(path))
    except Exception as exc:
        raise ExtractError(f"PowerPoint ファイルを読めませんでした（{type(exc).__name__}）", permanent=True) from exc

    def shape_texts(shapes) -> list[str]:
        texts: list[str] = []
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                texts.extend(shape_texts(shape.shapes))
                continue
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                texts.append(_table_to_markdown(rows))
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
        return texts

    out: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip()
        out.append(f"## スライド {i}" + (f"：{title}" if title else ""))
        body = [t for t in shape_texts(slide.shapes) if t != title]
        out.extend(body or ["（テキストなし）"])
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                out.append("ノート：" + notes)
    return "\n\n".join(out)


# ---------------------------------------------------------------- LibreOffice 変換
async def convert_with_libreoffice(src: Path, target_ext: str, timeout: float = 180) -> Path:
    """LibreOffice（headless）で新形式に変換し、変換後のファイルパスを返す。"""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ExtractError("LibreOffice が見つかりません（旧形式は変換できません）")
    outdir = Path(tempfile.mkdtemp(prefix="lo-out-"))
    profile = Path(tempfile.gettempdir()) / f"lo-profile-{uuid.uuid4().hex}"
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", target_ext, "--outdir", str(outdir), str(src),
    ]
    env = dict(os.environ)
    env.setdefault("HOME", tempfile.gettempdir())
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ExtractError("LibreOffice での変換がタイムアウトしました") from None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    produced = outdir / f"{src.stem}.{target_ext}"
    if proc.returncode != 0 or not produced.exists():
        detail = err.decode("utf-8", errors="replace").strip()[-300:]
        shutil.rmtree(outdir, ignore_errors=True)
        raise ExtractError(f"LibreOffice での変換に失敗しました: {detail or proc.returncode}", permanent=True)
    return produced


async def extract_office(data: bytes, filename: str) -> str:
    """Office 系ファイル（新旧形式・ODF）からテキストを抽出する。"""
    ext = Path(filename).suffix.lower()
    workdir = Path(tempfile.mkdtemp(prefix="extract-"))
    target: Path | None = None
    try:
        src = workdir / f"input{ext}"
        src.write_bytes(data)
        if ext in WORD_EXT or ext in EXCEL_EXT or ext in POWERPOINT_EXT:
            target = src
        elif ext in CONVERT_EXT:
            target = await convert_with_libreoffice(src, CONVERT_EXT[ext])
        else:
            raise ExtractError("対応していない形式です", permanent=True)
        kind = file_kind(target.name)
        if kind == "word":
            return await asyncio.to_thread(extract_docx, target)
        if kind == "excel":
            return await asyncio.to_thread(extract_xlsx, target)
        if kind == "powerpoint":
            return await asyncio.to_thread(extract_pptx, target)
        raise ExtractError("対応していない形式です", permanent=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if target is not None and target.parent != workdir:
            shutil.rmtree(target.parent, ignore_errors=True)
