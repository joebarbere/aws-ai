# local-kb

A local re-implementation of an Amazon Bedrock Knowledge Base: the same index layout, field names,
API shapes and ingestion behaviour as the cloud `knowledge_base` module, at $0/month instead of
~$350. `PLAN.md` has the design and the build phases. `fixtures/` holds what a real KB produced,
captured to test against.

**Status:** Phases 0–3 (ground truth, infrastructure, models, control plane) are done. Ingestion and
the runtime API come next.

## Run it

You need podman with a compose provider. If you don't have one, install it without sudo:
`uv tool install podman-compose`.

```bash
make local-up      # writes local-kb/.env with random credentials, starts the stack, runs init
make local-smoke   # 8 checks on the storage behaviours the sync relies on
make local-models  # LM Studio server on :1234 with kb-embed and kb-llm (models: PLAN.md, Phase 2)
make local-down    # stop, keep data
make local-wipe    # stop and delete the volumes: the local `terraform destroy`
```

| Service | URL | Stands in for |
|---|---|---|
| RustFS | `http://127.0.0.1:9000` (S3), `http://127.0.0.1:9001` (console) | The S3 data source; bucket `kb-data`, prefix `kb/` |
| kb-events | `http://127.0.0.1:8098/messages` | SQS between the bucket's event notifications and the sync trigger |
| OpenSearch 3.9.0 | `https://127.0.0.1:9200` | The OpenSearch Serverless vector collection |
| Dashboards | `http://127.0.0.1:5601` | The collection's dashboards endpoint |

Credentials are in `local-kb/.env`, which is gitignored:

- Log in to Dashboards and OpenSearch as `admin` with `OPENSEARCH_INITIAL_ADMIN_PASSWORD`.
- Log in to the RustFS console with `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY`.
- The worker and runtime use the least-privilege `bedrock-kb` user, never `admin`. It is the local
  version of the AOSS data-access policy.

Every port is bound to 127.0.0.1. OpenSearch's demo TLS certificate is self-signed, so use
`curl -k`.

## Why the bucket events go through a queue

RustFS refuses to call webhooks on link-local addresses, even when they are allow-listed. Under
rootless podman, the host is reachable from containers only at `host.containers.internal`
(`169.254.1.2`), which is link-local. So bucket events go to `kb-events`, a small container on the
compose network, and the host polls it. That is S3 → SQS → poller, which is also how you'd wire it
on AWS. It also means nothing on the host listens beyond loopback.
