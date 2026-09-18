# Bedrock: the two things it genuinely provisions at the Practitioner level.
#
#   1. A guardrail — the exam's "responsible AI" domain made concrete.
#   2. Model invocation logging — the governance/observability answer.
#
# Model *access* is not Terraform-managed: enabling a foundation model in the console is an
# account-level entitlement, and it has no resource here. Enable the models you want in the
# Bedrock console before invoking anything.

resource "aws_bedrock_guardrail" "this" {
  name                      = "${var.name_prefix}-guardrail"
  description               = "Study guardrail: content filters, denied topic, PII masking."
  blocked_input_messaging   = "That request was blocked by the guardrail."
  blocked_outputs_messaging = "The response was blocked by the guardrail."
  kms_key_arn               = var.kms_key_arn

  content_policy_config {
    filters_config {
      type            = "HATE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "VIOLENCE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE" # Prompt-attack filtering applies to input only.
    }
  }

  topic_policy_config {
    topics_config {
      name       = "FinancialAdvice"
      type       = "DENY"
      definition = "Individualized recommendations to buy, sell, or hold specific securities."
      examples   = ["Which stock should I buy today?"]
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "EMAIL"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "US_SOCIAL_SECURITY_NUMBER"
      action = "BLOCK"
    }
  }
}

resource "aws_bedrock_guardrail_version" "this" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Initial version"
}

# ------------------------------------------------------- invocation logging
# One configuration per account per region — applying this in two stacks in the
# same region will fight over the same singleton.

data "aws_iam_policy_document" "logging_trust" {
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

resource "aws_iam_role" "logging" {
  name               = "${var.name_prefix}-bedrock-logging"
  assume_role_policy = data.aws_iam_policy_document.logging_trust.json
}

resource "aws_iam_role_policy" "logging" {
  name = "${var.name_prefix}-bedrock-logging"
  role = aws_iam_role.logging.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${var.log_group_arn}:*"
    }]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  logging_config {
    embedding_data_delivery_enabled = true
    image_data_delivery_enabled     = false
    text_data_delivery_enabled      = true

    cloudwatch_config {
      log_group_name = var.log_group_name
      role_arn       = aws_iam_role.logging.arn
    }
  }

  depends_on = [aws_iam_role_policy.logging]
}
