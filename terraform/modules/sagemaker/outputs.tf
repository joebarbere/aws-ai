output "domain_id" {
  description = "Studio domain id."
  value       = aws_sagemaker_domain.this.id
}

output "domain_url" {
  description = "Studio landing URL."
  value       = aws_sagemaker_domain.this.url
}

output "execution_role_arn" {
  description = "Role Studio notebooks and jobs run as."
  value       = aws_iam_role.execution.arn
}

output "user_profile_name" {
  description = "Studio user profile name."
  value       = aws_sagemaker_user_profile.this.user_profile_name
}

output "endpoint_name" {
  description = "Endpoint name, or null when no endpoint was created."
  value       = local.create_endpoint ? aws_sagemaker_endpoint.this[0].name : null
}
