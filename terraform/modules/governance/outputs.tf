output "config_recorder_name" {
  description = "AWS Config configuration recorder."
  value       = aws_config_configuration_recorder.this.name
}

output "config_rule_names" {
  description = "Managed Config rules evaluating the project bucket."
  value       = [aws_config_config_rule.s3_public_read.name, aws_config_config_rule.s3_encrypted.name]
}

output "macie_classification_job_id" {
  description = "Macie one-time job id, or null when Macie is disabled."
  value       = var.enable_macie ? aws_macie2_classification_job.this[0].job_id : null
}

output "macie_custom_identifier_id" {
  description = "Custom data identifier id, or null when Macie is disabled."
  value       = var.enable_macie ? aws_macie2_custom_data_identifier.study_id[0].id : null
}

output "audit_manager_framework_id" {
  description = "Audit Manager framework id, or null when disabled."
  value       = var.enable_audit_manager ? aws_auditmanager_framework.this[0].id : null
}
