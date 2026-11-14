"""FastAPI アプリ本体（エンドポイントの定義）。

POST /webhook          LINE からの Webhook。署名を検証してイベントごとに Cloud Tasks へ積む。
POST /tasks/ingest     記録処理（Cloud Tasks からのみ）
POST /tasks/ask        質問回答・オーナーの指示の処理（Cloud Tasks からのみ）
POST /cron/reminders   リマインド送信（Cloud Scheduler からのみ、5分おき）
GET  /health           動作確認用

LINE の Webhook は2秒以内に応答しないと時間切れになる。起動直後でも間に合うよう、
このモジュールでは Webhook の処理に必要な軽い部品だけを読み込み、Claude や LINE API の
クライアントなどの重い部品（app.workers）は起動後に裏で読み込む。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from linebot.v3.webhook import SignatureValidator

from .auth import AuthError, GoogleOidcVerifier
from .config import Settings
from .errors import RetryLater
from .github_store import ForbiddenPathError
from .logging_setup import setup_logging
from .tasks_client import TasksClient, task_id_for

log = logging.getLogger(__name__)

RECORD_EVENT_TYPES = {"message", "unsend", "messageEdited"}
LOADING_TIMEOUT_SECONDS = 0.8


@dataclass
class Services:
    settings: Settings
    signature: SignatureValidator
    tasks: Any
    verifier: Any
    workers: Any = None  # app.workers.Workers（未指定なら初回に組み立てる）
    closers: list[Callable[[], Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def workers_ready(self) -> bool:
        return self.workers is not None

    async def get_workers(self):
        """重い部品を（まだなら）読み込んで組み立てる。失敗したら次の呼び出しでやり直す。"""
        if self.workers is not None:
            return self.workers
        async with self._lock:
            if self.workers is None:
                # import は時間がかかるので別スレッドで行い、その間も Webhook に応答できるようにする
                module = await asyncio.to_thread(importlib.import_module, "app.workers")
                workers = module.build_workers(self.settings, self.tasks)
                self.closers.extend(workers.closers)
                self.workers = workers
        return self.workers

    async def warm_up(self) -> None:
        workers = await self.get_workers()
        if workers.vault is not None:
            await workers.vault.ensure_cloned()  # 起動時に Vault を浅く複製しておく

    async def aclose(self) -> None:
        for close in self.closers:
            try:
                await close()
            except Exception:  # pragma: no cover
                log.exception("終了処理に失敗しました")


def build_services(settings: Settings) -> Services:
    tasks = TasksClient(settings.gcp_project_id, settings.tasks_location, settings.service_url,
                        settings.tasks_invoker_sa, settings.ingest_queue, settings.ask_queue,
                        settings.task_dispatch_deadline_seconds)
    verifier = GoogleOidcVerifier(settings.service_url)
    return Services(
        settings=settings,
        signature=SignatureValidator(settings.line_channel_secret),
        tasks=tasks,
        verifier=verifier,
        closers=[verifier.aclose],
    )


def plan_tasks(event: dict, owner_user_id: str) -> list[tuple[str, dict, str]]:
    """1つのイベントから積むタスクを決める。戻り値：[(キュー種別, ペイロード, タスクID)]"""
    etype = event.get("type")
    source = event.get("source") or {}
    stype = source.get("type")
    event_id = event.get("webhookEventId") or task_id_for(json.dumps(event, sort_keys=True))
    jobs: list[tuple[str, dict, str]] = []
    # 記録：メッセージ・送信取消・編集。グループのその他のイベントは、グループ一覧への登録のため
    if etype in RECORD_EVENT_TYPES or (stype == "group" and etype != "leave"):
        jobs.append(("ingest", {"kind": "event", "event": event}, task_id_for(event_id, "ingest")))
    # 質問回答・指示：オーナーとの1対1のメッセージだけ
    if etype == "message" and stype == "user" and source.get("userId") == owner_user_id:
        jobs.append(("ask", {"event": event}, task_id_for(event_id, "ask")))
    return jobs


async def _run_background(coro, what: str) -> None:
    try:
        await coro
    except Exception:
        log.exception("%s に失敗しました", what)


async def _show_loading_quickly(line, user_id: str) -> None:
    """Webhook の応答を遅らせないよう、短い時間で打ち切る（表示は質問処理でも出す）。"""
    try:
        await asyncio.wait_for(line.show_loading(user_id, 60), timeout=LOADING_TIMEOUT_SECONDS)
    except Exception:
        pass


def create_app(services_factory: Callable[[], Services] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services_factory is None:
            setup_logging()
            services = build_services(Settings.from_env())
        else:
            services = services_factory()
        app.state.services = services
        # 重い部品の読み込みと Vault の浅い複製を裏で始める（Webhook の応答は待たせない）
        app.state.warm_up = asyncio.create_task(_run_background(services.warm_up(), "起動準備"))
        yield
        app.state.warm_up.cancel()
        await services.aclose()

    app = FastAPI(title="LINE Vault Bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def svc(request: Request) -> Services:
        return request.app.state.services

    async def _verify(request: Request, allowed: set[str]) -> None:
        try:
            await svc(request).verifier.verify(request.headers.get("authorization"), allowed)
        except AuthError as exc:
            log.warning("呼び出しを拒否しました（%s）: %s", request.url.path, exc)
            raise HTTPException(status_code=403, detail="forbidden") from None

    async def require_tasks(request: Request) -> None:
        await _verify(request, {svc(request).settings.tasks_invoker_sa})

    async def require_scheduler(request: Request) -> None:
        await _verify(request, {svc(request).settings.scheduler_invoker_sa})

    @app.get("/health")
    async def health(request: Request) -> dict:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> dict:
        services = svc(request)
        body = await request.body()
        signature = request.headers.get("x-line-signature")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="invalid body") from None
        if not signature or not services.signature.validate(text, signature):
            log.warning("署名が不正な Webhook を拒否しました")
            raise HTTPException(status_code=400, detail="invalid signature")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json") from None
        events = payload.get("events") or []
        owner_id = services.settings.owner_line_user_id
        jobs = [job for ev in events if isinstance(ev, dict) for job in plan_tasks(ev, owner_id)]
        if jobs:
            enqueues = [
                services.tasks.enqueue_ingest(p, tid) if kind == "ingest" else services.tasks.enqueue_ask(p, tid)
                for kind, p, tid in jobs
            ]
            # オーナーの質問・指示を受け付けたら、すぐにローディング表示を出す
            # （起動直後で LINE API の部品がまだ読み込み中なら、質問処理の側で出す）
            wants_loading = services.workers_ready and any(
                kind == "ask" and (p["event"].get("message") or {}).get("type") == "text" for kind, p, _ in jobs
            )
            loading = [_show_loading_quickly(services.workers.line, owner_id)] if wants_loading else []
            results = await asyncio.gather(*enqueues, *loading, return_exceptions=True)
            errors = [r for r in results[: len(enqueues)] if isinstance(r, BaseException)]
            if errors:
                log.error("Cloud Tasks への登録に失敗しました: %s", errors[0])
                # 500 を返すと、Webhook の再送を有効にしていれば LINE が再送する（重複はタスク名で防ぐ）
                raise HTTPException(status_code=500, detail="enqueue failed")
        return {"ok": True, "tasks": len(jobs)}

    @app.post("/tasks/ingest", dependencies=[Depends(require_tasks)])
    async def tasks_ingest(request: Request):
        payload = await request.json()
        try:
            workers = await svc(request).get_workers()
            status = await workers.ingestor.handle(payload)
            return {"status": status}
        except RetryLater as exc:
            # 「テキスト化失敗」を記録した上で、Cloud Tasks に再試行させる
            return JSONResponse({"status": "retry", "reason": str(exc)}, status_code=503)
        except ForbiddenPathError:
            log.exception("書き込み禁止のパスへの書き込みを止めました")
            return {"status": "forbidden_path"}
        except Exception as exc:
            log.exception("記録処理に失敗しました")
            return JSONResponse({"status": "error", "reason": type(exc).__name__}, status_code=500)

    @app.post("/tasks/ask", dependencies=[Depends(require_tasks)])
    async def tasks_ask(request: Request) -> dict:
        payload = await request.json()
        try:
            workers = await svc(request).get_workers()
            status = await workers.owner.handle(payload.get("event") or {})
        except Exception:
            # 回答の途中で失敗しても、再試行で二重に返信しないよう 200 を返す
            log.exception("質問回答の処理に失敗しました")
            status = "error"
        return {"status": status}

    @app.post("/cron/reminders", dependencies=[Depends(require_scheduler)])
    async def cron_reminders(request: Request):
        try:
            workers = await svc(request).get_workers()
            return await workers.reminders.run()
        except Exception as exc:
            log.exception("リマインドの処理に失敗しました")
            return JSONResponse({"status": "error", "reason": type(exc).__name__}, status_code=500)

    return app


app = create_app()
