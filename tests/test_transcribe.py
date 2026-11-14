"""画像の前処理・PDFの分割・文字起こし（Claude 呼び出しは偽物）のテスト。"""

import base64
import io

import pytest
from anthropic.types.beta import BetaMessage
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.claude import ClaudeError, RefusalError
from app.line_api import LineApiError
from app.transcribe import (
    MAX_IMAGE_EDGE,
    TranscribeError,
    Transcriber,
    classify_failure,
    clean_summary,
    prepare_image,
    split_pdf,
)


def make_message(text: str, stop_reason: str = "end_turn", fallback: bool = False) -> BetaMessage:
    content = []
    if fallback:
        content.append({"type": "text", "text": "（拒否されたモデルの途中出力）"})
        content.append({"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                        "trigger": {"type": "refusal", "category": "cyber"}})
    content.append({"type": "text", "text": text})
    return BetaMessage.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None, "stop_details": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def pdf_bytes(pages: int, password: str | None = None, owner_only: bool = False) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    if password is not None:
        w.encrypt(user_password="" if owner_only else password, owner_password=password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 画像
def _jpeg(size=(100, 50), orientation: int | None = None) -> bytes:
    img = Image.new("RGB", size, (200, 10, 10))
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def test_small_jpeg_is_passed_through():
    data = _jpeg()
    out, media = prepare_image(data)
    assert out == data and media == "image/jpeg"


def test_exif_rotation_is_applied():
    out, media = prepare_image(_jpeg((100, 50), orientation=6))  # 90度回転の指定
    img = Image.open(io.BytesIO(out))
    assert img.size == (50, 100) and media == "image/jpeg"


def test_large_image_is_resized():
    buf = io.BytesIO()
    Image.new("RGB", (6000, 3000), (0, 0, 0)).save(buf, format="PNG")
    out, media = prepare_image(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert max(img.size) <= MAX_IMAGE_EDGE and media == "image/jpeg"


def test_unsupported_format_is_converted():
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (0, 255, 0)).save(buf, format="BMP")
    out, media = prepare_image(buf.getvalue())
    assert media == "image/jpeg" and Image.open(io.BytesIO(out)).format == "JPEG"


def test_transparent_png_stays_png_when_resized():
    buf = io.BytesIO()
    Image.new("RGBA", (3000, 100), (0, 0, 0, 0)).save(buf, format="PNG")
    out, media = prepare_image(buf.getvalue())
    assert media == "image/png"


def test_not_an_image_is_permanent_failure():
    with pytest.raises(TranscribeError) as exc:
        prepare_image(b"not an image")
    assert exc.value.permanent


# ---------------------------------------------------------------- PDF の分割
def test_small_pdf_is_single_chunk():
    data = pdf_bytes(3)
    total, chunks = split_pdf(data, 20)
    assert total == 3 and chunks == [(1, 3, data)]


def test_large_pdf_is_split_by_pages():
    total, chunks = split_pdf(pdf_bytes(45), 20)
    assert total == 45
    assert [(a, b) for a, b, _ in chunks] == [(1, 20), (21, 40), (41, 45)]
    assert [len(PdfReader(io.BytesIO(c)).pages) for _, _, c in chunks] == [20, 20, 5]


def test_password_protected_pdf_is_permanent_failure():
    with pytest.raises(TranscribeError) as exc:
        split_pdf(pdf_bytes(2, password="secret"), 20)
    assert exc.value.permanent and "パスワード" in str(exc.value)


def test_owner_password_only_pdf_is_readable():
    total, chunks = split_pdf(pdf_bytes(2, password="owner", owner_only=True), 20)
    assert total == 2 and len(chunks) == 1
    assert not PdfReader(io.BytesIO(chunks[0][2])).is_encrypted


def test_broken_pdf_is_permanent_failure():
    with pytest.raises(TranscribeError) as exc:
        split_pdf(b"%PDF-1.4 broken", 20)
    assert exc.value.permanent


# ---------------------------------------------------------------- 文字起こし（偽の Claude）
class FakeClaude:
    def __init__(self):
        self.stream_calls = []
        self.json_calls = []

    async def stream(self, *, model, messages, system=None, max_tokens=64000, effort=None, output_format=None):
        content = messages[0]["content"]
        pages = len(PdfReader(io.BytesIO(base64.b64decode(content[0]["source"]["data"]))).pages)
        prompt = content[1]["text"]
        self.stream_calls.append((pages, prompt))
        if pages > 2:
            return make_message("途中まで", stop_reason="max_tokens")
        start = int(prompt.split("元の文書の ")[1].split("〜")[0])
        return make_message(f"--- p.{start} ---\n本文{start}")

    async def json_call(self, *, model, system, content, schema, effort=None, max_tokens=16000, streaming=False):
        self.json_calls.append(content)
        if "transcription" in schema["properties"]:
            return {"transcription": "請求書 No.12", "description": "請求書の写真",
                    "summary": ["・請求書の写真", "金額の記載あり", "3行目", "4行目は捨てる"]}
        return {"summary": ["要約A", "要約B"]}


@pytest.mark.anyio
async def test_pdf_chunk_is_split_again_when_output_hits_max_tokens():
    fake = FakeClaude()
    t = Transcriber(fake, "claude-opus-5-5", "medium", pdf_pages_per_chunk=4)
    text = await t.pdf(pdf_bytes(5), "資料.pdf")
    # 4ページの塊は出力上限に達したので 2+2 に分け直し、残り1ページはそのまま
    assert [p for p, _ in fake.stream_calls] == [4, 2, 2, 1]
    assert text.index("本文1") < text.index("本文3") < text.index("本文5")
    assert "途中まで" not in text


@pytest.mark.anyio
async def test_pdf_uses_only_text_after_fallback_boundary():
    class FallbackClaude(FakeClaude):
        async def stream(self, **kwargs):
            return make_message("フォールバック後の本文", fallback=True)

    t = Transcriber(FallbackClaude(), "claude-opus-5-5", "medium", pdf_pages_per_chunk=20)
    assert await t.pdf(pdf_bytes(1), "a.pdf") == "フォールバック後の本文"


@pytest.mark.anyio
async def test_image_transcription_and_summary_cleanup():
    from PIL import Image as _Image

    buf = io.BytesIO()
    _Image.new("RGB", (10, 10)).save(buf, format="PNG")
    fake = FakeClaude()
    t = Transcriber(fake, "claude-opus-5-5", "medium", 20)
    result = await t.image(buf.getvalue())
    assert result.transcription == "請求書 No.12"
    assert result.summary == ["請求書の写真", "金額の記載あり", "3行目"]
    sent = fake.json_calls[0]
    assert sent[0]["type"] == "image" and sent[0]["source"]["media_type"] == "image/png"


@pytest.mark.anyio
async def test_long_text_summary_is_map_reduced():
    fake = FakeClaude()
    t = Transcriber(fake, "claude-opus-5-5", "medium", 20)
    summary = await t.summarize("あ" * 650_000, "大きい.xlsx", "Excel")
    assert summary == ["要約A", "要約B"]
    assert len(fake.json_calls) == 4  # 3つの部分 + まとめ


def test_clean_summary():
    assert clean_summary(["・一行目", "", "二行目<br>続き", "三", "四"]) == ["一行目", "二行目 続き", "三"]
    assert clean_summary(["あ" * 200])[0].endswith("…")


@pytest.mark.parametrize(
    "exc, permanent",
    [
        (TranscribeError("x", permanent=True), True),
        (TranscribeError("x"), False),
        (RefusalError("cyber"), True),
        (ClaudeError("overloaded"), False),
        (LineApiError("取得失敗", 500), False),
        (RuntimeError("?"), False),
    ],
)
def test_classify_failure(exc, permanent):
    reason, perm = classify_failure(exc)
    assert perm is permanent and reason
