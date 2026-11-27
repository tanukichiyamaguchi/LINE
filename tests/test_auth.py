"""/tasks と /cron の呼び出し元検証のテスト（Cloud Tasks・Cloud Scheduler のサービスアカウント以外は拒否）。

テスト用の RSA 鍵で署名したトークンと、その公開鍵を返す偽の証明書エンドポイントを使う。
"""

import datetime as dt
import time
from types import SimpleNamespace

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt
from linebot.v3.webhook import SignatureValidator

from app.auth import GOOGLE_CERTS_URL, AuthError, GoogleOidcVerifier
from app.main import Services, create_app

from .conftest import make_settings

AUD = "https://bot.example.run.app"
TASKS_SA = "tasks@test-project.iam.gserviceaccount.com"
SCHED_SA = "scheduler@test-project.iam.gserviceaccount.com"


def _make_key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return pem_key, cert.public_bytes(serialization.Encoding.PEM).decode()


PEM_KEY, PEM_CERT = _make_key_and_cert()
OTHER_KEY, _ = _make_key_and_cert()
KID = "test-kid-1"


def make_token(email=TASKS_SA, aud=AUD, iss="https://accounts.google.com", exp_delta=600, verified=True,
               key=PEM_KEY, kid=KID):
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "email": email, "email_verified": verified,
               "iat": now, "exp": now + exp_delta, "sub": "123"}
    signer = crypt.RSASigner.from_string(key, key_id=kid)
    return jwt.encode(signer, payload).decode()


def certs_client(calls: list | None = None) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == GOOGLE_CERTS_URL
        if calls is not None:
            calls.append(1)
        return httpx2.Response(200, json={KID: PEM_CERT}, headers={"cache-control": "public, max-age=3600"})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


@pytest.fixture
def verifier():
    return GoogleOidcVerifier(AUD, client=certs_client())


@pytest.mark.anyio
async def test_valid_tasks_token_is_accepted(verifier):
    claims = await verifier.verify("Bearer " + make_token(), {TASKS_SA})
    assert claims["email"] == TASKS_SA


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kwargs, allowed",
    [
        ({"email": "attacker@example.com"}, {TASKS_SA}),
        ({"email": SCHED_SA}, {TASKS_SA}),  # Scheduler のトークンで /tasks は呼べない
        ({"aud": "https://other.example.com"}, {TASKS_SA}),
        ({"iss": "https://evil.example.com"}, {TASKS_SA}),
        ({"exp_delta": -3600}, {TASKS_SA}),
        ({"verified": False}, {TASKS_SA}),
        ({"key": OTHER_KEY}, {TASKS_SA}),  # 別の鍵で署名された偽トークン
    ],
)
async def test_invalid_tokens_are_rejected(verifier, kwargs, allowed):
    with pytest.raises(AuthError):
        await verifier.verify("Bearer " + make_token(**kwargs), allowed)


@pytest.mark.anyio
@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer not-a-jwt", "Bearer a.b.SIGNATURE_REMOVED_BY_GOOGLE"])
async def test_missing_or_malformed_header_is_rejected(verifier, header):
    with pytest.raises(AuthError):
        await verifier.verify(header, {TASKS_SA})


@pytest.mark.anyio
async def test_certs_are_cached():
    calls: list = []
    v = GoogleOidcVerifier(AUD, client=certs_client(calls))
    for _ in range(3):
        await v.verify("Bearer " + make_token(), {TASKS_SA})
    assert len(calls) == 1


class Recorder:
    def __init__(self):
        self.calls = []

    async def handle(self, payload):
        self.calls.append(payload)
        return "ok"

    async def run(self):
        self.calls.append("run")
        return {"checked": 0, "results": []}


@pytest.fixture
def app_client():
    settings = make_settings()
    rec = Recorder()

    def factory():
        workers = SimpleNamespace(line=None, ingestor=rec, owner=rec, reminders=rec, vault=None, closers=[])
        return Services(settings=settings, signature=SignatureValidator(settings.line_channel_secret), tasks=None,
                        verifier=GoogleOidcVerifier(AUD, client=certs_client()), workers=workers)

    with TestClient(create_app(factory)) as c:
        yield c, rec


@pytest.mark.parametrize("path", ["/tasks/ingest", "/tasks/ask", "/cron/reminders"])
def test_endpoints_reject_requests_without_token(app_client, path):
    client, rec = app_client
    assert client.post(path, json={}).status_code == 403
    assert rec.calls == []


def test_tasks_accept_only_tasks_service_account(app_client):
    client, rec = app_client
    ok = client.post("/tasks/ingest", json={"kind": "event"}, headers={"Authorization": "Bearer " + make_token(TASKS_SA)})
    assert ok.status_code == 200
    ng = client.post("/tasks/ingest", json={}, headers={"Authorization": "Bearer " + make_token(SCHED_SA)})
    assert ng.status_code == 403
    assert rec.calls == [{"kind": "event"}]


def test_cron_accepts_only_scheduler_service_account(app_client):
    client, rec = app_client
    ok = client.post("/cron/reminders", headers={"Authorization": "Bearer " + make_token(SCHED_SA)})
    assert ok.status_code == 200 and rec.calls == ["run"]
    ng = client.post("/cron/reminders", headers={"Authorization": "Bearer " + make_token(TASKS_SA)})
    assert ng.status_code == 403
