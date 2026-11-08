output "cluster_arn" {
  description = "Aurora cluster ARN — the Data API resource, and rdsConfiguration.resourceArn."
  value       = aws_rds_cluster.this.arn
}

output "master_secret_arn" {
  description = "Aurora-managed master credentials, used by rds_kb.py to set up the schema."
  value       = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "bedrock_user_secret_arn" {
  description = "bedrock_user's credentials — rdsConfiguration.credentialsSecretArn."
  value       = aws_secretsmanager_secret.bedrock_user.arn
}

output "database_name" {
  description = "Database that holds the knowledge base table."
  value       = aws_rds_cluster.this.database_name
}

output "role_arn" {
  description = "Role the knowledge base assumes."
  value       = aws_iam_role.kb.arn
}

output "postgresql_log_group" {
  description = "CloudWatch log group with every statement the cluster ran."
  value       = aws_cloudwatch_log_group.postgresql.name
}
