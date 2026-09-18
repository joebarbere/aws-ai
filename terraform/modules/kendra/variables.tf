variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "region" {
  description = "AWS region, used to scope the log-group ARNs in the index role."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed key encrypting the index."
  type        = string
}

variable "bucket_name" {
  description = "Name of the bucket holding documents to index."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of that bucket."
  type        = string
}

variable "edition" {
  description = "DEVELOPER_EDITION (~$810/month) or ENTERPRISE_EDITION (~4x that). Both bill hourly from creation."
  type        = string
  default     = "DEVELOPER_EDITION"

  validation {
    condition     = contains(["DEVELOPER_EDITION", "ENTERPRISE_EDITION"], var.edition)
    error_message = "edition must be DEVELOPER_EDITION or ENTERPRISE_EDITION."
  }
}

variable "enable_s3_data_source" {
  description = "Create the S3 data source and its role. The data source itself is free; the index it feeds is not."
  type        = bool
  default     = true
}

variable "s3_prefix" {
  description = "Key prefix crawled by the data source, so unrelated objects in the bucket are left alone."
  type        = string
  default     = "kendra/"
}
