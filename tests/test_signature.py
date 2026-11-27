"""署名検証のテスト：x-line-signature が不正なら 400、正しければイベントごとに Cloud Tasks へ積んで 200。"""

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from linebot.v3.webhook import SignatureValidator

from app.main import Services, create_app

from .conftest import OWNER_ID, FakeLine, FakeTasks, make_settings
from .helpers import base_event, image_event, ms, owner_text_event, source, text_event, unsend_event

SECRET = "test-channel-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class Unused:
    """このテストでは呼ばれない処理（呼ばれたら失敗させる）。"""

    def __getattr__(self, name):
        raise AssertionError(f"呼ばれるはずのない処理です: {name}")


@pytest.fixture
def tasks():
    return FakeTasks()


@pytest.fixture
def line():
    return FakeLine()


@pytest.fixture
def client(tasks, line):
    settings = make_settings(line_channel_secret=SECRET)

    def factory():
        workers = SimpleNamespace(line=line, ingestor=Unused(), owner=Unused(), reminders=Unused(), vault=None, closers=[])
        return Services(settings=settings, signature=SignatureValidator(SECRET), tasks=tasks,
                        verifier=Unused(), workers=workers)

    with TestClient(create_app(factory)) as c:
        yield c


def post(client, payload: dict, signature: str | None = "auto"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["x-line-signature"] = sign(body)
    elif signature is not None:
        headers["x-line-signature"] = signature
    return client.post("/webhook", content=body, headers=headers)


def test_valid_signature_accepts_and_enqueues(client, tasks):
    event = text_event("1", "こんにちは", ms(2026, 11, 14, 9, 0))
    resp = post(client, {"destination": "Uxxx", "events": [event]})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "tasks": 1}
    assert [(q, path) for q, path, _p, _id in tasks.enqueued] == [("line-ingest", "/tasks/ingest")]
    assert tasks.enqueued[0][2] == {"kind": "event", "event": event}


def test_invalid_signature_is_rejected_with_400(client, tasks):
    resp = post(client, {"events": [text_event("1", "x", ms(2026, 11, 14, 9, 0))]}, signature=sign(b"other body"))
    assert resp.status_code == 400
    assert tasks.enqueued == []


def test_signature_with_wrong_secret_is_rejected(client, tasks):
    body = json.dumps({"events": []}).encode()
    resp = client.post("/webhook", content=body, headers={"x-line-signature": sign(body, "wrong-secret")})
    assert resp.status_code == 400


def test_missing_signature_is_rejected_with_400(client, tasks):
    resp = post(client, {"events": []}, signature=None)
    assert resp.status_code == 400


def test_tampered_body_is_rejected(client, tasks):
    payload = {"events": [text_event("1", "元の本文", ms(2026, 11, 14, 9, 0))]}
    body = json.dumps(payload, ensure_ascii=False).encode()
    good = sign(body)
    tampered = body.replace("元の本文".encode(), "改ざん".encode())
    resp = client.post("/webhook", content=tampered, headers={"x-line-signature": good})
    assert resp.status_code == 400
    assert tasks.enqueued == []


def test_verify_request_with_empty_events_returns_200(client, tasks):
    resp = post(client, {"destination": "Uxxx", "events": []})
    assert resp.status_code == 200
    assert tasks.enqueued == []


def test_owner_text_goes_to_both_queues(client, tasks, line):
    event = owner_text_event("先週の会議の結論は？", ms(2026, 11, 14, 9, 0))
    post(client, {"events": [event]})
    assert sorted(q for q, *_ in tasks.enqueued) == ["line-ask", "line-ingest"]
    ask = [p for q, _path, p, _id in tasks.enqueued if q == "line-ask"][0]
    assert ask == {"event": event}
    assert line.loading == [OWNER_ID]  # 受け付けた時点でローディング表示を出す


def test_no_loading_for_group_messages(client, line):
    post(client, {"events": [text_event("4", "x", ms(2026, 11, 14, 9, 0))]})
    assert line.loading == []


def test_other_users_one_to_one_is_recorded_but_not_answered(client, tasks):
    event = text_event("2", "質問です", ms(2026, 11, 14, 9, 0), source(kind="user", user_id="U" + "d" * 32))
    post(client, {"events": [event]})
    assert [q for q, *_ in tasks.enqueued] == ["line-ingest"]


def test_owner_message_in_group_is_not_answered(client, tasks):
    event = text_event("3", "グループでの発言", ms(2026, 11, 14, 9, 0), source(kind="group", user_id=OWNER_ID))
    post(client, {"events": [event]})
    assert [q for q, *_ in tasks.enqueued] == ["line-ingest"]


def test_each_event_becomes_its_own_task(client, tasks):
    events = [
        text_event("10", "a", ms(2026, 11, 14, 9, 0)),
        image_event("11", ms(2026, 11, 14, 9, 1)),
        unsend_event("10", ms(2026, 11, 14, 9, 2)),
        base_event("join", ms(2026, 11, 14, 9, 3), source(user_id=None)),
        base_event("leave", ms(2026, 11, 14, 9, 4), source(user_id=None)),
        base_event("follow", ms(2026, 11, 14, 9, 5), source(kind="user")),
    ]
    resp = post(client, {"events": events})
    assert resp.json()["tasks"] == 4  # leave と follow は記録しない
    assert all(q == "line-ingest" for q, *_ in tasks.enqueued)


def test_redelivered_webhook_does_not_create_duplicate_tasks(client, tasks):
    event = text_event("20", "再送テスト", ms(2026, 11, 14, 9, 0))
    post(client, {"events": [event]})
    redelivered = dict(event, deliveryContext={"isRedelivery": True})
    post(client, {"events": [redelivered]})
    assert len(tasks.enqueued) == 1  # 同じ webhookEventId からは同じタスク名になる


def test_enqueue_failure_returns_500_so_line_can_redeliver(client, tasks):
    tasks.fail = True
    resp = post(client, {"events": [text_event("30", "x", ms(2026, 11, 14, 9, 0))]})
    assert resp.status_code == 500


def test_invalid_json_with_valid_signature_is_400(client):
    body = b"{not json"
    resp = client.post("/webhook", content=body, headers={"x-line-signature": sign(body)})
    assert resp.status_code == 400


def test_sdk_validator_matches_line_algorithm():
    body = '{"events":[]}'
    assert SignatureValidator(SECRET).validate(body, sign(body.encode()))
    assert not SignatureValidator(SECRET).validate(body, sign(b'{"events":[1]}'))
