output "bot_id" {
  description = "Lex V2 bot id."
  value       = aws_lexv2models_bot.this.id
}

output "bot_name" {
  description = "Lex V2 bot name."
  value       = aws_lexv2models_bot.this.name
}

output "locale_id" {
  description = "Locale the bot was built for."
  value       = aws_lexv2models_bot_locale.this.locale_id
}

output "intent_name" {
  description = "Name of the worked example intent."
  value       = aws_lexv2models_intent.study.name
}

output "lex_role_arn" {
  description = "Role Lex assumes to call Polly and Comprehend."
  value       = aws_iam_role.lex.arn
}
