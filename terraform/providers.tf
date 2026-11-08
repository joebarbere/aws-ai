provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "aws-ai"
      ManagedBy = "terraform"
      Purpose   = "aif-c01-study"
    }
  }
}

data "aws_caller_identity" "current" {}

# The caller's ARN is a session ARN (arn:aws:sts::…:assumed-role/…) under SSO, which IAM
# accepts but OpenSearch Serverless data-access policies silently never match. issuer_arn is
# the underlying role ARN, path included.
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

data "aws_region" "current" {}
