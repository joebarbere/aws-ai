output "bucket_id" {
  description = "Name of the data bucket."
  value       = aws_s3_bucket.data.id
}

output "bucket_arn" {
  description = "ARN of the data bucket."
  value       = aws_s3_bucket.data.arn
}

output "kms_key_arn" {
  description = "ARN of the customer-managed key."
  value       = aws_kms_key.this.arn
}

output "ai_service_role_arn" {
  description = "ARN of the role the serverless AI services assume."
  value       = aws_iam_role.ai_service.arn
}

output "log_group_name" {
  description = "Name of the shared AI log group."
  value       = aws_cloudwatch_log_group.ai.name
}

output "log_group_arn" {
  description = "ARN of the shared AI log group."
  value       = aws_cloudwatch_log_group.ai.arn
}
