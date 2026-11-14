"""起動時間に関するテスト。

LINE の Webhook は2秒以内に応答する必要があるため、app.main の読み込みでは
Claude（anthropic）や LINE API クライアントなどの重い部品を読み込まないこと、
それらは初回利用時（または起動後の裏での準備）に組み立てられることを確かめる。
"""

import subprocess
import sys

import pytest
from linebot.v3.webhook import SignatureValidator

from app.main import Services

from .conftest import FakeTasks, make_settings


def test_main_module_does_not_import_heavy_libraries():
    code = (
        "import sys, app.main\n"
        "heavy = [m for m in ('anthropic', 'linebot.v3.messaging', 'docx', 'openpyxl', 'pptx', 'pypdf', 'PIL') if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=".")
    assert out.stdout.strip() == ""


@pytest.mark.anyio
async def test_workers_are_built_on_first_use(tmp_path):
    settings = make_settings(vault_dir=str(tmp_path / "vault"))
    services = Services(settings=settings, signature=SignatureValidator(settings.line_channel_secret),
                        tasks=FakeTasks(), verifier=None)
    assert not services.workers_ready
    workers = await services.get_workers()
    assert services.workers_ready
    assert await services.get_workers() is workers  # 2回目以降は同じもの
    assert workers.ingestor is not None and workers.owner is not None and workers.reminders is not None
    assert len(services.closers) == 3
    await services.aclose()
