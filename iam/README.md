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
[`cost-seatbelt.json`](cost-seatbelt.json) (a copy — see [below](#attaching-the-seatbelt)), which binds *regardless* of privilege because an
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

> **This file is a copy. The source of truth is
> [`jansky-research/infra/seatbelt.json`](https://github.com/joebarbere/jansky-research/blob/main/infra/seatbelt.json).**
> Both repos share one AWS account (separated by the `Project` tag), and a permission set holds
> one inline policy, so there is one seatbelt for both. It has been attached to
> `AdministratorAccess` since 2026-09-26. To change it: edit the jansky-research file, copy it
> here byte-for-byte, re-attach, re-provision, then run
> `make -C ../jansky-research seatbelt-check` — it compares this copy and the **live** attached
> policy against the source and exits non-zero on any drift.

IAM policy JSON cannot carry comments, and IAM rejects unknown keys. The one free-text field it
allows is the optional top-level **`Id`**, so that is where the policy names its source of truth
(`source-of-truth:jansky-research/infra/seatbelt.json;…`). IAM ignores `Id` when evaluating.

From the CLI (what the check and plan 96 use):

```fish
aws sso-admin put-inline-policy-to-permission-set --instance-arn $INST \
    --permission-set-arn $PS --inline-policy file://../jansky-research/infra/seatbelt.json
aws sso-admin provision-permission-set --instance-arn $INST --permission-set-arn $PS \
    --target-type ALL_PROVISIONED_ACCOUNTS
```

Or in the console: IAM Identity Center → Permission sets → your set → **Inline policy** → paste
→ **Provision to AWS accounts**. That last step is the one people forget: an edited permission set
does nothing until it is re-provisioned.

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

To study one of them, delete that line **in the jansky-research source**, copy, re-provision,
apply, and put it back afterwards. The friction is the feature — it mirrors the `enable_*`
variables one level lower, where a typo in a `-var` flag cannot bypass it.

**EC2 instance-type allowlist** (added for jansky-research's cloud leg). `ec2:RunInstances` is
denied for any instance type outside a short list of small, CPU and single-GPU types (`t3`/`t4g`
small, `c7i`/`c7a`, `m7a`/`r7a`, `g4dn`/`g5`/`g6` up to `2xlarge`, `g6e.xlarge`). An 8×H100
`p5.48xlarge` is ~$55/hour; this is the line that makes that a denied call instead of an invoice.

**No NAT gateways, fleets or commitments.** `CreateNatGateway` (hourly plus per-GB, forever),
`CreateFleet` / `RequestSpotFleet` / `RequestSpotInstances` (they launch through paths the
allowlist does not see — use `run-instances --instance-market-options` for spot), dedicated hosts,
capacity reservations, Reserved Instances and Savings Plans (a one-click multi-year commitment).

Every deny was proven by a `--dry-run` call on 2026-09-26, not assumed: `p5.48xlarge` and
`g6e.2xlarge` denied, `g6.xlarge` / `g5.xlarge` spot / `t3.micro` allowed, NAT gateway on a real
subnet denied, `us-west-2` denied.

## Two things IAM will not do

**It will not cap spend.** No policy limits cost; the deny list is a proxy for the specific
resources that bill hourly. The real backstop is the budgets, which exist since 2026-09-26:
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
