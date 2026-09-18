# Kendra: a managed intelligent-search index, and an S3 data source to fill it.
#
# COST, AND THIS ONE IS SEVERE. Kendra Developer Edition bills about $1.125/hr — roughly
# $810/month — from the moment the index is created, with no free tier and no usage required.
# Enterprise Edition is about 4x that. There is no pause. Create it for a session, destroy it
# the same day. The 30-day free trial applies to the first Developer Edition index in an
# account only.

data "aws_iam_policy_document" "index_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["kendra.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "index" {
  name               = "${var.name_prefix}-kendra-index"
  assume_role_policy = data.aws_iam_policy_document.index_trust.json
}

# The index role's job is metrics and logs; it does not read documents. That is the data
# source role's job, below.
data "aws_iam_policy_document" "index" {
  statement {
    sid       = "PublishMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["AWS/Kendra"]
    }
  }

  statement {
    sid       = "DescribeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/kendra/*"]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "index" {
  name   = "${var.name_prefix}-kendra-index"
  role   = aws_iam_role.index.id
  policy = data.aws_iam_policy_document.index.json
}

resource "aws_kendra_index" "this" {
  name        = "${var.name_prefix}-index"
  description = "AIF-C01 study index"
  edition     = var.edition
  role_arn    = aws_iam_role.index.arn

  server_side_encryption_configuration {
    kms_key_id = var.kms_key_arn
  }

  depends_on = [aws_iam_role_policy.index]
}

# --------------------------------------------------------- S3 data source

data "aws_iam_policy_document" "data_source_trust" {
  count = var.enable_s3_data_source ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["kendra.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "data_source" {
  count = var.enable_s3_data_source ? 1 : 0

  name               = "${var.name_prefix}-kendra-datasource"
  assume_role_policy = data.aws_iam_policy_document.data_source_trust[0].json
}

data "aws_iam_policy_document" "data_source" {
  count = var.enable_s3_data_source ? 1 : 0

  statement {
    sid       = "ReadDocuments"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.bucket_arn}/*"]
  }

  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.bucket_arn]
  }

  statement {
    sid       = "IndexDocuments"
    effect    = "Allow"
    actions   = ["kendra:BatchPutDocument", "kendra:BatchDeleteDocument"]
    resources = [aws_kendra_index.this.arn]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "data_source" {
  count = var.enable_s3_data_source ? 1 : 0

  name   = "${var.name_prefix}-kendra-datasource"
  role   = aws_iam_role.data_source[0].id
  policy = data.aws_iam_policy_document.data_source[0].json
}

resource "aws_kendra_data_source" "s3" {
  count = var.enable_s3_data_source ? 1 : 0

  index_id = aws_kendra_index.this.id
  name     = "${var.name_prefix}-s3"
  type     = "S3"
  role_arn = aws_iam_role.data_source[0].arn

  configuration {
    s3_configuration {
      bucket_name = var.bucket_name
      # Only this prefix is crawled, so the bucket can hold unrelated study data.
      inclusion_prefixes = [var.s3_prefix]
    }
  }

  depends_on = [aws_iam_role_policy.data_source]
}
