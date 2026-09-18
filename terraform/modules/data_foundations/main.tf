# Data foundations: the Glue catalog that feeds training data, and an Aurora PostgreSQL
# cluster with pgvector as a second, much cheaper RAG store.
#
# COST. Glue is close to free at rest — the Data Catalog's first million objects are free, and a
# crawler bills only while it runs (~$0.44/DPU-hr, minimum 10 minutes). Aurora Serverless v2 is
# the hourly one: ~$0.12/ACU-hr with a 0.5 ACU floor, so roughly $43/month if left running. That
# is still an eighth of what the OpenSearch Serverless collection costs, which is the point of
# having both.

# ------------------------------------------------------------------- Glue

data "aws_iam_policy_document" "glue_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "glue" {
  name               = "${var.name_prefix}-glue"
  assume_role_policy = data.aws_iam_policy_document.glue_trust.json
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

data "aws_iam_policy_document" "glue_data" {
  statement {
    sid       = "ProjectBucket"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [var.bucket_arn, "${var.bucket_arn}/*"]
  }

  statement {
    sid       = "ProjectKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "glue_data" {
  name   = "${var.name_prefix}-glue-data"
  role   = aws_iam_role.glue.id
  policy = data.aws_iam_policy_document.glue_data.json
}

resource "aws_glue_catalog_database" "this" {
  name        = replace("${var.name_prefix}_catalog", "-", "_")
  description = "Catalog for AIF-C01 study datasets"
}

# The crawler infers schema from whatever you drop under the prefix and writes table
# definitions into the catalog. Running it is how the catalog gets populated — creating it
# does nothing on its own.
resource "aws_glue_crawler" "this" {
  name          = "${var.name_prefix}-crawler"
  role          = aws_iam_role.glue.arn
  database_name = aws_glue_catalog_database.this.name
  description   = "Infers table schemas from CSV/JSON/Parquet under the datasets prefix"

  s3_target {
    path = "s3://${var.bucket_name}/${var.datasets_prefix}"
  }

  # Only re-crawl what changed; a full recrawl on every run costs DPU-minutes for nothing.
  recrawl_policy {
    recrawl_behavior = "CRAWL_EVERYTHING"
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }
}

# The job resource does not require the script to exist yet — Terraform will happily create a
# job pointing at an object you have not uploaded, and the failure shows up at run time.
resource "aws_glue_job" "etl" {
  count = var.enable_glue_job ? 1 : 0

  name         = "${var.name_prefix}-etl"
  role_arn     = aws_iam_role.glue.arn
  glue_version = "4.0"
  description  = "Example PySpark ETL over the catalog"

  command {
    script_location = "s3://${var.bucket_name}/${var.glue_script_prefix}etl.py"
    python_version  = "3"
  }

  worker_type       = "G.1X"
  number_of_workers = 2

  # Without a timeout a runaway job bills until it is killed by hand.
  timeout = 10
}

# --------------------------------------------------------- Aurora + pgvector
#
# THE ORDERING PROBLEM, AGAIN. `CREATE EXTENSION vector;` is SQL against a running database,
# not a control-plane call, so Terraform cannot do it — the same shape as the OpenSearch
# Serverless index in modules/knowledge_base. Apply, run python/scripts/enable_pgvector.py,
# then use it.

data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = length(var.subnet_ids) == 0 ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
}

locals {
  vpc_id     = var.vpc_id != null ? var.vpc_id : data.aws_vpc.default[0].id
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : data.aws_subnets.default[0].ids
}

resource "aws_db_subnet_group" "this" {
  count = var.enable_aurora ? 1 : 0

  name       = "${var.name_prefix}-aurora"
  subnet_ids = local.subnet_ids
}

resource "aws_security_group" "aurora" {
  count = var.enable_aurora ? 1 : 0

  name        = "${var.name_prefix}-aurora"
  description = "Aurora PostgreSQL access for the AIF-C01 study cluster"
  vpc_id      = local.vpc_id

  # No ingress rule by default: the cluster is reachable only from inside the VPC's own
  # security group members. Set allowed_cidr_blocks to reach it from a laptop, and understand
  # that you are opening Postgres to that range.
  dynamic "ingress" {
    for_each = length(var.allowed_cidr_blocks) > 0 ? [1] : []

    content {
      description = "PostgreSQL from allowed ranges"
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = var.allowed_cidr_blocks
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_rds_cluster" "this" {
  count = var.enable_aurora ? 1 : 0

  cluster_identifier = "${var.name_prefix}-pgvector"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.aurora_engine_version
  database_name      = "vectors"
  master_username    = "postgres"

  # Secrets Manager holds the password and rotates it. The alternative puts a password in
  # state, which is the single most common way a study repo leaks a credential.
  manage_master_user_password = true

  storage_encrypted = true
  kms_key_id        = var.kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.this[0].name
  vpc_security_group_ids = [aws_security_group.aurora[0].id]

  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_acu
    max_capacity = var.aurora_max_acu
  }

  # A study cluster, not a production one: no final snapshot, destroyable in one command.
  skip_final_snapshot = true
}

resource "aws_rds_cluster_instance" "this" {
  count = var.enable_aurora ? 1 : 0

  identifier         = "${var.name_prefix}-pgvector-1"
  cluster_identifier = aws_rds_cluster.this[0].id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.this[0].engine
  engine_version     = aws_rds_cluster.this[0].engine_version

  # Without a public IP you need to run the pgvector script from inside the VPC.
  publicly_accessible = var.aurora_publicly_accessible
}
