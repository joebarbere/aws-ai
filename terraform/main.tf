locals {
  # Bucket names are globally unique; the account id keeps a shared prefix from colliding.
  bucket_name = "${var.name_prefix}-data-${data.aws_caller_identity.current.account_id}"
}

module "foundation" {
  source = "./modules/foundation"

  name_prefix        = var.name_prefix
  bucket_name        = local.bucket_name
  log_retention_days = var.log_retention_days
  account_id         = data.aws_caller_identity.current.account_id
  region             = data.aws_region.current.name
}

module "bedrock" {
  source = "./modules/bedrock"
  count  = var.enable_bedrock ? 1 : 0

  name_prefix    = var.name_prefix
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.name
  kms_key_arn    = module.foundation.kms_key_arn
  log_group_name = module.foundation.log_group_name
  log_group_arn  = module.foundation.log_group_arn
}
