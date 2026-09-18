output "glue_database_name" {
  description = "Glue Data Catalog database."
  value       = aws_glue_catalog_database.this.name
}

output "glue_crawler_name" {
  description = "Crawler that populates the catalog. Start it with: aws glue start-crawler --name <this>"
  value       = aws_glue_crawler.this.name
}

output "glue_role_arn" {
  description = "Role the crawler and jobs run as."
  value       = aws_iam_role.glue.arn
}

output "glue_job_name" {
  description = "Example ETL job, or null when disabled."
  value       = var.enable_glue_job ? aws_glue_job.etl[0].name : null
}

output "aurora_endpoint" {
  description = "Writer endpoint, or null when Aurora is disabled."
  value       = var.enable_aurora ? aws_rds_cluster.this[0].endpoint : null
}

output "aurora_database_name" {
  description = "Initial database name."
  value       = var.enable_aurora ? aws_rds_cluster.this[0].database_name : null
}

output "aurora_secret_arn" {
  description = "Secrets Manager secret holding the managed master password — the password is never in Terraform state."
  value       = var.enable_aurora ? aws_rds_cluster.this[0].master_user_secret[0].secret_arn : null
}
