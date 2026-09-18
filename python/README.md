# aws_ai

Thin boto3 wrappers for the AWS AI services on the AIF-C01 exam.

Every function takes its client as an argument instead of constructing one. That keeps the tests
offline — a fake client stands in for `bedrock-runtime` and `comprehend` — and lets a caller pass a
client configured for a different region or profile without any change here.

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```

Usage:

```python
import boto3
from aws_ai import detect_sentiment, invoke_text_model

reply = invoke_text_model(
    boto3.client("bedrock-runtime", region_name="us-east-1"),
    "amazon.nova-lite-v1:0",
    "Explain inference latency in one sentence.",
    guardrail_id="<terraform output bedrock_guardrail_id>",
    guardrail_version="1",
)
print(reply.text, reply.blocked_by_guardrail)

sentiment = detect_sentiment(boto3.client("comprehend"), "This exam guide is excellent.")
print(sentiment.label, sentiment.confidence)
```

Calling Bedrock needs model access enabled in the console for that account and region; Terraform
cannot grant it.
