variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the project data bucket the execution role may read and write."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed key for the Studio EFS volume and project data."
  type        = string
}

variable "vpc_id" {
  description = "VPC for the Studio domain. Null uses the account's default VPC."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnets for the Studio domain. Empty uses all subnets in the chosen VPC."
  type        = list(string)
  default     = []
}

variable "user_profile_name" {
  description = "Studio user profile name."
  type        = string
  default     = "student"
}

# ------------------------------------------------------------- endpoint gate

variable "enable_endpoint" {
  description = "Deploy a real-time inference endpoint. Bills per hour from creation to deletion, with no traffic required — ml.m5.large is roughly $0.13/hr, about $95/month if forgotten."
  type        = bool
  default     = false
}

variable "model_image" {
  description = "ECR URI of the inference container. Required for an endpoint; null means no endpoint is created even when enable_endpoint is true."
  type        = string
  default     = null
}

variable "model_data_url" {
  description = "S3 URI of model artifacts (model.tar.gz). Required for an endpoint."
  type        = string
  default     = null
}

variable "endpoint_instance_type" {
  description = "Instance type backing the endpoint."
  type        = string
  default     = "ml.m5.large"
}

variable "bucket_name" {
  description = "Project bucket name, for the feature store's offline S3 location and monitor output."
  type        = string
}

# ------------------------------------------------------------ MLOps extras

variable "enable_feature_store" {
  description = "Create a worked-example feature group. The offline (S3) store costs storage only."
  type        = bool
  default     = false
}

variable "enable_online_feature_store" {
  description = "Also enable the online store: low-latency lookups billed per GB-month plus reads and writes. The offline store alone is the frugal default."
  type        = bool
  default     = false
}

variable "enable_model_monitor" {
  description = "Schedule data-quality monitoring against the endpoint. Requires an endpoint and model_monitor_image_uri; each run is a processing job billed by the minute."
  type        = bool
  default     = false
}

variable "model_monitor_image_uri" {
  description = "Region-specific model-monitor container image URI. There is no single public value — look up the analyzer image for your region. Null disables monitoring."
  type        = string
  default     = null
}

variable "monitor_schedule_expression" {
  description = "How often the monitoring job runs. Hourly is the SageMaker minimum for a cron schedule."
  type        = string
  default     = "cron(0 * ? * * *)"
}
