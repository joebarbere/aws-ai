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

data "aws_region" "current" {}
