variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for trust and bucket-policy conditions."
  type        = string
}

variable "bucket_name" {
  description = "Project bucket: Config delivers history here and Macie scans it."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of that bucket."
  type        = string
}

variable "kms_key_arn" {
  description = "Project key, so Config can encrypt delivered history."
  type        = string
}

variable "record_all_resource_types" {
  description = "Record every supported resource type. Realistic for production and the fastest way to run up a Config bill in a study account — each configuration item is ~$0.003."
  type        = bool
  default     = false
}

variable "recorded_resource_types" {
  description = "Resource types recorded when record_all_resource_types is false. Defaults to the ones this repo actually creates."
  type        = list(string)
  default = [
    "AWS::S3::Bucket",
    "AWS::IAM::Role",
    "AWS::KMS::Key",
    "AWS::SageMaker::Domain",
  ]
}

variable "enable_macie" {
  description = "Enable Macie and run a ONE_TIME classification job over the project bucket. ~$1.00/GB for the first 50 GB — cents for a study bucket, real money against a data lake."
  type        = bool
  default     = false
}

variable "enable_audit_manager" {
  description = "Register Audit Manager and create a minimal custom framework. Assessments bill ~$1.25 per assessed resource."
  type        = bool
  default     = false
}
