# bedrock

A guardrail (content filters, a denied topic, PII handling) plus account-level model invocation
logging.

**Idle cost:** none. Guardrails bill per text unit *evaluated*, and invocation logging bills as
CloudWatch ingestion. Neither charges while you are not calling a model.

**Two things Terraform cannot do for you:**

1. **Model access.** Enabling a foundation model is an account entitlement granted in the Bedrock
   console. There is no resource for it, and `InvokeModel` fails with `AccessDeniedException`
   until you do it.
2. **Region.** Model availability differs by region. A guardrail applies fine in a region with no
   models enabled — and then every invocation fails.

The invocation logging configuration is a **singleton per account per region**. Two stacks in one
region will overwrite each other's setting.
