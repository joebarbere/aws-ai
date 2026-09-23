# Credentials and guardrails

How to get credentials for this repo, and the one policy worth attaching.

## Use IAM Identity Center, not an IAM user

An IAM user means a long-lived access key in `~/.aws/credentials` — the thing most likely to end up
in a screenshot, a paste, or a commit. Identity Center gives temporary credentials, MFA that costs
nothing at the CLI, and no secret on disk.

### Why there is no "least privilege" Terraform policy here

This repo creates IAM roles and attaches managed policies to them. Any principal that can call
`iam:CreateRole` + `iam:AttachRolePolicy` + `iam:PassRole` can mint itself an admin role and assume
it. A scoped 400-line policy containing those three actions is administrator access wearing a
lanyard — it buys the feeling of containment and almost none of the substance.

So: use `AdministratorAccess`, be honest about the blast radius, and spend the effort on
[`cost-seatbelt.json`](cost-seatbelt.json), which binds *regardless* of privilege because an
explicit `Deny` always wins.

## Setup

### 0. AWS CLI v2

`aws configure sso` does not exist in CLI v1. On Fedora:

```fish
sudo dnf install -y awscli2
uv tool uninstall awscli    # if the v1 from uv is still on PATH ahead of /usr/bin
aws --version               # expect aws-cli/2.x
```

### 1. Enable Identity Center (console, once)

IAM Identity Center → **Enable**. Two things to know first:

- It requires AWS Organizations. In a standalone account, enabling it creates one for free.
- The identity store is pinned to a region, and moving it later means deleting and recreating the
  whole thing. Pick `us-east-1` to match this repo's default.

Then:

1. **Users** → add yourself (real email — an activation mail arrives).
2. **Multi-factor authentication** → register a device.
3. **Permission sets** → Create → Predefined → `AdministratorAccess`. Session duration 8 hours is
   a sensible study-day default.
4. **AWS accounts** → your account → **Assign users** → yourself + that permission set.

Copy the start URL (`https://d-xxxxxxxxxx.awsapps.com/start`).

### 2. Configure the CLI

```fish
aws configure sso
```

| Prompt | Answer |
|---|---|
| SSO session name | `aws-ai` |
| SSO start URL | your `d-xxxx.awsapps.com/start` URL |
| SSO region | `us-east-1` |
| SSO registration scopes | accept the default (`sso:account:access`) |
| CLI default client region | `us-east-1` |
| CLI default output format | `json` |
| CLI profile name | `aws-ai` |

A browser opens to approve a device code. Nothing secret is written — just a cached token under
`~/.aws/sso/cache/`.

### 3. Use it

```fish
aws sso login --profile aws-ai
set -Ux AWS_PROFILE aws-ai      # fish universal var; bash/zsh: export AWS_PROFILE=aws-ai
aws sts get-caller-identity
```

Terraform needs no changes: `providers.tf` hardcodes no profile, and the AWS provider resolves SSO
tokens natively at `~> 5.70`. When the token expires, Terraform reports `ExpiredToken` or
`SSOTokenProviderFailure` — that is just `aws sso login --profile aws-ai` again, not a broken
setup.

## Attaching the seatbelt

IAM Identity Center → Permission sets → your set → **Inline policy** → paste
[`cost-seatbelt.json`](cost-seatbelt.json) → **Provision to AWS accounts**.

That last step is the one people forget: an edited permission set does nothing until it is
re-provisioned.

### What it does

**Region lock.** Everything outside `us-east-1` is denied, with global services exempted via
`NotAction` (omit those and you break IAM, STS, and the SSO console itself). This is the
underrated half: it stops a stray `-var region=` or a copied example from quietly creating an $810
/month Kendra index somewhere you never look — which is exactly how a forgotten resource survives
a billing cycle.

**Hourly-billing deny.** Five actions, which are the entire hourly surface of this repo:

| Action | Resource | Rate |
|---|---|---|
| `kendra:CreateIndex` | Kendra index | ~$810/month |
| `aoss:CreateCollection` | OpenSearch Serverless | ~$350/month |
| `sagemaker:CreateEndpoint` | Inference endpoint | ~$95/month |
| `rds:CreateDBCluster` / `CreateDBInstance` | Aurora Serverless v2 | ~$43/month |

To study one of them, delete that line, re-provision, apply, and put it back afterwards. The
friction is the feature — it mirrors the `enable_*` variables one level lower, where a typo in a
`-var` flag cannot bypass it.

## Two things IAM will not do

**It will not cap spend.** No policy limits cost; the deny list is a proxy for the specific
resources that bill hourly. Set an actual AWS Budget alert at $10 — that is the real backstop.

**It will not protect your state file.** `terraform.tfstate` holds every resource attribute. The
Aurora password stays out of it (Secrets Manager manages that), but the file is sensitive and is
gitignored. If you move to an S3 backend, encrypt it with the project KMS key.

## If you must use an IAM user

Access key only, no console password, policies attached through a group rather than to the user
directly — plus `AdministratorAccess` and the seatbelt as a second attached policy.

One gotcha: do **not** attach a blanket "deny unless `aws:MultiFactorAuthPresent`" policy to a user
Terraform runs as. Long-lived access keys carry no MFA context, so every call denies and it reads
as a broken install. Enforcing MFA on an IAM user means an `aws sts get-session-token` dance before
every session — friction that Identity Center simply removes.

## Reference: services Terraform touches

Not a security boundary (see above) — useful only for reading CloudTrail or debugging an
`AccessDenied`:

`s3`, `kms`, `iam`, `logs`, `sts`, `ec2` (VPC/subnet lookups, Aurora security group), `bedrock`
(guardrails, agents, and knowledge bases all use this prefix), `lex`, `sagemaker`, `kendra`, `aoss`,
`glue`, `rds`, `secretsmanager`, `config`, `macie2`, `auditmanager`, `rekognition`, `comprehend`,
plus `iam:CreateServiceLinkedRole` — several of these create one on first use.
