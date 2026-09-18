output "index_id" {
  description = "Kendra index id, passed to the Query API."
  value       = aws_kendra_index.this.id
}

output "index_arn" {
  description = "Kendra index ARN."
  value       = aws_kendra_index.this.arn
}

output "index_role_arn" {
  description = "Role the index uses for metrics and logs."
  value       = aws_iam_role.index.arn
}

output "data_source_id" {
  description = "S3 data source id, or null when the data source is disabled."
  value       = var.enable_s3_data_source ? aws_kendra_data_source.s3[0].id : null
}
