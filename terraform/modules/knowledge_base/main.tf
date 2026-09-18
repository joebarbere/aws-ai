# Bedrock Knowledge Base: managed RAG over documents in S3.
#
# COST. The vector store is the expensive part, not Bedrock. OpenSearch Serverless bills per
# OCU-hour with a floor of 2 OCUs for a vector collection (1 indexing + 1 search) at ~$0.24/OCU-hr
# — about $350/month, running from the moment the collection exists, query traffic or not.
#
# THE ORDERING PROBLEM. A knowledge base requires a vector index that already exists inside the
# collection, and there is no Terraform resource that creates an index in OpenSearch Serverless —
# it is an HTTP call to the collection endpoint, not a control-plane API. So this module applies
# in two passes:
#
#   1. terraform apply                      -> collection + policies + role
#   2. python/scripts/create_vector_index.py -> the index itself
#   3. terraform apply -var kb_vector_index_ready=true -> the knowledge base + data source
#
# Skipping step 2 makes step 3 fail with a validation error that does not name the real cause.

locals {
  collection_name = "${var.name_prefix}-kb"
}

# ---------------------------------------------------- OpenSearch Serverless
# All three policies must exist before the collection, or creation fails.

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${var.name_prefix}-kb-enc"
  type = "encryption"

  # AWS-owned key rather than the project CMK: AOSS reaches the key through service grants,
  # and pointing it at the project key means widening that key's policy for aoss.amazonaws.com.
  # Worth doing in production, needless complexity for a study stack.
  policy = jsonencode({
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.collection_name}"]
    }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${var.name_prefix}-kb-net"
  type = "network"

  # Public endpoint. The VPC-endpoint alternative is the production answer and needs an
  # aws_opensearchserverless_vpc_endpoint plus a VPC to put it in.
  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.collection_name}"]
      },
      {
        ResourceType = "dashboard"
        Resource     = ["collection/${local.collection_name}"]
      }
    ]
    AllowFromPublic = true
  }])
}

# Data access is separate from IAM: an IAM role with full aoss:* still reads nothing unless a
# data-access policy names it. That surprise is worth meeting here rather than in production.
resource "aws_opensearchserverless_access_policy" "data" {
  name = "${var.name_prefix}-kb-data"
  type = "data"

  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "index"
        Resource     = ["index/${local.collection_name}/*"]
        Permission   = ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"]
      },
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.collection_name}"]
        Permission   = ["aoss:CreateCollectionItems", "aoss:DescribeCollectionItems", "aoss:UpdateCollectionItems"]
      }
    ]
    # Both principals are needed: the Bedrock role to index and query, and whoever runs
    # create_vector_index.py to create the index in the first place.
    Principal = concat([aws_iam_role.kb.arn], var.additional_data_access_principals)
  }])
}

resource "aws_opensearchserverless_collection" "this" {
  name = local.collection_name
  type = "VECTORSEARCH"

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
    aws_opensearchserverless_access_policy.data,
  ]
}

# ------------------------------------------------------------- Bedrock role

data "aws_iam_policy_document" "kb_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "kb" {
  name               = "${var.name_prefix}-kb"
  assume_role_policy = data.aws_iam_policy_document.kb_trust.json
}

data "aws_iam_policy_document" "kb" {
  statement {
    sid       = "InvokeEmbeddingModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }

  statement {
    sid       = "ReadSourceDocuments"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.bucket_arn, "${var.bucket_arn}/*"]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }

  statement {
    sid       = "QueryVectorStore"
    effect    = "Allow"
    actions   = ["aoss:APIAccessAll"]
    resources = [aws_opensearchserverless_collection.this.arn]
  }
}

resource "aws_iam_role_policy" "kb" {
  name   = "${var.name_prefix}-kb"
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}

# --------------------------------------------------------- knowledge base
# Second pass only — see the ordering note at the top of this file.

locals {
  embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"
}

resource "aws_bedrockagent_knowledge_base" "this" {
  count = var.vector_index_ready ? 1 : 0

  name     = "${var.name_prefix}-kb"
  role_arn = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"

    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.this.arn
      vector_index_name = var.vector_index_name

      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }

  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "s3" {
  count = var.vector_index_ready ? 1 : 0

  knowledge_base_id = aws_bedrockagent_knowledge_base.this[0].id
  name              = "${var.name_prefix}-s3"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn         = var.bucket_arn
      inclusion_prefixes = [var.s3_prefix]
    }
  }
}
