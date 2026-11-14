"""/tasks と /cron の呼び出し元の検証。

Cloud Tasks・Cloud Scheduler は、指定したサービスアカウントの OIDC トークン
（Google が署名した ID トークン）を Authorization: Bearer ヘッダーで付けて呼び出す。
ここでは次をすべて確認し、満たさなければ拒否する。

* Google の公開鍵による署名の検証（有効期限を含む）
* 発行者（iss）が Google であること
* 宛先（aud）がこのサービスの URL であること
* メールアドレス（email）が許可したサービスアカウントで、確認済みであること
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx2
from google.auth import exceptions as gauth_exc
from google.auth import jwt as gjwt

log = logging.getLogger(__name__)

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class AuthError(Exception):
    pass


class GoogleOidcVerifier:
    def __init__(self, audience: str, client: httpx2.AsyncClient | None = None) -> None:
        self.audience = audience
        self._client = client or httpx2.AsyncClient(timeout=10.0)
        self._certs: dict[str, str] = {}
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_certs(self, force: bool = False) -> dict[str, str]:
        async with self._lock:
            if not force and self._certs and time.monotonic() < self._expires:
                return self._certs
            resp = await self._client.get(GOOGLE_CERTS_URL)
            resp.raise_for_status()
            self._certs = resp.json()
            max_age = 3600
            m = re.search(r"max-age=(\d+)", resp.headers.get("cache-control", ""))
            if m:
                max_age = int(m.group(1))
            self._expires = time.monotonic() + max(60, min(max_age, 6 * 3600))
            return self._certs

    async def verify(self, authorization: str | None, allowed_emails: set[str]) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("Authorization ヘッダーがありません")
        token = authorization.split(" ", 1)[1].strip()
        if token.count(".") != 2 or token.endswith("SIGNATURE_REMOVED_BY_GOOGLE"):
            raise AuthError("トークンの形式が不正です")
        try:
            header = gjwt.decode_header(token)
        except (ValueError, gauth_exc.GoogleAuthError) as exc:
            raise AuthError("トークンを読めません") from exc
        certs = await self._get_certs()
        if header.get("kid") not in certs:
            certs = await self._get_certs(force=True)  # 鍵の更新に追従する
        try:
            claims = gjwt.decode(token, certs=certs, audience=self.audience, clock_skew_in_seconds=30)
        except (ValueError, gauth_exc.GoogleAuthError) as exc:
            raise AuthError(f"トークンの検証に失敗しました: {exc}") from exc
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise AuthError("発行者が Google ではありません")
        email = claims.get("email")
        if not claims.get("email_verified") or email not in allowed_emails:
            raise AuthError(f"許可されていない呼び出し元です: {email}")
        return claims
