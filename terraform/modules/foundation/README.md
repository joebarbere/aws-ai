# foundation

KMS key, S3 bucket, IAM role, and CloudWatch log group — the shared base the AI services read from,
write to, and log into.

**Idle cost:** a customer-managed KMS key is $1/month. S3 and CloudWatch bill on what you store.
Nothing here bills hourly, so it is safe to leave applied between study sessions.

**What it deliberately does not do:** create per-service resources for Comprehend, Rekognition,
Textract, Transcribe, or Translate. Those are called, not provisioned — this module plus a boto3
client is all they need.
