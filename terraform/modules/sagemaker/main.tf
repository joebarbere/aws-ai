# SageMaker Studio: a domain, a user profile, and an optional real-time endpoint.
#
# COST. The domain itself is free; what bills is what runs inside it. A JupyterLab app on
# ml.t3.medium is ~$0.05/hr, and it keeps billing until you shut the app down — closing the
# browser tab does not stop it. An endpoint bills from CreateEndpoint to DeleteEndpoint
# regardless of traffic, which is the single most expensive mistake available in this repo.

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

  # An endpoint needs both an inference image and model artifacts; without them there is
  # nothing to deploy, so the endpoint resources stay out of the plan entirely.
  create_endpoint = var.enable_endpoint && var.model_image != null && var.model_data_url != null
}

# ------------------------------------------------------------- execution role

data "aws_iam_policy_document" "execution_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-sagemaker-execution"
  assume_role_policy = data.aws_iam_policy_document.execution_trust.json
}

# Broad by design: Studio needs wide SageMaker access to be usable for study, and narrowing
# it turns every notebook into an IAM debugging session. Data access below is NOT broad —
# it is scoped to this project's bucket and key.
resource "aws_iam_role_policy_attachment" "sagemaker_full" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
}

data "aws_iam_policy_document" "data_access" {
  statement {
    sid       = "ProjectBucket"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
    resources = [var.bucket_arn, "${var.bucket_arn}/*"]
  }

  statement {
    sid       = "ProjectKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "data_access" {
  name   = "${var.name_prefix}-sagemaker-data"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.data_access.json
}

# ------------------------------------------------------------------- domain

resource "aws_sagemaker_domain" "this" {
  domain_name = "${var.name_prefix}-studio"
  auth_mode   = "IAM"
  vpc_id      = local.vpc_id
  subnet_ids  = local.subnet_ids
  kms_key_id  = var.kms_key_arn

  # PublicInternetOnly keeps the study setup simple. VpcOnly is the production answer and
  # requires interface endpoints for every service Studio talks to.
  app_network_access_type = "PublicInternetOnly"

  default_user_settings {
    execution_role = aws_iam_role.execution.arn
  }

  retention_policy {
    home_efs_file_system = "Delete"
  }
}

resource "aws_sagemaker_user_profile" "this" {
  domain_id         = aws_sagemaker_domain.this.id
  user_profile_name = var.user_profile_name

  user_settings {
    execution_role = aws_iam_role.execution.arn
  }
}

# --------------------------------------------------------- optional endpoint

resource "aws_sagemaker_model" "this" {
  count = local.create_endpoint ? 1 : 0

  name               = "${var.name_prefix}-model"
  execution_role_arn = aws_iam_role.execution.arn

  primary_container {
    image          = var.model_image
    model_data_url = var.model_data_url
  }
}

resource "aws_sagemaker_endpoint_configuration" "this" {
  count = local.create_endpoint ? 1 : 0

  name = "${var.name_prefix}-endpoint-config"

  production_variants {
    variant_name           = "AllTraffic"
    model_name             = aws_sagemaker_model.this[0].name
    initial_instance_count = 1
    instance_type          = var.endpoint_instance_type
    initial_variant_weight = 1
  }
}

resource "aws_sagemaker_endpoint" "this" {
  count = local.create_endpoint ? 1 : 0

  name                 = "${var.name_prefix}-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.this[0].name
}

# ----------------------------------------------------------- Feature Store
#
# The offline store is S3 (cheap, for training); the online store is a managed low-latency
# key-value store billed per GB-month plus reads and writes. Enabling only the offline store
# is the frugal default, and the online/offline split is itself an exam topic.

resource "aws_sagemaker_feature_group" "this" {
  count = var.enable_feature_store ? 1 : 0

  feature_group_name             = "${var.name_prefix}-features"
  record_identifier_feature_name = "record_id"
  event_time_feature_name        = "event_time"
  role_arn                       = aws_iam_role.execution.arn
  description                    = "Worked example feature group for the AIF-C01 study stack"

  feature_definition {
    feature_name = "record_id"
    feature_type = "String"
  }

  # Event time must be String (ISO-8601) or Fractional (epoch seconds) — there is no
  # timestamp type, which surprises everyone once.
  feature_definition {
    feature_name = "event_time"
    feature_type = "String"
  }

  feature_definition {
    feature_name = "score"
    feature_type = "Fractional"
  }

  online_store_config {
    enable_online_store = var.enable_online_feature_store
  }

  offline_store_config {
    s3_storage_config {
      s3_uri     = "s3://${var.bucket_name}/feature-store/"
      kms_key_id = var.kms_key_arn
    }
  }
}

# ------------------------------------------------------------ Model Monitor
#
# Monitoring only makes sense against a live endpoint, and it needs a region-specific
# model-monitor container image — so it is gated on both. Each scheduled run is a processing
# job billed by the minute, so an hourly schedule on ml.m5.large is not free.

locals {
  create_monitor = (
    local.create_endpoint && var.enable_model_monitor && var.model_monitor_image_uri != null
  )
}

resource "aws_sagemaker_data_quality_job_definition" "this" {
  count = local.create_monitor ? 1 : 0

  name     = "${var.name_prefix}-data-quality"
  role_arn = aws_iam_role.execution.arn

  data_quality_app_specification {
    image_uri = var.model_monitor_image_uri
  }

  data_quality_job_input {
    endpoint_input {
      endpoint_name = aws_sagemaker_endpoint.this[0].name
      local_path    = "/opt/ml/processing/input"
    }
  }

  data_quality_job_output_config {
    monitoring_outputs {
      s3_output {
        s3_uri     = "s3://${var.bucket_name}/model-monitor/"
        local_path = "/opt/ml/processing/output"
      }
    }
  }

  job_resources {
    cluster_config {
      instance_count    = 1
      instance_type     = "ml.m5.large"
      volume_size_in_gb = 20
    }
  }
}

resource "aws_sagemaker_monitoring_schedule" "this" {
  count = local.create_monitor ? 1 : 0

  name = "${var.name_prefix}-data-quality"

  monitoring_schedule_config {
    monitoring_job_definition_name = aws_sagemaker_data_quality_job_definition.this[0].name
    monitoring_type                = "DataQuality"

    schedule_config {
      schedule_expression = var.monitor_schedule_expression
    }
  }
}
