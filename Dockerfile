# LINE Vault Bot（Cloud Run 用）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# git：Vault の浅い複製に使う
# LibreOffice（画面なし版）：旧形式（.doc .xls .ppt）を新形式に変換してからテキストを抽出する
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        libreoffice-impress-nogui \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# root 以外で動かす（書き込みは /tmp のみ）
RUN useradd --system --uid 10001 --home-dir /tmp --shell /usr/sbin/nologin appuser
USER appuser

# Cloud Run は PORT 環境変数で待ち受けポートを指定する
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 8"]
