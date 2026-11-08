"""The local knowledge base over HTTP, on Bedrock's own REST routes.

Point stock boto3 at it and the same code that talks to AWS talks to this:

    agent = boto3.client("bedrock-agent", endpoint_url="http://127.0.0.1:8099")
    runtime = boto3.client("bedrock-agent-runtime", endpoint_url="http://127.0.0.1:8099")

Routes, methods and success codes are the ones in botocore's service models (rest-json: PUT
creates, POST lists, DELETE answers 202). The SigV4 signature boto3 sends is accepted and not
checked — any credentials will do. Errors come back the way botocore parses them: the code in
``x-amzn-ErrorType``, the message in the body, so ``client.exceptions.ValidationException`` and
friends are raised as on AWS.

A background thread runs queued ingestion jobs, since on AWS the service runs them itself;
``--watch`` also syncs on bucket changes (see trigger.py).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from aws_ai.local_kb import cli, config
from aws_ai.local_kb.control import ControlPlane, open_control_plane
from aws_ai.local_kb.errors import BedrockAgentError, ValidationException
from aws_ai.local_kb.generate import retrieve_and_generate
from aws_ai.local_kb.ingest import Services, run_pending
from aws_ai.local_kb.retrieve import retrieve
from aws_ai.local_kb.services import purger

KB = "/knowledgebases/{knowledgeBaseId}"
DS = KB + "/datasources/{dataSourceId}"


def _json(body: Any, status: int = 200) -> Response:
    def iso(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(type(value).__name__)

    return Response(json.dumps(body, default=iso), status, media_type="application/json")


def _require(body: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in body]
    if missing:
        raise ValidationException(f"Missing required parameter(s): {', '.join(missing)}.")


def create_app(services: Services, state_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="local-kb", docs_url=None, redoc_url=None)

    def cp() -> ControlPlane:
        # One connection per request: sqlite3 connections don't cross threads.
        return open_control_plane(state_dir)

    async def body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationException("The request body is not valid JSON.") from exc
        return parsed

    @app.exception_handler(BedrockAgentError)
    async def aws_error(_: Request, exc: BedrockAgentError) -> JSONResponse:
        return JSONResponse(
            {"message": exc.message}, exc.status, headers={"x-amzn-ErrorType": exc.code}
        )

    # ------------------------------------------------------------ bedrock-agent

    @app.put("/knowledgebases/")
    async def create_knowledge_base(request: Request) -> Response:
        b = await body(request)
        _require(b, "name", "roleArn", "knowledgeBaseConfiguration")
        kb = cp().create_knowledge_base(
            name=b["name"],
            role_arn=b["roleArn"],
            description=b.get("description"),
            knowledge_base_configuration=b["knowledgeBaseConfiguration"],
            storage_configuration=b.get("storageConfiguration")
            or cli.default_storage_configuration(config.INDEX_NAME),
        )
        return _json({"knowledgeBase": kb}, 202)

    @app.post("/knowledgebases/")
    async def list_knowledge_bases() -> Response:
        return _json({"knowledgeBaseSummaries": cp().list_knowledge_bases()})

    @app.get(KB)
    async def get_knowledge_base(knowledgeBaseId: str) -> Response:  # noqa: N803
        return _json({"knowledgeBase": cp().get_knowledge_base(knowledgeBaseId)})

    @app.delete(KB)
    def delete_knowledge_base(knowledgeBaseId: str) -> Response:  # noqa: N803
        plane = cp()
        return _json(plane.delete_knowledge_base(knowledgeBaseId, purger(plane, services)), 202)

    @app.put(KB + "/datasources/")
    async def create_data_source(knowledgeBaseId: str, request: Request) -> Response:  # noqa: N803
        b = await body(request)
        _require(b, "name", "dataSourceConfiguration")
        ds = cp().create_data_source(
            knowledge_base_id=knowledgeBaseId,
            name=b["name"],
            description=b.get("description"),
            data_source_configuration=b["dataSourceConfiguration"],
            vector_ingestion_configuration=b.get("vectorIngestionConfiguration"),
            data_deletion_policy=b.get("dataDeletionPolicy", "DELETE"),
        )
        return _json({"dataSource": ds})

    @app.post(KB + "/datasources/")
    async def list_data_sources(knowledgeBaseId: str) -> Response:  # noqa: N803
        return _json({"dataSourceSummaries": cp().list_data_sources(knowledgeBaseId)})

    @app.get(DS)
    async def get_data_source(knowledgeBaseId: str, dataSourceId: str) -> Response:  # noqa: N803
        return _json({"dataSource": cp().get_data_source(knowledgeBaseId, dataSourceId)})

    @app.delete(DS)
    def delete_data_source(knowledgeBaseId: str, dataSourceId: str) -> Response:  # noqa: N803
        plane = cp()
        result = plane.delete_data_source(knowledgeBaseId, dataSourceId, purger(plane, services))
        return _json(result, 202)

    @app.put(DS + "/ingestionjobs/")
    async def start_ingestion_job(
        knowledgeBaseId: str,  # noqa: N803
        dataSourceId: str,  # noqa: N803
        request: Request,
    ) -> Response:
        b = await body(request)
        job = cp().start_ingestion_job(knowledgeBaseId, dataSourceId, b.get("description"))
        return _json({"ingestionJob": job}, 202)

    @app.post(DS + "/ingestionjobs/")
    async def list_ingestion_jobs(
        knowledgeBaseId: str,  # noqa: N803
        dataSourceId: str,  # noqa: N803
        request: Request,
    ) -> Response:
        b = await body(request)
        statuses = [
            v
            for f in b.get("filters", [])
            if f.get("attribute") == "STATUS"
            for v in f.get("values", [])
        ] or None
        ascending = b.get("sortBy", {}).get("order") == "ASCENDING"
        jobs = cp().list_ingestion_jobs(
            knowledgeBaseId, dataSourceId, statuses=statuses, ascending=ascending
        )
        return _json({"ingestionJobSummaries": jobs})

    @app.get(DS + "/ingestionjobs/{ingestionJobId}")
    async def get_ingestion_job(
        knowledgeBaseId: str,  # noqa: N803
        dataSourceId: str,  # noqa: N803
        ingestionJobId: str,  # noqa: N803
    ) -> Response:
        job = cp().get_ingestion_job(knowledgeBaseId, dataSourceId, ingestionJobId)
        return _json({"ingestionJob": job})

    @app.post(DS + "/ingestionjobs/{ingestionJobId}/stop")
    async def stop_ingestion_job(
        knowledgeBaseId: str,  # noqa: N803
        dataSourceId: str,  # noqa: N803
        ingestionJobId: str,  # noqa: N803
    ) -> Response:
        job = cp().stop_ingestion_job(knowledgeBaseId, dataSourceId, ingestionJobId)
        return _json({"ingestionJob": job}, 202)

    # ---------------------------------------------------- bedrock-agent-runtime

    # Embedding, search and generation block, so they run in the thread pool — each with its
    # own control-plane connection, opened in that thread.

    @app.post(KB + "/retrieve")
    async def retrieve_route(knowledgeBaseId: str, request: Request) -> Response:  # noqa: N803
        b = await body(request)
        _require(b, "retrievalQuery")
        result = await run_in_threadpool(
            lambda: retrieve(
                cp(),
                services,
                knowledgeBaseId,
                b["retrievalQuery"],
                b.get("retrievalConfiguration"),
            )
        )
        return _json(result)

    @app.post("/retrieveAndGenerate")
    async def retrieve_and_generate_route(request: Request) -> Response:
        b = await body(request)
        _require(b, "input", "retrieveAndGenerateConfiguration")
        result = await run_in_threadpool(
            lambda: retrieve_and_generate(
                cp(),
                services,
                b["input"],
                b["retrieveAndGenerateConfiguration"],
                b.get("sessionId"),
            )
        )
        return _json(result)

    return app


def start_worker(
    services: Services,
    state_dir: Path | None,
    watch: bool,
    log: Callable[[str], None],
) -> threading.Thread:
    """Run queued ingestion jobs in the background — and, with ``watch``, sync on bucket
    changes too."""

    def loop() -> None:
        plane = open_control_plane(state_dir)
        if watch:
            from aws_ai.local_kb.trigger import watch as watch_loop

            watch_loop(plane, services, log=log)
            return
        while True:
            for job in run_pending(plane, services):
                log(f"ingestion job {job['ingestionJobId']} {job['status']}")
            time.sleep(2)

    thread = threading.Thread(target=loop, name="local-kb-worker", daemon=True)
    thread.start()
    return thread
