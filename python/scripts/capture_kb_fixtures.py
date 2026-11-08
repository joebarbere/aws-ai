#!/usr/bin/env python3
"""Phase 0 of local-kb/PLAN.md: record what a real Bedrock knowledge base writes and returns,
so the local implementation can be tested against it instead of against guesses.

Run it after the three-step apply in terraform/modules/knowledge_base/README.md. It uploads
local-kb/sample-docs/kb/ under the data source prefix, syncs, then saves to local-kb/fixtures/:

    knowledge-base.json, data-source.json   control-plane defaults Bedrock filled in
    ingestion-job-1.json                    statistics for the first sync
    index-mapping.json                      _mapping, i.e. what dynamic mapping added
    index-documents.json                    every stored chunk, vector field dropped
    retrieve-*.json                         raw Retrieve responses: semantic, hybrid, filters
    rag-*.json                              raw RetrieveAndGenerate responses
    ingestion-job-2.json                    second sync after one edit, one delete, one
                                            metadata-only change
    index-documents-after-mutation.json
    invocation-logs.json                    the rendered prompts Bedrock sent to the models

The account id is replaced with 000000000000 in everything written, so fixtures can be committed.

``--store rds`` does the same against an Aurora PostgreSQL knowledge base
(terraform/modules/knowledge_base_rds + rds_kb.py) and writes to local-kb/fixtures/rds/. There
is no mapping there; ``table-schema.json`` holds the columns and indexes instead, rows are read
through the RDS Data API, and ``postgresql-statements.json`` holds every SQL statement the
cluster ran — Bedrock's inserts, searches and filters, verbatim.

    uv run python scripts/capture_kb_fixtures.py --knowledge-base-id ABCDEFGHIJ \\
        --data-source-id KLMNOPQRST --endpoint https://abc.us-east-1.aoss.amazonaws.com \\
        --bucket aws-ai-study-data-123 --log-group /aws-ai-study/ai

    uv run python scripts/capture_kb_fixtures.py --store rds --knowledge-base-id ... \
        --data-source-id ... --cluster-arn arn:aws:rds:...:cluster:aws-ai-study-kb-rds \
        --secret-arn <kb_rds_master_secret_arn> \
        --postgres-log-group /aws/rds/cluster/aws-ai-study-kb-rds/postgresql \
        --bucket aws-ai-study-data-123 --log-group /aws-ai-study/ai
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REPO = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = REPO / "local-kb" / "sample-docs" / "kb"
FIXTURES = REPO / "local-kb" / "fixtures"
INDEX = "bedrock-knowledge-base-default-index"
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"

QUESTION = (
    "What temperature must the camera reach before science frames, and on which page is that?"
)
FOLLOW_UP = "And what happens if I try to slew it with the shutter closed?"


class Capture:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        session = boto3.Session(region_name=args.region)
        credentials = session.get_credentials()
        if credentials is None:
            raise SystemExit("No AWS credentials found. Run aws sso login first.")
        self.credentials = credentials
        self.account = session.client("sts").get_caller_identity()["Account"]
        self.agent = session.client("bedrock-agent")
        self.runtime = session.client("bedrock-agent-runtime")
        self.s3 = session.client("s3")
        self.logs = session.client("logs")
        self.kb = {"knowledgeBaseId": args.knowledge_base_id}
        self.ds = {**self.kb, "dataSourceId": args.data_source_id}
        self.out = FIXTURES / "rds" if args.store == "rds" else FIXTURES
        self.rds = session.client("rds-data") if args.store == "rds" else None
        self.model_arn = (
            f"arn:aws:bedrock:{args.region}:{self.account}:inference-profile/{args.model_id}"
            if args.model_id.startswith(("us.", "global."))
            else f"arn:aws:bedrock:{args.region}::foundation-model/{args.model_id}"
        )

    # ------------------------------------------------------------------ output

    def save(self, name: str, data: Any) -> None:
        text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
        text = text.replace(self.account, "000000000000")
        (self.out / name).write_text(text + "\n")
        print(f"  wrote {(self.out / name).relative_to(FIXTURES.parent)}")

    # ------------------------------------------------------- OpenSearch (SigV4)

    def aoss(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.args.endpoint.rstrip('/')}/{path.lstrip('/')}"
        payload = json.dumps(body).encode() if body is not None else None
        # Without the payload hash AOSS answers 403 to any request with a body.
        headers = {
            "Content-Type": "application/json",
            "x-amz-content-sha256": hashlib.sha256(payload or b"").hexdigest(),
        }
        request = AWSRequest(method=method, url=url, data=payload, headers=headers)
        SigV4Auth(self.credentials.get_frozen_credentials(), "aoss", self.args.region).add_auth(
            request
        )
        signed = urllib.request.Request(  # noqa: S310 - endpoint from terraform output
            url, data=payload, headers=dict(request.headers), method=method
        )
        try:
            with urllib.request.urlopen(signed, timeout=60) as response:  # noqa: S310
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path}: HTTP {exc.code} {exc.read().decode()}") from exc

    def dump_documents(self, name: str) -> None:
        if self.args.store == "rds":
            self.dump_rows(name)
            return
        # AOSS is eventually consistent: 30 s after a job reported COMPLETE, an edited file's new
        # chunk was still invisible and a deleted file still searchable. Wait longer than that.
        time.sleep(120)
        result = self.aoss(
            "POST",
            f"{INDEX}/_search",
            {"size": 200, "query": {"match_all": {}}, "_source": {"excludes": [VECTOR_FIELD]}},
        )
        hits = result["hits"]["hits"]
        # Keep one vector's length and norm as evidence, not 1024 floats per chunk.
        probe = self.aoss("POST", f"{INDEX}/_search", {"size": 1, "query": {"match_all": {}}})
        vector = probe["hits"]["hits"][0]["_source"][VECTOR_FIELD] if probe["hits"]["hits"] else []
        self.save(
            name,
            {
                "total": result["hits"]["total"],
                "vector_probe": {
                    "length": len(vector),
                    "l2_norm": sum(v * v for v in vector) ** 0.5,
                    "first_8": vector[:8],
                },
                "hits": hits,
            },
        )

    # ------------------------------------------------- Aurora (RDS Data API)

    def sql(self, statement: str) -> list[dict[str, Any]]:
        assert self.rds is not None
        response = self.rds.execute_statement(
            resourceArn=self.args.cluster_arn,
            secretArn=self.args.secret_arn,
            database=self.args.database,
            sql=statement,
            formatRecordsAs="JSON",
        )
        rows: list[dict[str, Any]] = json.loads(response.get("formattedRecords") or "[]")
        return rows

    def dump_schema(self, name: str) -> None:
        schema, table = self.args.table.split(".")
        where = f"table_schema = '{schema}' AND table_name = '{table}'"
        self.save(
            name,
            {
                "columns": self.sql(
                    "SELECT column_name, data_type, udt_name, is_nullable "
                    f"FROM information_schema.columns WHERE {where} ORDER BY ordinal_position"
                ),
                "indexes": self.sql(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    f"WHERE schemaname = '{schema}' AND tablename = '{table}'"
                ),
                "pgvector": self.sql(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                ),
                "settings": self.sql(
                    "SELECT name, setting FROM pg_settings WHERE name LIKE 'hnsw.%' "
                    "OR name IN ('server_version', 'default_text_search_config')"
                ),
            },
        )

    def dump_rows(self, name: str) -> None:
        # Same wait as for AOSS, so the two stores are snapshotted at the same moment after
        # the job — Aurora is transactional, but whether Bedrock writes before or after it
        # reports COMPLETE is part of what this records.
        time.sleep(120)
        rows = self.sql(
            "SELECT id::text AS id, chunks, metadata::text AS metadata, "
            "custom_metadata::text AS custom_metadata, vector_dims(embedding) AS dims, "
            "vector_norm(embedding) AS l2_norm, "
            "subvector(embedding, 1, 8)::text AS first_8 "
            f"FROM {self.args.table} ORDER BY metadata::text, chunks"
        )
        for row in rows:
            for key in ("metadata", "custom_metadata"):
                if row.get(key) is not None:
                    row[key] = json.loads(row[key])
        self.save(name, {"total": len(rows), "rows": rows})

    def capture_statements(self, since: datetime) -> None:
        """Every statement the cluster logged, vector literals shortened to their length."""
        # One character class, not a repeated group: the group form backtracks catastrophically
        # on the 1024-float literals in Bedrock's INSERTs.
        vector = re.compile(r"\[[-+\d.eE,]{200,}\]")
        password = re.compile(r"PASSWORD '[^']*'", re.IGNORECASE)
        events: list[dict[str, Any]] = []
        group = self.args.postgres_log_group
        # One stream per instance. filter_log_events crawls this group for tens of minutes
        # (it pages through time, mostly empty pages); reading each stream in order is instant.
        for stream in self.logs.describe_log_streams(logGroupName=group)["logStreams"]:
            token = None
            while True:
                page = self.logs.get_log_events(
                    logGroupName=group,
                    logStreamName=stream["logStreamName"],
                    startTime=int(since.timestamp() * 1000),
                    startFromHead=True,
                    **({"nextToken": token} if token else {}),
                )
                for event in page["events"]:
                    message = vector.sub(
                        lambda m: f"[<{m.group(0).count(',') + 1} floats>]", event["message"]
                    )
                    # rds_kb.py sets bedrock_user's password with ALTER ROLE, which is logged.
                    message = password.sub("PASSWORD '<redacted>'", message)
                    events.append({"timestamp": event["timestamp"], "message": message})
                if page["nextForwardToken"] == token:
                    break
                token = page["nextForwardToken"]
        events.sort(key=lambda e: e["timestamp"])
        self.save("postgresql-statements.json", events)

    # ------------------------------------------------------------ control plane

    def sync(self, name: str) -> None:
        job = self.agent.start_ingestion_job(**self.ds)["ingestionJob"]
        print(f"  ingestion job {job['ingestionJobId']} started")
        while job["status"] in ("STARTING", "IN_PROGRESS", "STOPPING"):
            time.sleep(10)
            job = self.agent.get_ingestion_job(**self.ds, ingestionJobId=job["ingestionJobId"])[
                "ingestionJob"
            ]
        print(f"  ingestion job {job['status']}: {job.get('statistics')}")
        self.save(name, job)

    def upload(self, folder: Path) -> None:
        prefix = self.args.prefix
        existing = self.s3.list_objects_v2(Bucket=self.args.bucket, Prefix=prefix).get(
            "Contents", []
        )
        for obj in existing:
            self.s3.delete_object(Bucket=self.args.bucket, Key=obj["Key"])
        for path in sorted(folder.iterdir()):
            self.s3.upload_file(str(path), self.args.bucket, f"{prefix}{path.name}")
            print(f"  uploaded s3://{self.args.bucket}/{prefix}{path.name}")

    # ----------------------------------------------------------------- runtime

    def retrieve(self, name: str, query: str, **config: Any) -> None:
        response = self.runtime.retrieve(
            **self.kb,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": config},
        )
        response.pop("ResponseMetadata", None)
        self.save(
            name, {"request": {"query": query, "vectorSearchConfiguration": config}, **response}
        )

    def rag(
        self,
        name: str,
        text: str,
        session_id: str | None = None,
        model_id: str | None = None,
        **kb_config: Any,
    ) -> str:
        request: dict[str, Any] = {
            "input": {"text": text},
            "retrieveAndGenerateConfiguration": {
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": self.args.knowledge_base_id,
                    "modelArn": self.model_arn
                    if model_id is None
                    else f"arn:aws:bedrock:{self.args.region}::foundation-model/{model_id}",
                    **kb_config,
                },
            },
        }
        if session_id:
            request["sessionId"] = session_id
        response = self.runtime.retrieve_and_generate(**request)
        response.pop("ResponseMetadata", None)
        self.save(name, {"request": request, **response})
        return str(response["sessionId"])

    # ---------------------------------------------------------------- the run

    def run(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)
        if self.args.resume_after_first_sync:
            # Job 1 already ran and was saved; re-running it would sync nothing new.
            job = json.loads((self.out / "ingestion-job-1.json").read_text())
            started = datetime.fromisoformat(job["startedAt"])

        print("control plane")
        self.save("knowledge-base.json", self.agent.get_knowledge_base(**self.kb)["knowledgeBase"])
        self.save("data-source.json", self.agent.get_data_source(**self.ds)["dataSource"])

        if not self.args.resume_after_first_sync:
            print("first sync")
            self.upload(SAMPLE_DOCS)
            self.sync("ingestion-job-1.json")
        if self.args.store == "rds":
            self.dump_schema("table-schema.json")
        else:
            self.save("index-mapping.json", self.aoss("GET", f"{INDEX}/_mapping"))
        self.dump_documents("index-documents.json")

        print("retrieve")
        self.retrieve("retrieve-semantic.json", "Which fuse does the dew controller take?")
        self.retrieve(
            "retrieve-semantic-keyword.json",
            "ZX-4471",
            overrideSearchType="SEMANTIC",
        )
        self.retrieve("retrieve-hybrid-keyword.json", "ZX-4471", overrideSearchType="HYBRID")
        self.retrieve(
            "retrieve-filter-equals.json",
            "What should I do at the start of a session?",
            filter={"equals": {"key": "department", "value": "operations"}},
        )
        self.retrieve(
            "retrieve-filter-page.json",
            "telescope",
            filter={"equals": {"key": "x-amz-bedrock-kb-document-page-number", "value": 2}},
        )
        self.retrieve(
            "retrieve-filter-startswith.json",
            "telescope",
            filter={
                "startsWith": {
                    "key": "x-amz-bedrock-kb-source-uri",
                    "value": f"s3://{self.args.bucket}/{self.args.prefix}tidewater",
                }
            },
        )
        self.retrieve(
            "retrieve-filter-andall.json",
            "maintenance",
            filter={
                "andAll": [
                    {"equals": {"key": "safety_critical", "value": True}},
                    {"greaterThanOrEquals": {"key": "effective_date", "value": 20261001}},
                    {"listContains": {"key": "tags", "value": "heater"}},
                ]
            },
        )
        self.retrieve(
            "retrieve-inventory-10.json", "Which items are in drawer B?", numberOfResults=10
        )

        print("retrieve and generate")
        session = self.rag("rag-default.json", QUESTION)
        self.rag("rag-follow-up.json", FOLLOW_UP, session_id=session)
        self.rag(
            "rag-no-format-instructions.json",
            QUESTION,
            generationConfiguration={
                "promptTemplate": {
                    "textPromptTemplate": "Answer from these results only.\n$search_results$\n"
                    "Question: $query$"
                }
            },
        )
        self.rag(
            "rag-decomposition.json",
            "How much does a student membership cost, and which port does the dew strap use?",
            orchestrationConfiguration={
                "queryTransformationConfiguration": {"type": "QUERY_DECOMPOSITION"}
            },
        )
        # The default prompt depends on the model family: Nova gets a "%[1]%" passage template,
        # other models the XML <search_results>/<answer_part> one. One of each, top 10 results.
        top_10 = {"vectorSearchConfiguration": {"numberOfResults": 10}}
        for slug, model_id in (
            ("llama3-8b", "meta.llama3-8b-instruct-v1:0"),
            ("nova-pro", "amazon.nova-pro-v1:0"),
        ):
            self.rag(f"rag-{slug}.json", QUESTION, model_id=model_id, retrievalConfiguration=top_10)

        print("second sync: edit one, delete one, change one sidecar only")
        with tempfile.TemporaryDirectory() as tmp:
            mutated = Path(tmp)
            for path in SAMPLE_DOCS.iterdir():
                shutil.copy(path, mutated / path.name)
            (mutated / "winter-closure-2025.txt").unlink()
            with (mutated / "night-log-template.txt").open("a") as f:
                f.write("\nFrom November 2026, also record the moon phase.\n")
            sidecar = mutated / "dew-heater-guide.md.metadata.json"
            meta = json.loads(sidecar.read_text())
            meta["metadataAttributes"]["revision"]["value"]["numberValue"] = 4
            sidecar.write_text(json.dumps(meta, indent=2))
            self.upload(mutated)
        self.sync("ingestion-job-2.json")
        self.dump_documents("index-documents-after-mutation.json")

        print("invocation logs")
        self.capture_logs(started)
        if self.args.store == "rds":
            print("postgresql statements")
            self.capture_statements(started)

    def capture_logs(self, since: datetime) -> None:
        # Delivery to CloudWatch lags by a minute or two.
        time.sleep(120)
        events: list[dict[str, Any]] = []
        paginator = self.logs.get_paginator("filter_log_events")
        for page in paginator.paginate(
            logGroupName=self.args.log_group, startTime=int(since.timestamp() * 1000)
        ):
            for event in page["events"]:
                try:
                    record = json.loads(event["message"])
                except json.JSONDecodeError:
                    continue
                if "modelId" not in record:
                    continue
                # Embedding outputs are vectors; keep what was embedded, drop the floats.
                output = record.get("output", {})
                if "embed" in record["modelId"]:
                    output.pop("outputBodyJson", None)
                events.append(record)
        self.save("invocation-logs.json", events)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--data-source-id", required=True)
    parser.add_argument("--store", choices=("aoss", "rds"), default="aoss")
    parser.add_argument("--endpoint", help="AOSS: collection endpoint (https://...)")
    parser.add_argument("--cluster-arn", help="RDS: terraform output kb_rds_cluster_arn")
    parser.add_argument("--secret-arn", help="RDS: kb_rds_master_secret_arn")
    parser.add_argument("--database", default="bedrock", help="RDS: database name")
    parser.add_argument(
        "--table", default="bedrock_integration.bedrock_kb", help="RDS: schema-qualified table"
    )
    parser.add_argument(
        "--postgres-log-group", help="RDS: terraform output kb_rds_postgresql_log_group"
    )
    parser.add_argument(
        "--bucket", required=True, help="Data bucket (terraform output data_bucket)"
    )
    parser.add_argument("--log-group", required=True, help="Bedrock invocation-logging log group")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--prefix", default="kb/", help="Data source inclusion prefix")
    parser.add_argument(
        "--model-id",
        # Not the us. inference profile: it routes to other regions, which the seatbelt denies.
        default="amazon.nova-lite-v1:0",
        help="Generation model or inference profile for RetrieveAndGenerate",
    )
    parser.add_argument(
        "--resume-after-first-sync",
        action="store_true",
        help="Skip the upload and first sync, which already ran and saved ingestion-job-1.json",
    )
    args = parser.parse_args()
    needed = (
        ("cluster_arn", "secret_arn", "postgres_log_group")
        if args.store == "rds"
        else ("endpoint",)
    )
    missing = [f"--{n.replace('_', '-')}" for n in needed if getattr(args, n) is None]
    if missing:
        parser.error(f"--store {args.store} needs {', '.join(missing)}")
    Capture(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
