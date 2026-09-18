variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for AI inputs and outputs."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 14
}

variable "account_id" {
  description = "AWS account id, used for the confused-deputy conditions on service trust policies."
  type        = string
}

variable "region" {
  description = "AWS region, used in the CloudWatch Logs key-policy principal."
  type        = string
}
