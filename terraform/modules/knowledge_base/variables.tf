variable "name_prefix" {
  description = "Prefix for resource names. OpenSearch Serverless collection names are 3-32 lowercase chars."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "region" {
  description = "AWS region. The embedding model must be enabled in this region or ingestion fails."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the bucket holding source documents."
  type        = string
}

variable "kms_key_arn" {
  description = "Project key, so the knowledge base role can decrypt source documents."
  type        = string
}

variable "s3_prefix" {
  description = "Key prefix ingested into the knowledge base."
  type        = string
  default     = "kb/"
}

variable "embedding_model_id" {
  description = "Embedding model. Titan Text Embeddings V2 produces 1024 dimensions, which must match the vector index created by create_vector_index.py."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "vector_index_name" {
  description = "Name of the vector index inside the collection."
  type        = string
  default     = "bedrock-knowledge-base-default-index"
}

variable "vector_index_ready" {
  description = "Set true only after running create_vector_index.py. Terraform cannot create an OpenSearch Serverless index, so the knowledge base is held back until the index exists."
  type        = bool
  default     = false
}

variable "additional_data_access_principals" {
  description = "Extra IAM principal ARNs granted data access on the collection — normally your own user or role, so you can create the index and inspect documents."
  type        = list(string)
  default     = []
}
