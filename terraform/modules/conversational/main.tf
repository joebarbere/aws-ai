# Conversational AI: Lex V2 bot, plus the IAM that Transcribe and Polly need.
#
# COST. Nothing here bills hourly. Lex charges per request (~$0.004 per speech request, ~$0.00075
# per text request), Transcribe ~$0.024/minute, Polly per character with a large free tier for the
# first year. This module is safe to leave applied — it is the cheap end of the repo.
#
# Transcribe and Polly provision nothing at all. Their entire infrastructure is the bucket and role
# in modules/foundation; the role below exists only because Lex needs one.

data "aws_iam_policy_document" "lex_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lexv2.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "lex" {
  name               = "${var.name_prefix}-lex"
  assume_role_policy = data.aws_iam_policy_document.lex_trust.json
}

# Lex calls Polly itself to speak responses, and Comprehend to score sentiment when that is
# switched on. Those are the only permissions a basic bot needs.
data "aws_iam_policy_document" "lex" {
  statement {
    sid       = "SpeakResponses"
    effect    = "Allow"
    actions   = ["polly:SynthesizeSpeech"]
    resources = ["*"]
  }

  statement {
    sid       = "DetectSentiment"
    effect    = "Allow"
    actions   = ["comprehend:DetectSentiment"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lex" {
  name   = "${var.name_prefix}-lex"
  role   = aws_iam_role.lex.id
  policy = data.aws_iam_policy_document.lex.json
}

resource "aws_lexv2models_bot" "this" {
  name     = "${var.name_prefix}-bot"
  role_arn = aws_iam_role.lex.arn

  # COPPA. Not optional, and the answer is recorded with the bot.
  data_privacy {
    child_directed = false
  }

  # How long Lex remembers slot values between turns before the session expires.
  idle_session_ttl_in_seconds = var.idle_session_ttl_seconds
}

resource "aws_lexv2models_bot_locale" "this" {
  bot_id      = aws_lexv2models_bot.this.id
  bot_version = "DRAFT"
  locale_id   = var.locale_id

  # Below this confidence Lex falls back to AMAZON.FallbackIntent rather than guessing.
  # 0.40 is the console default; raising it makes the bot admit confusion more often.
  n_lu_intent_confidence_threshold = var.confidence_threshold

  voice_settings {
    voice_id = var.voice_id
  }
}

# One worked intent. The sample utterances are the training data — Lex generalizes from them,
# so a handful of genuinely different phrasings beats a long list of near-duplicates.
resource "aws_lexv2models_intent" "study" {
  bot_id      = aws_lexv2models_bot.this.id
  bot_version = "DRAFT"
  locale_id   = aws_lexv2models_bot_locale.this.locale_id
  name        = "CheckExamTopic"
  description = "Ask what a given AIF-C01 topic covers."

  sample_utterance {
    utterance = "What is on the exam about {Topic}"
  }

  sample_utterance {
    utterance = "Tell me about {Topic}"
  }

  sample_utterance {
    utterance = "I want to study {Topic}"
  }
}
