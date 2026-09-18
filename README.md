# aws-ai

Terraform and Python for the AWS AI/ML services covered by the **AWS Certified AI Practitioner
(AIF-C01)** exam — built as working infrastructure rather than flashcards, so each service is
something you can `apply`, poke at from Python, and `destroy`.

## Why it is shaped this way

Many of the AI services on the exam are *serverless APIs* with nothing to provision. What they need
is a bucket to read from, an IAM role that lets them read it, and a log group. That is what
`modules/foundation` is — and knowing **which** services are like that is itself exam material, so
this repo makes the distinction explicit rather than inventing resources that do not exist.

## What gets created

Nine modules. Everything validates against AWS provider `~> 5.70`.

### `foundation` — always on · ~$1/month

KMS key (`aws_kms_key`, `_alias`, `_key_policy`, rotation on), private bucket (`aws_s3_bucket` plus
`_public_access_block`, `_versioning`, `_server_side_encryption_configuration`),
`aws_cloudwatch_log_group`, and an `aws_iam_role` trusted by Comprehend, Rekognition, Textract,
Transcribe, and Translate — scoped to one bucket and key, with `aws:SourceAccount` on the trust
policy.

### `bedrock` — on by default · billed per use

`aws_bedrock_guardrail` (hate/violence/prompt-attack filters, a denied topic, `EMAIL` anonymized,
`US_SOCIAL_SECURITY_NUMBER` blocked), `aws_bedrock_guardrail_version`, and
`aws_bedrock_model_invocation_logging_configuration`.

### `conversational` — on by default · billed per request

`aws_lexv2models_bot` + `_bot_locale` + `_intent`, and the role Lex uses to call Polly and
Comprehend. Transcribe and Polly appear only in the permissions.

### `sagemaker` — gated off · domain free, apps ~$0.05/hr

`aws_sagemaker_domain` + `_user_profile` + execution role. Optionally `_model` +
`_endpoint_configuration` + `_endpoint`, `aws_sagemaker_feature_group` (Feature Store), and
`_data_quality_job_definition` + `_monitoring_schedule` (Model Monitor).

### `data_foundations` — gated off · Glue near-free, Aurora ~$43/month

`aws_glue_catalog_database`, `aws_glue_crawler`, optional `aws_glue_job`. Plus Aurora PostgreSQL
Serverless v2 (`aws_rds_cluster` + `_cluster_instance`, subnet group, security group) for
**pgvector** — a second RAG store at an eighth the cost of OpenSearch Serverless.

### `governance` — gated off · usage-billed

`aws_config_configuration_recorder` + `_delivery_channel` + `_recorder_status` + two managed
`aws_config_config_rule`s, `aws_macie2_account` + `_custom_data_identifier` + `_classification_job`,
and `aws_auditmanager_account_registration` + `_control` + `_framework`.

### `vision_nlp` — gated off · free at rest

`aws_rekognition_collection` (+ optional `_project`), and `aws_comprehend_document_classifier` /
`_entity_recognizer` with their training role — both gated on you supplying training data.

### `knowledge_base` — gated off · **~$350/month**

`aws_opensearchserverless_collection` with encryption, network, and data-access policies, plus
`aws_bedrockagent_knowledge_base` and `_data_source`.

### `kendra` — gated off · **~$810/month**

`aws_kendra_index` + `aws_kendra_data_source`, with Kendra's real two-role split.

## Services with nothing to provision

This list is deliberate. Each of these appears on the exam, and each has **zero** Terraform
resources — verified against the provider schema, not assumed:

| Service | Why | How you use it here |
|---|---|---|
| **Polly** | Pure API | `aws_ai.speech.synthesize_speech` |
| **Translate** | Pure API | boto3 + the foundation role |
| **Textract** | Pure API (sync); async jobs need only a bucket | boto3 + the foundation role |
| **SageMaker Clarify** | Runs as processing *jobs* you submit | SDK, from a Studio notebook |
| **Data Wrangler** | A Studio UI feature, not a resource | Inside the SageMaker domain |
| **Model Cards** | API and console only | boto3 |
| **Fraud Detector** | Has an API and CloudFormation, but the provider has never covered it | boto3 or CloudFormation |
| **AWS Artifact** | A console portal for compliance reports | Console; concept-only for the exam |

Rekognition and Comprehend are the interesting middle case: `DetectLabels` and `detect_sentiment`
provision nothing, while a face *collection* and a *custom classifier* are real resources. Same
service, two shapes — which is exactly what `vision_nlp` exists to show.

## Cost, in one table

| Module | Idle cost | Notes |
|---|---|---|
| foundation | ~$1/month | KMS key; storage on top |
| bedrock | $0 | Per text unit evaluated |
| conversational | $0 | Lex ~$0.00075/text request |
| vision_nlp | $0 | Per image / per training hour |
| governance | $0 idle | Config ~$0.003/item, Macie ~$1/GB |
| data_foundations (Glue) | ~$0 | Crawler ~$0.44/DPU-hr **while running** |
| data_foundations (Aurora) | ~$0.06/hr | ~$43/month; does **not** scale to zero |
| sagemaker (domain) | $0 | A running app is ~$0.05/hr until shut down |
| sagemaker (endpoint) | ~$0.13/hr | ~$95/month, traffic or not |
| knowledge_base | ~$0.48/hr | ~$350/month |
| kendra | ~$1.125/hr | ~$810/month |

## Python

`python/src/aws_ai/` — every function takes its boto3 client as an argument, so the 25 tests run
offline with fakes.

| Module | Functions |
|---|---|
| `bedrock` | `invoke_text_model` (Converse API, optional guardrail) |
| `comprehend` | `detect_sentiment`, `detect_dominant_language` |
| `kendra` | `query_index`, `start_sync` |
| `lex` | `recognize_text` |
| `speech` | `synthesize_speech`, `start_transcription`, `get_transcription` |

Two scripts do the things Terraform cannot:

- `scripts/create_vector_index.py` — creates the OpenSearch Serverless vector index (signed HTTP).
- `scripts/enable_pgvector.py` — runs `CREATE EXTENSION vector` and builds an HNSW table
  (`uv sync --extra db`).

## Getting started

**Terraform 1.16.3** at `~/.local/bin/terraform`:

```bash
curl -sLo tf.zip https://releases.hashicorp.com/terraform/1.16.3/terraform_1.16.3_linux_amd64.zip
unzip tf.zip && mv terraform ~/.local/bin/
```

**AWS CLI** — `aws-cli/1.46.1` via `uv tool install awscli`. For v2:
`sudo dnf install -y awscli2`, then `aws configure sso`.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
terraform init && terraform apply     # foundation + bedrock + conversational; nothing hourly

cd ../python && uv sync --extra dev && uv run pytest
```

**Before Bedrock will answer you**, enable model access in the console (Bedrock → Model access).
There is no Terraform resource for it, and `InvokeModel` returns `AccessDeniedException` until you
do.

## Tips for playing around

**Start where it's free.** The defaults give you a guardrail, a Lex bot, and the bucket/role/key
base for about a dollar a month.

**Make the guardrail fire.** Ask for stock advice and watch `stop_reason` come back as
`guardrail_intervened`; send an email address and watch it come back anonymized. Note the
asymmetry: `PROMPT_ATTACK` filtering applies to input only.

**Watch Lex's confidence, not its reply.** `recognize_text` returns `intent_confidence` and
`fell_back`. Find the utterance where `n_lu_intent_confidence_threshold` flips the answer.

**Break a Config rule on purpose.** Turn off the bucket's public-access block, watch the rule go
`NON_COMPLIANT`, then put it back. Continuous compliance in one minute.

**Feed Macie something it should find.** Upload a file with `STUDY-12345` and a fake email — the
custom identifier catches one, managed identifiers catch the other. Custom versus managed is what
Macie questions turn on.

**Run the two RAG stores against each other.** Same documents, same embedding model: OpenSearch
Serverless (~$350/month, fully managed) versus Aurora + pgvector (~$43/month, you own the schema
and index). A Bedrock knowledge base supports either, and the tradeoff only becomes real once
you've paid both.

**Notice what is synchronous.** Polly hands you bytes; Transcribe hands you a job to poll. Same
split for Textract and Comprehend batch — a favorite exam shape.

**Read the IAM.** Two patterns repay attention: `aws:SourceAccount` on every service trust policy
(the confused-deputy fix), and the places where IAM alone is not enough — OpenSearch Serverless
needs a *data-access policy* on top, and AWS Config needs the *bucket policy* to agree with the
role.

**Watch the gates refuse you.** Set `enable_sagemaker_endpoint = true` with no model image and
nothing is created. Skip `create_vector_index.py` and the knowledge base fails with an error that
never mentions the index. Try `allowed_cidr_blocks = ["0.0.0.0/0"]` and Terraform refuses outright.

**Set a billing alarm before enabling anything hourly.** A $10 budget alert is the difference
between noticing a forgotten Kendra index on day one instead of day thirty.

**Destroy with the same flags you applied with:**

```bash
terraform destroy -var enable_kendra=true -var enable_aurora=true
```

A gated resource left out of the destroy command does not get destroyed. `terraform state list` is
the check.

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
