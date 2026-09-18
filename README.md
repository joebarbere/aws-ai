# aws-ai

Terraform and Python for the AWS AI/ML services covered by the **AWS Certified AI Practitioner
(AIF-C01)** exam — built as working infrastructure rather than flashcards, so each service is
something you can `apply`, poke at from Python, and `destroy`.

## Why it is shaped this way

Most of the AI services on the exam are *serverless APIs*: Comprehend, Rekognition, Polly,
Translate, Transcribe, and the synchronous Textract calls have nothing to provision. What they need
is a bucket to read from, an IAM role that lets them read it, and a log group. That is what
`modules/foundation` is, and it is why the module list is much shorter than the service list.

The services that genuinely own infrastructure — Bedrock guardrails and knowledge bases, SageMaker
domains and endpoints, Kendra indexes, Lex bots — get their own modules.

## What gets created

Six modules. Everything validates against AWS provider `~> 5.70`.

### `foundation` — always on · ~$1/month

The shared base. A KMS key (`aws_kms_key`, `_alias`, `_key_policy`, rotation on), a private bucket
(`aws_s3_bucket` plus `_public_access_block`, `_versioning`, `_server_side_encryption_configuration`),
a `aws_cloudwatch_log_group`, and an `aws_iam_role` trusted by Comprehend, Rekognition, Textract,
Transcribe, and Translate — scoped to that one bucket and key, with an `aws:SourceAccount` condition
on the trust policy.

### `bedrock` — on by default · billed per use

`aws_bedrock_guardrail` with content filters (hate, violence, prompt-attack), a denied topic, and
PII handling — `EMAIL` anonymized, `US_SOCIAL_SECURITY_NUMBER` blocked. Plus
`aws_bedrock_guardrail_version` and `aws_bedrock_model_invocation_logging_configuration` with its
own logging role.

### `conversational` — on by default · billed per request

`aws_lexv2models_bot` + `_bot_locale` + `_intent` (one worked intent with sample utterances), and
the role Lex assumes to call Polly and Comprehend. Transcribe and Polly appear only in the
permissions, because they provision nothing.

### `sagemaker` — gated off · domain free, apps ~$0.05/hr

`aws_sagemaker_domain` + `_user_profile` + execution role, and optionally `aws_sagemaker_model` +
`_endpoint_configuration` + `_endpoint`. The endpoint is **doubly gated**: `enable_sagemaker_endpoint`
does nothing unless `sagemaker_model_image` and `sagemaker_model_data_url` are both set.

### `knowledge_base` — gated off · **~$350/month**

Managed RAG. `aws_opensearchserverless_collection` (VECTORSEARCH) with its three required policies
— encryption, network, and the separate *data access* policy — plus the Bedrock role,
`aws_bedrockagent_knowledge_base`, and `aws_bedrockagent_data_source`. Applies in three steps; see
[its README](terraform/modules/knowledge_base/README.md).

### `kendra` — gated off · **~$810/month**

`aws_kendra_index` + `aws_kendra_data_source` with Kendra's real two-role split: an index role for
metrics and logs, a separate data-source role that reads S3 and calls `BatchPutDocument`.

## Cost, in one table

| Module | Idle cost | Notes |
|---|---|---|
| foundation | ~$1/month | KMS key; storage on top |
| bedrock | $0 | Guardrails bill per text unit evaluated |
| conversational | $0 | Lex ~$0.00075/text request; Polly has a big free tier |
| sagemaker (domain) | $0 | But a running JupyterLab app is ~$0.05/hr **until you shut it down** |
| sagemaker (endpoint) | ~$0.13/hr | ~$95/month, traffic or not |
| knowledge_base | ~$0.48/hr | OpenSearch Serverless 2-OCU floor, ~$350/month |
| kendra | ~$1.125/hr | ~$810/month, no free tier past a 30-day trial |

The bottom two are the ones that hurt. They are off by default, and both READMEs lead with the
number.

## Python

`python/src/aws_ai/` — every function takes its boto3 client as an argument, so the 25 tests run
offline with fakes and you can point a call at any region or profile.

| Module | Functions |
|---|---|
| `bedrock` | `invoke_text_model` (Converse API, optional guardrail) |
| `comprehend` | `detect_sentiment`, `detect_dominant_language` |
| `kendra` | `query_index`, `start_sync` |
| `lex` | `recognize_text` |
| `speech` | `synthesize_speech`, `start_transcription`, `get_transcription` |

Plus `python/scripts/create_vector_index.py`, which does the one thing Terraform cannot: create the
OpenSearch Serverless vector index, via a SigV4-signed HTTP PUT.

## Getting started

**Terraform 1.16.3** is at `~/.local/bin/terraform`; reproduce with:

```bash
curl -sLo tf.zip https://releases.hashicorp.com/terraform/1.16.3/terraform_1.16.3_linux_amd64.zip
unzip tf.zip && mv terraform ~/.local/bin/
```

**AWS CLI** — `aws-cli/1.46.1` via `uv tool install awscli`; its botocore covers every service here.
For v2, which is what AWS documents: `sudo dnf install -y awscli2`, then `aws configure sso`.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit region and name_prefix
terraform init
terraform apply        # foundation + bedrock + conversational; nothing hourly

cd ../python && uv sync --extra dev && uv run pytest
```

**Before Bedrock will answer you**, enable model access in the console: Bedrock → Model access →
enable Titan and Claude in your region. There is no Terraform resource for this, and `InvokeModel`
returns `AccessDeniedException` until you do it.

## Tips for playing around

**Start where it's free.** `apply` with the defaults gives you a guardrail, a Lex bot, and the
bucket/role/key base. That covers a surprising amount of the exam and costs about a dollar a month.

**Make the guardrail fire.** Ask the model something the denied topic covers — "which stock should
I buy today?" — and watch `stop_reason` come back as `guardrail_intervened`. Then send an email
address through and see it come back anonymized. The `blocked_by_guardrail` property exists so this
is one line to check. Editing `content_policy_config` strengths and re-applying takes seconds, and
it teaches the input/output asymmetry: `PROMPT_ATTACK` only applies to input.

**Watch Lex's confidence, not its reply.** Build the bot, then send it utterances that are
deliberately near the edge of the intent. `recognize_text` returns `intent_confidence` and
`fell_back`, so you can find the point where `n_lu_intent_confidence_threshold` flips the answer to
the fallback. That threshold is a favorite exam topic and it makes much more sense once you've seen
it move.

**Compare the two search stories.** Kendra and a Bedrock knowledge base solve overlapping problems
differently — Kendra is managed search with FAQ matching and result *types*, a knowledge base is
retrieval feeding generation. Both are expensive, so pick one, run it for an afternoon with the
same documents, destroy it, and do the other another day. The `QueryResult.result_type` field in
the Kendra wrapper is there to make the distinction visible.

**Notice what is synchronous.** Polly hands you bytes; Transcribe hands you a job to poll. That
split — synchronous inference versus asynchronous batch — is exactly the shape the exam asks about
for Textract and Comprehend too.

**Read the IAM, it's half the exam.** Two patterns here repay a close look: the `aws:SourceAccount`
condition on every service trust policy (the confused-deputy fix), and the fact that OpenSearch
Serverless needs a *data-access policy* on top of IAM — a role with `aoss:APIAccessAll` still reads
nothing without it.

**Break something on purpose.** Set `enable_sagemaker_endpoint = true` without a model image and
watch nothing get created — that gate is deliberate. Or skip `create_vector_index.py` and see the
knowledge base fail with an error that never mentions the missing index.

**Set a billing alarm before you enable anything hourly.** A budget alert at $10 takes two minutes
and is the difference between noticing a forgotten Kendra index on day one instead of day thirty.

**Destroy at the end of every session.**

```bash
terraform destroy -var enable_kendra=true    # flags must match what you applied
```

A gated resource left out of the destroy command does not get destroyed. If in doubt,
`terraform state list` and look for anything hourly.

## Exam reference

Study target is the AWS Certified AI Practitioner (AIF-C01) exam guide, with *AWS Certified AI
Practitioner Study Guide* (Tom Taulli, O'Reilly, 2025) as a reading companion. The authoritative
scope is always [AWS's own exam guide](https://aws.amazon.com/certification/certified-ai-practitioner/).

## The agent

`.claude/agents/aws-ai-terraform.md` defines an agent that writes Terraform for a named service,
keeps the cost gates in place, refuses to leave a bucket public, and knows which services provision
nothing at all. Invoke it with the Agent tool as `aws-ai-terraform`.

## License

MIT — see [LICENSE](LICENSE).
