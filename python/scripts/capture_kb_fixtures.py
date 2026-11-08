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

    uv run python scripts/capture_kb_fixtures.py --knowledge-base-id ABCDEFGHIJ \\
        --data-source-id KLMNOPQRST --endpoint https://abc.us-east-1.aoss.amazonaws.com \\
        --bucket aws-ai-study-data-123 --log-group /aws-ai-study/ai
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
        self.model_arn = (
            f"arn:aws:bedrock:{args.region}:{self.account}:inference-profile/{args.model_id}"
            if args.model_id.startswith(("us.", "global."))
            else f"arn:aws:bedrock:{args.region}::foundation-model/{args.model_id}"
        )

    # ------------------------------------------------------------------ output

    def save(self, name: str, data: Any) -> None:
        text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
        text = text.replace(self.account, "000000000000")
        (FIXTURES / name).write_text(text + "\n")
        print(f"  wrote fixtures/{name}")

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
        FIXTURES.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)

        print("control plane")
        self.save("knowledge-base.json", self.agent.get_knowledge_base(**self.kb)["knowledgeBase"])
        self.save("data-source.json", self.agent.get_data_source(**self.ds)["dataSource"])

        print("first sync")
        self.upload(SAMPLE_DOCS)
        self.sync("ingestion-job-1.json")
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
    parser.add_argument("--endpoint", required=True, help="Collection endpoint (https://...)")
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
    Capture(parser.parse_args()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
