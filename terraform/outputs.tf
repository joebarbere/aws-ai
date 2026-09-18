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
