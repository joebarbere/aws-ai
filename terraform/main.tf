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

# Hourly-billed from here down. Both default to off; see variables.tf for the rates.

module "sagemaker" {
  source = "./modules/sagemaker"
  count  = var.enable_sagemaker_domain ? 1 : 0

  name_prefix = var.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
  bucket_arn  = module.foundation.bucket_arn
  kms_key_arn = module.foundation.kms_key_arn

  enable_endpoint = var.enable_sagemaker_endpoint
  model_image     = var.sagemaker_model_image
  model_data_url  = var.sagemaker_model_data_url
}

module "kendra" {
  source = "./modules/kendra"
  count  = var.enable_kendra ? 1 : 0

  name_prefix = var.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  kms_key_arn = module.foundation.kms_key_arn
  bucket_name = module.foundation.bucket_id
  bucket_arn  = module.foundation.bucket_arn
}

module "knowledge_base" {
  source = "./modules/knowledge_base"
  count  = var.enable_knowledge_base ? 1 : 0

  name_prefix = var.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  bucket_arn  = module.foundation.bucket_arn
  kms_key_arn = module.foundation.kms_key_arn

  # Held back until create_vector_index.py has run; see the module README.
  vector_index_ready = var.kb_vector_index_ready

  # You need collection data access too, or you cannot create the index in the first place.
  additional_data_access_principals = [data.aws_caller_identity.current.arn]
}

# Per-request pricing, nothing hourly — on by default.
module "conversational" {
  source = "./modules/conversational"
  count  = var.enable_conversational ? 1 : 0

  name_prefix = var.name_prefix
  account_id  = data.aws_caller_identity.current.account_id
}
