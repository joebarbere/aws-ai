"""A local re-implementation of an Amazon Bedrock knowledge base (see local-kb/PLAN.md).

RustFS stands in for S3, OpenSearch for OpenSearch Serverless, and LM Studio for the embedding
and generation models. The aim is fidelity to what a real knowledge base writes and returns, as
recorded in local-kb/fixtures/, not a general-purpose RAG framework.
"""
