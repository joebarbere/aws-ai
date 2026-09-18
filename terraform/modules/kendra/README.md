# kendra

A Kendra index plus an S3 data source that crawls a prefix of the project bucket.

## Read this before applying

**Developer Edition bills about $1.125/hr — roughly $810/month — from the moment the index is
created.** There is no free tier beyond a 30-day trial on the first Developer Edition index in an
account, no usage requirement, and no pause. Enterprise Edition is about four times that.

This is the most expensive resource in the repo by a wide margin. Create it for a session and
destroy it the same day:

```bash
terraform apply  -var enable_kendra=true
# ... study ...
terraform destroy -var enable_kendra=true
```

Index creation also takes roughly 30 minutes, so `apply` will sit there for a while.

## The two-role split

Kendra uses one role for itself and a different one per data source:

- **Index role** — publishes CloudWatch metrics and writes logs. It does *not* read documents.
- **Data source role** — reads S3 objects and calls `BatchPutDocument` on the index.

Collapsing those into one role works, and is exactly the habit that turns into an over-permissioned
production index later.

## Indexing something

Put documents under the crawled prefix, then sync:

```bash
aws s3 cp notes.pdf "s3://$(terraform output -raw data_bucket)/kendra/"
aws kendra start-data-source-sync-job \
  --index-id "$(terraform output -raw kendra_index_id)" \
  --id "$(terraform output -raw kendra_data_source_id)"
```
