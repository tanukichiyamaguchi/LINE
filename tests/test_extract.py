"""Word・Excel・PowerPoint のテキスト抽出のテスト（旧形式は LibreOffice で変換してから抽出）。"""

import io
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.extract import ExtractError, decode_text, extract_office, file_kind

HAS_SOFFICE = shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def make_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("議事録", level=1)
    doc.add_paragraph("出席者：山田、佐藤")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "議題", "結論"
    table.cell(1, 0).text, table.cell(1, 1).text = "予算", "承認|済"
    doc.add_paragraph("以上")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "見積"
    ws.append(["品目", "数量", "単価"])
    ws.append(["りんご", 3, 120.0])
    ws.append([])
    ws.append(["納品日", datetime(2026, 12, 1), None])
    wb.create_sheet("空")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "来期の計画"
    slide.placeholders[1].text = "売上を10%伸ばす"
    rows = slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(1)).table
    rows.cell(0, 0).text, rows.cell(0, 1).text = "四半期", "目標"
    rows.cell(1, 0).text, rows.cell(1, 1).text = "Q1", "100"
    slide.notes_slide.notes_text_frame.text = "補足：前年比"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


@pytest.mark.anyio
async def test_docx():
    text = await extract_office(make_docx(), "議事録.docx")
    assert "# 議事録" in text
    assert "出席者：山田、佐藤" in text
    assert "| 議題 | 結論 |" in text and "| 予算 | 承認\\|済 |" in text
    assert text.index("出席者") < text.index("| 議題") < text.index("以上")


@pytest.mark.anyio
async def test_xlsx():
    text = await extract_office(make_xlsx(), "見積.xlsx")
    assert "## シート: 見積" in text
    assert "| A | B | C |" in text
    assert "| 品目 | 数量 | 単価 |" in text and "| りんご | 3 | 120 |" in text
    assert "| 納品日 | 2026-12-01 |  |" in text
    assert "## シート: 空" in text and "（空のシート）" in text


@pytest.mark.anyio
async def test_pptx():
    text = await extract_office(make_pptx(), "計画.pptx")
    assert "## スライド 1：来期の計画" in text
    assert "売上を10%伸ばす" in text
    assert "| 四半期 | 目標 |" in text
    assert "ノート：補足：前年比" in text


@pytest.mark.anyio
async def test_corrupt_file_is_permanent_failure():
    with pytest.raises(ExtractError) as exc:
        await extract_office(b"this is not a zip", "壊れた.docx")
    assert exc.value.permanent


def _convert(data: bytes, src_ext: str, target: str, tmp: Path) -> bytes:
    src = tmp / f"src{src_ext}"
    src.write_bytes(data)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    subprocess.run([soffice, "--headless", f"-env:UserInstallation=file://{tmp}/profile",
                    "--convert-to", target, "--outdir", str(tmp), str(src)], check=True, capture_output=True, timeout=180)
    return (tmp / f"src.{target}").read_bytes()


@pytest.mark.anyio
@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice が無い環境では旧形式の変換テストを省略")
@pytest.mark.parametrize(
    "maker, src_ext, legacy, expect",
    [
        (make_docx, ".docx", "doc", ["議事録", "出席者：山田、佐藤", "予算"]),
        (make_xlsx, ".xlsx", "xls", ["## シート: 見積", "りんご"]),
        (make_pptx, ".pptx", "ppt", ["来期の計画", "売上を10%伸ばす"]),
    ],
)
async def test_legacy_formats_via_libreoffice(tmp_path, maker, src_ext, legacy, expect):
    legacy_bytes = _convert(maker(), src_ext, legacy, tmp_path)
    text = await extract_office(legacy_bytes, f"古い形式.{legacy}")
    for needle in expect:
        assert needle in text


def test_decode_text_detects_japanese_encodings():
    assert decode_text("こんにちは".encode("utf-8")) == "こんにちは"
    assert decode_text("こんにちは".encode("cp932")) == "こんにちは"
    assert decode_text("﻿BOM付き".encode("utf-8")) == "BOM付き"
    assert decode_text("UTF16".encode("utf-16")) == "UTF16"


@pytest.mark.parametrize(
    "name, kind",
    [("a.PDF", "pdf"), ("a.docx", "word"), ("a.doc", "word"), ("a.xlsx", "excel"), ("a.xls", "excel"),
     ("a.pptx", "powerpoint"), ("a.ppt", "powerpoint"), ("a.csv", "text"), ("a.jpg", "image"), ("a.zip", "other"),
     ("名前なし", "other")],
)
def test_file_kind(name, kind):
    assert file_kind(name) == kind
