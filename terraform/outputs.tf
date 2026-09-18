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
