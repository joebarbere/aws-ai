# Bedrock Knowledge Base backed by Aurora PostgreSQL + pgvector — Bedrock's "RDS" store, the
# alternative to OpenSearch Serverless in ../knowledge_base.
#
# COST. Aurora Serverless v2 bills per ACU-hour (~$0.12) from a 0.5 ACU floor: about $0.06/hour,
# $43/month if forgotten — an eighth of the OpenSearch Serverless collection's 2-OCU floor.
#
# WHAT TERRAFORM DOES NOT DO. The table and its indexes are SQL, which Terraform cannot run.
# And the knowledge base itself needs rds_configuration.field_mapping.custom_metadata_field,
# which the AWS provider added in 6.22 — this repo pins ~> 5.70. Without that column Bedrock
# expects one table column per metadata attribute instead. So:
#
#   1. terraform apply -var enable_knowledge_base_rds=true   -> cluster, secrets, role
#   2. python/scripts/rds_kb.py create                       -> schema, bedrock_user, table,
#                                                               knowledge base, data source
#   3. python/scripts/rds_kb.py delete, then terraform apply -var enable_knowledge_base_rds=false
#
# Everything goes through the RDS Data API — Bedrock requires it, and it means the cluster
# needs no public address and no ingress rule for step 2 to work from a laptop.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-kb-rds"
  subnet_ids = data.aws_subnets.default.ids
}

# No ingress at all: the Data API reaches the cluster without a network path from the caller.
resource "aws_security_group" "this" {
  name        = "${var.name_prefix}-kb-rds"
  description = "Aurora for the Bedrock knowledge base; Data API only, no ingress"
  vpc_id      = data.aws_vpc.default.id
}

# Log every statement, so CloudWatch shows the exact SQL Bedrock runs: its inserts, its
# similarity and hybrid queries, how it translates metadata filters, how it computes `score`.
# That is the point of this module; turn it off for anything long-lived, it is verbose.
resource "aws_rds_cluster_parameter_group" "this" {
  name   = "${var.name_prefix}-kb-rds"
  family = "aurora-postgresql${split(".", var.engine_version)[0]}"

  parameter {
    name  = "log_statement"
    value = var.log_statements ? "all" : "none"
  }
}

# Created here, before the cluster exports to it, so it gets a retention period and is
# destroyed with the stack instead of lingering with "never expire".
resource "aws_cloudwatch_log_group" "postgresql" {
  name              = "/aws/rds/cluster/${var.name_prefix}-kb-rds/postgresql"
  retention_in_days = 1
}

resource "aws_rds_cluster" "this" {
  cluster_identifier = "${var.name_prefix}-kb-rds"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.engine_version
  database_name      = "bedrock"
  master_username    = "postgres"

  # The master password lives in a secret Aurora manages; it never touches state.
  manage_master_user_password = true

  # The RDS Data API — Bedrock's only way in.
  enable_http_endpoint = true

  storage_encrypted = true
  kms_key_id        = var.kms_key_arn

  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name
  enabled_cloudwatch_logs_exports = ["postgresql"]

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]

  serverlessv2_scaling_configuration {
    # Not 0: a paused cluster takes ~15 s to resume, longer than Bedrock waits on a query.
    min_capacity = 0.5
    max_capacity = var.max_acu
  }

  skip_final_snapshot = true

  depends_on = [aws_cloudwatch_log_group.postgresql]
}

resource "aws_rds_cluster_instance" "this" {
  identifier         = "${var.name_prefix}-kb-rds-1"
  cluster_identifier = aws_rds_cluster.this.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version
}

# bedrock_user's credentials, as AWS's setup guide has you create them. Terraform makes the
# empty secret; rds_kb.py generates the password and stores it, so it stays out of
# state too.
resource "aws_secretsmanager_secret" "bedrock_user" {
  name                    = "${var.name_prefix}-kb-rds-bedrock-user"
  description             = "Credentials Bedrock uses to reach the knowledge base table"
  recovery_window_in_days = 0
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
  name               = "${var.name_prefix}-kb-rds"
  assume_role_policy = data.aws_iam_policy_document.kb_trust.json
}

locals {
  embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"
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

  # The three permissions AWS lists for an Aurora vector store.
  statement {
    sid       = "DescribeCluster"
    effect    = "Allow"
    actions   = ["rds:DescribeDBClusters"]
    resources = [aws_rds_cluster.this.arn]
  }

  statement {
    sid       = "QueryVectorStore"
    effect    = "Allow"
    actions   = ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"]
    resources = [aws_rds_cluster.this.arn]
  }

  statement {
    sid       = "ReadCredentials"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.bedrock_user.arn]
  }
}

resource "aws_iam_role_policy" "kb" {
  name   = "${var.name_prefix}-kb-rds"
  role   = aws_iam_role.kb.id
  policy = data.aws_iam_policy_document.kb.json
}
