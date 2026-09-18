variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "locale_id" {
  description = "Bot locale. Voice availability and built-in slot types vary by locale."
  type        = string
  default     = "en_US"
}

variable "voice_id" {
  description = "Polly voice Lex speaks with. Neural voices (Joanna, Matthew, Amy) sound markedly better than standard ones."
  type        = string
  default     = "Joanna"
}

variable "confidence_threshold" {
  description = "Below this NLU confidence, Lex routes to the fallback intent instead of guessing. 0.40 is the console default."
  type        = number
  default     = 0.40

  validation {
    condition     = var.confidence_threshold >= 0 && var.confidence_threshold <= 1
    error_message = "confidence_threshold must be between 0 and 1."
  }
}

variable "idle_session_ttl_seconds" {
  description = "How long Lex keeps slot values between turns before the session expires."
  type        = number
  default     = 300
}
