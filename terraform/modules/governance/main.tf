# Governance: AWS Config (what changed), Macie (what sensitive data is sitting in S3), and
# Audit Manager (evidence for a framework). The compliance side of the exam, made concrete.
#
# COST, AND IT IS USAGE-SHAPED RATHER THAN HOURLY:
#   Config    ~$0.003 per configuration item recorded, plus per rule evaluation. An account
#             recording ALL supported resource types can run to real money in a busy account;
#             this module records a narrow list by default for exactly that reason.
#   Macie     ~$1.00/GB for a one-time classification job on the first 50 GB. A small study
#             bucket costs cents; pointing it at a data lake does not.
#   Audit Mgr ~$1.25 per assessment per resource assessed.
#
# None of it bills while idle, but all of it bills on volume — so the gates here narrow scope
# rather than switch things off.

# --------------------------------------------------------------- AWS Config

data "aws_iam_policy_document" "config_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "config" {
  name               = "${var.name_prefix}-config"
  assume_role_policy = data.aws_iam_policy_document.config_trust.json
}

resource "aws_iam_role_policy_attachment" "config_service" {
  role       = aws_iam_role.config.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

data "aws_iam_policy_document" "config_delivery" {
  statement {
    sid       = "WriteConfigHistory"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.bucket_arn}/config/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid       = "ReadBucketAcl"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl"]
    resources = [var.bucket_arn]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "config_delivery" {
  name   = "${var.name_prefix}-config-delivery"
  role   = aws_iam_role.config.id
  policy = data.aws_iam_policy_document.config_delivery.json
}

# The bucket needs a policy allowing Config to write, separate from the role's permissions —
# both sides must agree, which is the S3 cross-service pattern worth internalizing.
resource "aws_s3_bucket_policy" "config" {
  bucket = var.bucket_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ConfigBucketPermissionsCheck"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource  = var.bucket_arn
        Condition = {
          StringEquals = { "AWS:SourceAccount" = var.account_id }
        }
      },
      {
        Sid       = "ConfigBucketDelivery"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${var.bucket_arn}/config/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"      = "bucket-owner-full-control"
            "AWS:SourceAccount" = var.account_id
          }
        }
      }
    ]
  })
}

resource "aws_config_configuration_recorder" "this" {
  name     = "${var.name_prefix}-recorder"
  role_arn = aws_iam_role.config.arn

  recording_group {
    # Narrow by default. all_supported = true is the realistic production setting and the
    # fastest way to turn a study account into a surprising bill.
    all_supported                 = var.record_all_resource_types
    include_global_resource_types = false
    resource_types                = var.record_all_resource_types ? null : var.recorded_resource_types
  }
}

resource "aws_config_delivery_channel" "this" {
  name           = "${var.name_prefix}-delivery"
  s3_bucket_name = var.bucket_name
  s3_key_prefix  = "config"

  # A delivery channel cannot exist before the recorder it delivers for.
  depends_on = [aws_config_configuration_recorder.this]
}

resource "aws_config_configuration_recorder_status" "this" {
  name       = aws_config_configuration_recorder.this.name
  is_enabled = true

  # Starting the recorder before the channel exists fails; this ordering is not optional.
  depends_on = [aws_config_delivery_channel.this]
}

# AWS-managed rules: no Lambda to write, and they evaluate on the resources Config records.
resource "aws_config_config_rule" "s3_public_read" {
  name = "${var.name_prefix}-s3-no-public-read"

  source {
    owner             = "AWS"
    source_identifier = "S3_BUCKET_PUBLIC_READ_PROHIBITED"
  }

  depends_on = [aws_config_configuration_recorder_status.this]
}

resource "aws_config_config_rule" "s3_encrypted" {
  name = "${var.name_prefix}-s3-encryption-enabled"

  source {
    owner             = "AWS"
    source_identifier = "S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED"
  }

  depends_on = [aws_config_configuration_recorder_status.this]
}

# ------------------------------------------------------------------- Macie

resource "aws_macie2_account" "this" {
  count = var.enable_macie ? 1 : 0

  status                       = "ENABLED"
  finding_publishing_frequency = "FIFTEEN_MINUTES"
}

# Managed identifiers cover the usual PII. A custom identifier is how you find something
# specific to your own data — here, anything shaped like an internal study record id.
resource "aws_macie2_custom_data_identifier" "study_id" {
  count = var.enable_macie ? 1 : 0

  name                   = "${var.name_prefix}-study-id"
  description            = "Internal record ids of the form STUDY-00000"
  regex                  = "STUDY-[0-9]{5}"
  maximum_match_distance = 50

  depends_on = [aws_macie2_account.this]
}

# ONE_TIME, not SCHEDULED: a scheduled job re-scans on a cadence and re-bills each time.
resource "aws_macie2_classification_job" "this" {
  count = var.enable_macie ? 1 : 0

  name     = "${var.name_prefix}-scan"
  job_type = "ONE_TIME"

  s3_job_definition {
    bucket_definitions {
      account_id = var.account_id
      buckets    = [var.bucket_name]
    }
  }

  custom_data_identifier_ids = [aws_macie2_custom_data_identifier.study_id[0].id]

  depends_on = [aws_macie2_account.this]
}

# ----------------------------------------------------------- Audit Manager

resource "aws_auditmanager_account_registration" "this" {
  count = var.enable_audit_manager ? 1 : 0
}

# A custom control with a manual evidence source: the simplest thing that demonstrates the
# control -> framework -> assessment hierarchy the exam asks about.
resource "aws_auditmanager_control" "ai_data_handling" {
  count = var.enable_audit_manager ? 1 : 0

  name        = "${var.name_prefix}-ai-data-handling"
  description = "Training data is encrypted at rest and access is logged."

  control_mapping_sources {
    source_name          = "Manual review of bucket encryption and CloudTrail"
    source_set_up_option = "Procedural_Controls_Mapping"
    source_type          = "MANUAL"
  }

  depends_on = [aws_auditmanager_account_registration.this]
}

resource "aws_auditmanager_framework" "this" {
  count = var.enable_audit_manager ? 1 : 0

  name        = "${var.name_prefix}-ai-framework"
  description = "Minimal framework for responsible-AI evidence collection"

  control_sets {
    name = "DataHandling"

    controls {
      id = aws_auditmanager_control.ai_data_handling[0].id
    }
  }
}
