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
the cost seatbelt (now in [`aws-cloud`](https://github.com/joebarbere/aws-cloud) — see [below](#attaching-the-seatbelt)), which binds *regardless* of privilege because an
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
| SSO session name | `cloud` |
| SSO start URL | your `d-xxxx.awsapps.com/start` URL |
| SSO region | `us-east-1` |
| SSO registration scopes | accept the default (`sso:account:access`) |
| Account / role | your account, `AdministratorAccess` |
| CLI default client region | `us-east-1` |
| CLI default output format | `json` |
| CLI profile name | `joebarbere-admin` |

A browser opens to approve a device code. Nothing secret is written — just a cached token under
`~/.aws/sso/cache/`. The result in `~/.aws/config`:

```ini
[profile joebarbere-admin]
sso_session = cloud
sso_account_id = <account id>
sso_role_name = AdministratorAccess
region = us-east-1
output = json

[sso-session cloud]
sso_start_url = https://d-xxxxxxxxxx.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
```

### 3. Use it

```fish
aws sso login --profile joebarbere-admin     # opens a browser; token lasts the session duration
set -Ux AWS_PROFILE joebarbere-admin         # fish universal var; bash/zsh: export AWS_PROFILE=joebarbere-admin
aws sts get-caller-identity                  # confirm the account and AWSReservedSSO_AdministratorAccess role
```

Terraform needs no changes: `providers.tf` hardcodes no profile, and the AWS provider resolves SSO
tokens natively at `~> 5.70`.

| Symptom | Fix |
|---|---|
| `No valid credential sources found` | `AWS_PROFILE` is unset and there is no `default` profile — run the `set -Ux` above |
| `ExpiredToken` / `SSOTokenProviderFailure` | the token expired — `aws sso login --profile joebarbere-admin` |
| done for the day | `aws sso logout` clears the cached token |

## Attaching the seatbelt

**It is attached, and it lives in [`aws-cloud`](https://github.com/joebarbere/aws-cloud)** —
the account-wide Terraform repo for the one AWS account this repo shares with `jansky-research`.
Since 2026-09-26 the seatbelt is `aws-cloud/terraform/seatbelt.json`, managed as
`aws_ssoadmin_permission_set_inline_policy` on `AdministratorAccess` (the provider
re-provisions the permission set on change, the step people forget in the console). There is no
copy here any more: `terraform plan` in `aws-cloud` is the drift check.

What it denies (full table and dry-run proof in the `aws-cloud` README):

- **Region lock** — everything outside `us-east-1`, global services exempted via `NotAction`
  (omit those and you break IAM, STS and the SSO console). It stops a stray `-var region=` from
  quietly creating an $810/month Kendra index somewhere you never look.
- **Hourly-billed services** — the entire hourly surface of this repo:

  | Action | Resource | Rate |
  |---|---|---|
  | `kendra:CreateIndex` | Kendra index | ~$810/month |
  | `aoss:CreateCollection` | OpenSearch Serverless | ~$350/month |
  | `sagemaker:CreateEndpoint` | Inference endpoint | ~$95/month |
  | `rds:CreateDBCluster` / `CreateDBInstance` | Aurora Serverless v2 | ~$43/month |

- **EC2 instance-type allowlist** and **no NAT gateways, fleets or commitments** (added for
  jansky-research's cloud leg; they bind here too).

**To study a denied service:** delete that action in `aws-cloud/terraform/seatbelt.json`,
`make plan && make apply` there, apply this repo's module, study, `terraform destroy` here, then
put the line back and apply `aws-cloud` again. The friction is the feature — it mirrors the
`enable_*` variables one level lower, where a typo in a `-var` flag cannot bypass it.

**Comments in the policy:** IAM policy JSON allows none and rejects unknown keys; the optional
top-level `Id` is the one free-text field (ignored when evaluating), and it names `aws-cloud` as
the source of truth.

## Two things IAM will not do

**It will not cap spend.** No policy limits cost; the deny list is a proxy for the specific
resources that bill hourly. The real backstop is the budgets — managed in `aws-cloud` since 2026-09-26:
`account-monthly-25` ($25/month, account-wide, alerts only — email at 50/80/100% actual and 100%
forecast), plus `aws-ai-monthly` and `jansky-research-monthly` filtered on the `Project`
cost-allocation tag (activated the same day, so the split starts then), and a per-service
cost-anomaly monitor that emails daily for anomalies of $5 or more.

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
