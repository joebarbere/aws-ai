output "region" {
  description = "Region these resources live in."
  value       = data.aws_region.current.name
}

output "data_bucket" {
  description = "S3 bucket the AI services read from and write to."
  value       = module.foundation.bucket_id
}

output "kms_key_arn" {
  description = "Customer-managed key encrypting the bucket and logs."
  value       = module.foundation.kms_key_arn
}

output "ai_service_role_arn" {
  description = "Role assumable by the serverless AI services (Comprehend, Rekognition, Transcribe, Textract, Translate)."
  value       = module.foundation.ai_service_role_arn
}

output "bedrock_guardrail_id" {
  description = "Bedrock guardrail id, or null when enable_bedrock is false."
  value       = var.enable_bedrock ? module.bedrock[0].guardrail_id : null
}

output "bedrock_guardrail_version" {
  description = "Published guardrail version, or null when enable_bedrock is false."
  value       = var.enable_bedrock ? module.bedrock[0].guardrail_version : null
}

output "sagemaker_domain_id" {
  description = "SageMaker Studio domain id, or null when the domain is disabled."
  value       = var.enable_sagemaker_domain ? module.sagemaker[0].domain_id : null
}

output "sagemaker_studio_url" {
  description = "Studio landing URL, or null when the domain is disabled."
  value       = var.enable_sagemaker_domain ? module.sagemaker[0].domain_url : null
}

output "sagemaker_endpoint_name" {
  description = "Inference endpoint name, or null when no endpoint was created."
  value       = var.enable_sagemaker_domain ? module.sagemaker[0].endpoint_name : null
}

output "kendra_index_id" {
  description = "Kendra index id, or null when enable_kendra is false."
  value       = var.enable_kendra ? module.kendra[0].index_id : null
}

output "kendra_data_source_id" {
  description = "Kendra S3 data source id, or null when enable_kendra is false."
  value       = var.enable_kendra ? module.kendra[0].data_source_id : null
}

output "kb_collection_endpoint" {
  description = "OpenSearch Serverless endpoint — pass to create_vector_index.py."
  value       = var.enable_knowledge_base ? module.knowledge_base[0].collection_endpoint : null
}

output "kb_knowledge_base_id" {
  description = "Knowledge base id, or null until kb_vector_index_ready is true."
  value       = var.enable_knowledge_base ? module.knowledge_base[0].knowledge_base_id : null
}

output "kb_data_source_id" {
  description = "Knowledge base S3 data source id, or null until kb_vector_index_ready is true."
  value       = var.enable_knowledge_base ? module.knowledge_base[0].data_source_id : null
}

output "lex_bot_id" {
  description = "Lex V2 bot id, or null when enable_conversational is false."
  value       = var.enable_conversational ? module.conversational[0].bot_id : null
}

output "lex_intent_name" {
  description = "Name of the worked example intent."
  value       = var.enable_conversational ? module.conversational[0].intent_name : null
}

output "feature_group_name" {
  description = "SageMaker feature group, or null when disabled."
  value       = var.enable_sagemaker_domain ? module.sagemaker[0].feature_group_name : null
}

output "monitoring_schedule_name" {
  description = "Model Monitor schedule, or null when disabled."
  value       = var.enable_sagemaker_domain ? module.sagemaker[0].monitoring_schedule_name : null
}

output "glue_database_name" {
  description = "Glue Data Catalog database, or null when data foundations are disabled."
  value       = var.enable_data_foundations ? module.data_foundations[0].glue_database_name : null
}

output "glue_crawler_name" {
  description = "Glue crawler, or null when data foundations are disabled."
  value       = var.enable_data_foundations ? module.data_foundations[0].glue_crawler_name : null
}

output "aurora_endpoint" {
  description = "Aurora writer endpoint, or null when Aurora is disabled."
  value       = var.enable_data_foundations ? module.data_foundations[0].aurora_endpoint : null
}

output "aurora_secret_arn" {
  description = "Secrets Manager ARN for the Aurora master password — pass to enable_pgvector.py."
  value       = var.enable_data_foundations ? module.data_foundations[0].aurora_secret_arn : null
}

output "config_rule_names" {
  description = "AWS Config rule names, or null when governance is disabled."
  value       = var.enable_governance ? module.governance[0].config_rule_names : null
}

output "macie_classification_job_id" {
  description = "Macie one-time job id, or null when disabled."
  value       = var.enable_governance ? module.governance[0].macie_classification_job_id : null
}

output "audit_manager_framework_id" {
  description = "Audit Manager framework id, or null when disabled."
  value       = var.enable_governance ? module.governance[0].audit_manager_framework_id : null
}

output "rekognition_collection_id" {
  description = "Rekognition face collection, or null when vision_nlp is disabled."
  value       = var.enable_vision_nlp ? module.vision_nlp[0].rekognition_collection_id : null
}

output "comprehend_classifier_arn" {
  description = "Custom classifier ARN, or null when no training data was supplied."
  value       = var.enable_vision_nlp ? module.vision_nlp[0].classifier_arn : null
}
