variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the bucket holding source documents."
  type        = string
}

variable "kms_key_arn" {
  description = "Project key: encrypts the cluster and decrypts source documents."
  type        = string
}

variable "embedding_model_id" {
  description = "Embedding model. Titan Text Embeddings V2 emits 1024 dimensions, matching vector(1024) in rds_kb.py."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "engine_version" {
  description = "Aurora PostgreSQL version. Bedrock needs pgvector 0.5.0+; iterative index scans need 0.8.0."
  type        = string
  default     = "17.11"
}

variable "max_acu" {
  description = "Serverless v2 ceiling in ACUs."
  type        = number
  default     = 2
}

variable "log_statements" {
  description = "log_statement = all, exported to CloudWatch: records the SQL Bedrock runs. Verbose; for capture sessions."
  type        = bool
  default     = true
}
