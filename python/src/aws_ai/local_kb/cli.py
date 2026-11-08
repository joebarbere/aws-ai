"""``local-kb``: the ``aws bedrock-agent`` commands, against the local control plane.

The verbs, flags and JSON output follow the AWS CLI, so a command from the cloud module's
README works here with ``aws bedrock-agent`` swapped for ``local-kb``:

    local-kb create-knowledge-base --name study-kb
    local-kb create-data-source --knowledge-base-id ABCDEFGHIJ --name docs
    local-kb start-ingestion-job --knowledge-base-id ABCDEFGHIJ --data-source-id KLMNOPQRST
    local-kb get-ingestion-job --knowledge-base-id ... --data-source-id ... --ingestion-job-id ...

Three commands have no AWS CLI equivalent, because on AWS the service runs jobs itself:
``sync`` starts a job and runs it now (the console's Sync button), ``run-pending`` runs every
queued job, and ``watch`` syncs on bucket changes (the S3 event → Lambda wiring).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from aws_ai.local_kb import config
from aws_ai.local_kb.control import ControlPlane, open_control_plane
from aws_ai.local_kb.errors import BedrockAgentError


def default_knowledge_base_configuration(dimensions: int) -> dict[str, Any]:
    return {
        "type": "VECTOR",
        "vectorKnowledgeBaseConfiguration": {
            "embeddingModelArn": config.EMBEDDING_MODEL_ARN,
            "embeddingModelConfiguration": {
                "bedrockEmbeddingModelConfiguration": {"dimensions": dimensions}
            },
        },
    }


def default_storage_configuration(index_name: str) -> dict[str, Any]:
    """The local OpenSearch, described the way Bedrock describes a managed cluster."""
    return {
        "type": "OPENSEARCH_MANAGED_CLUSTER",
        "opensearchManagedClusterConfiguration": {
            "domainEndpoint": config.OPENSEARCH_ENDPOINT,
            "domainArn": f"arn:aws:es:{config.REGION}:{config.ACCOUNT_ID}:domain/local-kb",
            "vectorIndexName": index_name,
            "fieldMapping": {
                "vectorField": config.VECTOR_FIELD,
                "textField": config.TEXT_FIELD,
                "metadataField": config.METADATA_FIELD,
            },
        },
    }


def _json_arg(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc


def _services() -> Any:
    # Imported late: these need the running stack and the `local` extra, which the plain
    # control-plane commands do not.
    from aws_ai.local_kb.services import local_services

    return local_services()


def _purge(cp: ControlPlane) -> Callable[[dict[str, Any]], None]:
    from aws_ai.local_kb.services import purger

    return purger(cp, _services())  # type: ignore[no-any-return]


def _sync(cp: ControlPlane, kb: str, ds: str) -> dict[str, Any]:
    from aws_ai.local_kb.ingest import run_job

    return {"ingestionJob": run_job(cp, cp.start_ingestion_job(kb, ds), _services())}


def _run_pending(cp: ControlPlane) -> dict[str, Any]:
    from aws_ai.local_kb.ingest import run_pending

    return {"ingestionJobs": run_pending(cp, _services())}


def _watch(cp: ControlPlane, poll_seconds: float, debounce: float) -> None:
    from aws_ai.local_kb.trigger import watch

    print(f"watching kb-events and polling ETags every {poll_seconds:g}s; Ctrl-C to stop")
    try:
        watch(cp, _services(), poll_seconds=poll_seconds, debounce=debounce)
    except KeyboardInterrupt:
        pass


def _commands(cp: ControlPlane, args: argparse.Namespace) -> dict[str, Callable[[], Any]]:
    # Each command declares only the ids it needs; argparse requires those, so "" never reaches
    # the control plane.
    kb: str = getattr(args, "knowledge_base_id", "")
    ds: str = getattr(args, "data_source_id", "")
    job: str = getattr(args, "ingestion_job_id", "")
    return {
        "create-knowledge-base": lambda: {
            "knowledgeBase": cp.create_knowledge_base(
                name=args.name,
                description=args.description,
                knowledge_base_configuration=args.knowledge_base_configuration
                or default_knowledge_base_configuration(args.dimensions),
                storage_configuration=args.storage_configuration
                or default_storage_configuration(args.index_name),
            )
        },
        "get-knowledge-base": lambda: {"knowledgeBase": cp.get_knowledge_base(kb)},
        "list-knowledge-bases": lambda: {"knowledgeBaseSummaries": cp.list_knowledge_bases()},
        "delete-knowledge-base": lambda: cp.delete_knowledge_base(kb, _purge(cp)),
        "create-data-source": lambda: {
            "dataSource": cp.create_data_source(
                knowledge_base_id=kb,
                name=args.name,
                description=args.description,
                data_source_configuration=args.data_source_configuration
                or {
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": f"arn:aws:s3:::{args.bucket}",
                        "inclusionPrefixes": [args.prefix],
                    },
                },
                vector_ingestion_configuration=args.vector_ingestion_configuration,
                data_deletion_policy=args.data_deletion_policy,
            )
        },
        "get-data-source": lambda: {"dataSource": cp.get_data_source(kb, ds)},
        "list-data-sources": lambda: {"dataSourceSummaries": cp.list_data_sources(kb)},
        "delete-data-source": lambda: cp.delete_data_source(kb, ds, _purge(cp)),
        "start-ingestion-job": lambda: {
            "ingestionJob": cp.start_ingestion_job(kb, ds, args.description)
        },
        "get-ingestion-job": lambda: {"ingestionJob": cp.get_ingestion_job(kb, ds, job)},
        "list-ingestion-jobs": lambda: {"ingestionJobSummaries": cp.list_ingestion_jobs(kb, ds)},
        "stop-ingestion-job": lambda: {"ingestionJob": cp.stop_ingestion_job(kb, ds, job)},
        "sync": lambda: _sync(cp, kb, ds),
        "run-pending": lambda: _run_pending(cp),
        "watch": lambda: _watch(cp, args.poll_seconds, args.debounce),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-kb", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--state-dir", type=Path, help="Control-plane store (default: local-kb/.state)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, *ids: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        for flag in ids:
            p.add_argument(f"--{flag}", required=True)
        return p

    p = command("create-knowledge-base")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--dimensions", type=int, default=config.EMBEDDING_DIMENSIONS)
    p.add_argument("--index-name", default=config.INDEX_NAME)
    p.add_argument("--knowledge-base-configuration", type=_json_arg, help="JSON, as in AWS")
    p.add_argument("--storage-configuration", type=_json_arg, help="JSON, as in AWS")

    command("get-knowledge-base", "knowledge-base-id")
    command("list-knowledge-bases")
    command("delete-knowledge-base", "knowledge-base-id")

    p = command("create-data-source", "knowledge-base-id")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--bucket", default=config.DEFAULT_BUCKET)
    p.add_argument("--prefix", default=config.DEFAULT_PREFIX)
    p.add_argument("--data-source-configuration", type=_json_arg, help="JSON, as in AWS")
    p.add_argument(
        "--vector-ingestion-configuration",
        type=_json_arg,
        help='JSON, e.g. {"chunkingConfiguration": {"chunkingStrategy": "NONE"}}',
    )
    p.add_argument("--data-deletion-policy", choices=["DELETE", "RETAIN"], default="DELETE")

    command("get-data-source", "knowledge-base-id", "data-source-id")
    command("list-data-sources", "knowledge-base-id")
    command("delete-data-source", "knowledge-base-id", "data-source-id")
    command("start-ingestion-job", "knowledge-base-id", "data-source-id").add_argument(
        "--description"
    )
    command("get-ingestion-job", "knowledge-base-id", "data-source-id", "ingestion-job-id")
    command("list-ingestion-jobs", "knowledge-base-id", "data-source-id")
    command("stop-ingestion-job", "knowledge-base-id", "data-source-id", "ingestion-job-id")
    command("sync", "knowledge-base-id", "data-source-id")
    command("run-pending")
    p = command("watch")
    p.add_argument("--poll-seconds", type=float, default=60, help="ETag poll; 0 turns it off")
    p.add_argument("--debounce", type=float, default=10, help="Quiet seconds before syncing")
    return parser


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cp = open_control_plane(args.state_dir)
    try:
        result = _commands(cp, args)[args.command]()
    except BedrockAgentError as exc:
        # The AWS CLI's error line, so scripts that grep for it behave the same.
        print(
            f"An error occurred ({exc.code}) when calling the "
            f"{''.join(w.title() for w in args.command.split('-'))} operation: {exc.message}",
            file=sys.stderr,
        )
        return 254
    if result is not None:
        print(json.dumps(result, indent=4, default=_iso))
    return 0


if __name__ == "__main__":
    sys.exit(main())
