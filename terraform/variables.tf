variable "region" {
  description = "AWS region. Bedrock model availability varies by region; us-east-1 and us-west-2 carry the widest selection."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for all resource names. Must be globally unique enough for S3 bucket naming."
  type        = string
  default     = "aws-ai-study"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,32}$", var.name_prefix))
    error_message = "name_prefix must be 3-32 chars of lowercase letters, digits, or hyphens."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for AI service logs."
  type        = number
  default     = 14
}

# ---------------------------------------------------------------- cost gates
# Everything below bills by the hour whether or not you call it. Default false;
# turn on for a session, then `terraform destroy`.

variable "enable_bedrock" {
  description = "Bedrock guardrail + model invocation logging. Guardrails bill per text unit evaluated, not hourly."
  type        = bool
  default     = true
}

variable "enable_sagemaker_domain" {
  description = "SageMaker Studio domain. Apps inside it bill hourly (~$0.05/hr and up per running app)."
  type        = bool
  default     = false
}

variable "enable_kendra" {
  description = "Kendra index. Developer Edition is roughly $810/month with no free tier. Leave off unless actively studying it."
  type        = bool
  default     = false
}
