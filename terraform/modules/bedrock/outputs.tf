output "guardrail_id" {
  description = "Guardrail id, passed to InvokeModel as guardrailIdentifier."
  value       = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_arn" {
  description = "Guardrail ARN."
  value       = aws_bedrock_guardrail.this.guardrail_arn
}

output "guardrail_version" {
  description = "Published guardrail version, passed to InvokeModel as guardrailVersion."
  value       = aws_bedrock_guardrail_version.this.version
}

output "logging_role_arn" {
  description = "Role Bedrock assumes to write invocation logs."
  value       = aws_iam_role.logging.arn
}
