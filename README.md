# aws-ai

Terraform and Python for the AWS AI/ML services covered by the **AWS Certified AI Practitioner
(AIF-C01)** exam — built as working infrastructure rather than flashcards, so each service is
something you can `apply`, poke at from Python, and `destroy`.

## Why it is shaped this way

Most of the AI services on the exam are *serverless APIs*: Comprehend, Rekognition, Polly,
Translate, and Transcribe have nothing to provision. What they need is an S3 bucket to read from,
an IAM role that lets them read it, and a log group. That is what `modules/foundation` is for, and
it is why the module list below is much shorter than the service list on the exam.

The services that genuinely own infrastructure — Bedrock guardrails and knowledge bases, SageMaker
domains and endpoints, Kendra indexes, Lex bots — get their own modules as they are built out.

## Layout

```
terraform/
  versions.tf providers.tf variables.tf main.tf outputs.tf
  modules/
    foundation/   S3 + KMS + IAM + CloudWatch: the shared base every AI service call needs
    bedrock/      Guardrails, model invocation logging, agent/KB execution roles
python/
  src/aws_ai/     Thin, testable boto3 wrappers (clients are injected, so tests need no network)
  tests/          pytest, offline
.claude/agents/
  aws-ai-terraform.md   The agent that writes and reviews the Terraform in this repo
```

## Prerequisites

Neither is installed on this machine yet:

```bash
# Terraform (or OpenTofu)
sudo dnf install -y terraform     # or: https://opentofu.org/docs/intro/install/
# AWS CLI v2
sudo dnf install -y awscli2
aws configure sso                 # or any credential method you prefer
```

Python side:

```bash
cd python && uv sync && uv run pytest
```

## Cost warning

Some resources here are **not** free-tier and bill hourly whether or not you use them — SageMaker
domains and endpoints, Kendra indexes, OpenSearch Serverless collections. Anything in that class is
gated behind an explicit `enable_*` variable that defaults to `false`. Run `terraform destroy` when
you finish a study session.

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit: region, name_prefix
terraform init
terraform plan
terraform apply
```

## The agent

`.claude/agents/aws-ai-terraform.md` defines an agent that writes Terraform for a named service,
checks it against the exam domains, keeps the cost gates in place, and refuses to leave a bucket
public or a key unrotated. Invoke it with the Agent tool as `aws-ai-terraform`.

## Exam reference

Study target is the AWS Certified AI Practitioner (AIF-C01) exam guide, with *AWS Certified AI
Practitioner Study Guide* (Tom Taulli, O'Reilly, 2025) as the reading companion. The authoritative
scope is always [AWS's own exam guide](https://aws.amazon.com/certification/certified-ai-practitioner/).

## License

MIT — see [LICENSE](LICENSE).
