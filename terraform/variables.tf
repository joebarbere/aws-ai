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

variable "enable_knowledge_base" {
  description = "Bedrock knowledge base over an OpenSearch Serverless vector collection. The collection bills ~$0.24/OCU-hr with a 2-OCU floor — about $350/month from creation, query traffic or not."
  type        = bool
  default     = false
}

variable "kb_vector_index_ready" {
  description = "Set true only after python/scripts/create_vector_index.py has created the index. Terraform cannot create an OpenSearch Serverless index, so the knowledge base is a second apply."
  type        = bool
  default     = false
}

variable "enable_conversational" {
  description = "Lex V2 bot plus the Polly/Transcribe IAM. Per-request pricing with nothing hourly, so it is safe to leave on."
  type        = bool
  default     = true
}

variable "enable_sagemaker_endpoint" {
  description = "Real-time inference endpoint inside the SageMaker module. Bills hourly with no traffic required (~$0.13/hr on ml.m5.large). Also requires sagemaker_model_image and sagemaker_model_data_url."
  type        = bool
  default     = false
}

variable "sagemaker_model_image" {
  description = "ECR URI of the inference container. Null means no endpoint is created, whatever enable_sagemaker_endpoint says."
  type        = string
  default     = null
}

variable "sagemaker_model_data_url" {
  description = "S3 URI of the model artifacts (model.tar.gz) to serve."
  type        = string
  default     = null
}

# --------------------------------------------------------------- MLOps

variable "enable_feature_store" {
  description = "Worked-example SageMaker feature group. Offline (S3) store costs storage only. Requires enable_sagemaker_domain."
  type        = bool
  default     = false
}

variable "enable_online_feature_store" {
  description = "Also enable the online store: per GB-month plus reads and writes."
  type        = bool
  default     = false
}

variable "enable_model_monitor" {
  description = "Data-quality monitoring schedule. Needs an endpoint AND model_monitor_image_uri; each run is a processing job billed by the minute."
  type        = bool
  default     = false
}

variable "model_monitor_image_uri" {
  description = "Region-specific model-monitor analyzer image URI. No single public value exists — look it up for your region."
  type        = string
  default     = null
}

# ----------------------------------------------------- data foundations

variable "enable_data_foundations" {
  description = "Glue catalog and crawler (near-free at rest), plus the optional Aurora cluster."
  type        = bool
  default     = false
}

variable "enable_glue_job" {
  description = "Example PySpark ETL job. Free to define; ~$0.44/DPU-hr while running."
  type        = bool
  default     = false
}

variable "enable_aurora" {
  description = "Aurora PostgreSQL Serverless v2 for pgvector. ~$43/month at the 0.5 ACU floor — it does not scale to zero."
  type        = bool
  default     = false
}

variable "aurora_publicly_accessible" {
  description = "Give the Aurora instance a public IP, so enable_pgvector.py can reach it from a laptop."
  type        = bool
  default     = false
}

variable "allowed_cidr_blocks" {
  description = "CIDRs allowed to reach PostgreSQL on 5432. Empty means no ingress rule; 0.0.0.0/0 is refused."
  type        = list(string)
  default     = []
}

# --------------------------------------------------------- governance

variable "enable_governance" {
  description = "AWS Config recorder and rules, plus optional Macie and Audit Manager. Usage-billed, not hourly."
  type        = bool
  default     = false
}

variable "config_record_all_resource_types" {
  description = "Record every supported resource type rather than a narrow list. Realistic for production; ~$0.003 per configuration item adds up fast."
  type        = bool
  default     = false
}

variable "enable_macie" {
  description = "Enable Macie and run a one-time classification job over the project bucket (~$1.00/GB, first 50 GB)."
  type        = bool
  default     = false
}

variable "enable_audit_manager" {
  description = "Register Audit Manager and create a minimal custom framework."
  type        = bool
  default     = false
}

# --------------------------------------------------------- vision / NLP

variable "enable_vision_nlp" {
  description = "Rekognition collection (free at rest) and the Comprehend custom-model training roles."
  type        = bool
  default     = false
}

variable "enable_rekognition_project" {
  description = "Create a Custom Labels project."
  type        = bool
  default     = false
}

variable "classifier_training_s3_uri" {
  description = "S3 URI of labeled CSV training data for a Comprehend custom classifier. Null means no classifier."
  type        = string
  default     = null
}

variable "recognizer_documents_s3_uri" {
  description = "S3 URI of documents for a custom entity recognizer."
  type        = string
  default     = null
}

variable "recognizer_entity_list_s3_uri" {
  description = "S3 URI of the entity list for a custom entity recognizer."
  type        = string
  default     = null
}
