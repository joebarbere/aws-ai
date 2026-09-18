# governance

AWS Config (what changed), Macie (what sensitive data is sitting in S3), and Audit Manager
(evidence against a framework). The compliance domain of the exam, made concrete.

## Cost is usage-shaped, not hourly

| Service | Charge | Study-scale reality |
|---|---|---|
| Config | ~$0.003 per configuration item, plus rule evaluations | Cents — **unless** `record_all_resource_types = true` |
| Macie | ~$1.00/GB, first 50 GB, one-time job | Cents on a small bucket |
| Audit Manager | ~$1.25 per assessment per resource | Framework and control alone are free |

Nothing here bills while idle, so the gates narrow *scope* rather than switch things off. The one
to respect is `record_all_resource_types` — recording everything is the realistic production
setting and the fastest way to make a study account expensive.

## The pattern worth taking away

**Config needs permission from both sides.** The role gets `s3:PutObject`, *and* the bucket policy
independently allows `config.amazonaws.com` to write. Either one alone fails. That
identity-policy-plus-resource-policy handshake is how most cross-service S3 access works, and it is
a reliable exam question.

**Ordering is not optional.** Recorder → delivery channel → recorder status → rules. Starting a
recorder with no delivery channel fails, which is why the `depends_on` chain is explicit.

## Things to try

Apply, then break a rule on purpose:

```bash
aws s3api put-public-access-block --bucket "$(terraform output -raw data_bucket)" \
  --public-access-block-configuration BlockPublicPolicy=false,BlockPublicAcls=false,IgnorePublicAcls=false,RestrictPublicBuckets=false

aws configservice describe-compliance-by-config-rule \
  --config-rule-names "$(terraform output -json config_rule_names | python3 -c 'import json,sys;print(json.load(sys.stdin)[0])')"
```

Then put the block back. Watching a rule go `NON_COMPLIANT` and return is the whole idea of
continuous compliance in one minute.

For Macie, upload a file containing something like `STUDY-12345` plus a fake email address and run
the job — the custom identifier catches the first, the managed identifiers catch the second. That
split, custom versus managed, is exactly what Macie questions turn on.
