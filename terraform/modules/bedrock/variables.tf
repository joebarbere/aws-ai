variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "region" {
  description = "AWS region. Foundation model availability differs by region — check the Bedrock console before assuming a model is invocable here."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed key encrypting the guardrail."
  type        = string
}

variable "log_group_name" {
  description = "CloudWatch log group for model invocation logs."
  type        = string
}

variable "log_group_arn" {
  description = "ARN of that log group, for the logging role policy."
  type        = string
}
