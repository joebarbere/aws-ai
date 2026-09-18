# The shared base every AI service call needs: a key, a bucket, a role, a log group.
#
# Comprehend, Rekognition, Textract, Transcribe, and Translate provision nothing of their own —
# they read an object, write a result, and log. This module is the whole of their infrastructure.

resource "aws_kms_key" "this" {
  description             = "${var.name_prefix} — encrypts AI training data, inputs, and logs"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.this.key_id
}

# Let CloudWatch Logs use the key, or encrypted log groups fail to write.
resource "aws_kms_key_policy" "this" {
  key_id = aws_kms_key.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountRoot"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.region}:${var.account_id}:log-group:*"
          }
        }
      }
    ]
  })
}

resource "aws_s3_bucket" "data" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_cloudwatch_log_group" "ai" {
  name              = "/aws/ai/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn

  depends_on = [aws_kms_key_policy.this]
}

# ------------------------------------------------------------------ IAM role
# One role the serverless AI services assume to reach the bucket. The SourceAccount
# condition is what stops a confused-deputy: without it, any account's Comprehend job
# could name this role.

data "aws_iam_policy_document" "ai_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      identifiers = [
        "comprehend.amazonaws.com",
        "rekognition.amazonaws.com",
        "textract.amazonaws.com",
        "transcribe.amazonaws.com",
        "translate.amazonaws.com",
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "ai_service" {
  name               = "${var.name_prefix}-ai-service"
  assume_role_policy = data.aws_iam_policy_document.ai_trust.json
}

data "aws_iam_policy_document" "ai_access" {
  statement {
    sid       = "ReadWriteDataBucket"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.this.arn]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ai.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ai_access" {
  name   = "${var.name_prefix}-ai-access"
  role   = aws_iam_role.ai_service.id
  policy = data.aws_iam_policy_document.ai_access.json
}
