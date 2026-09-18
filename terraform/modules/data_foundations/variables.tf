variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "bucket_name" {
  description = "Project bucket name, for crawler and script paths."
  type        = string
}

variable "bucket_arn" {
  description = "Project bucket ARN, for the Glue role."
  type        = string
}

variable "kms_key_arn" {
  description = "Project key: decrypts crawler input and encrypts the Aurora cluster."
  type        = string
}

variable "datasets_prefix" {
  description = "Prefix the Glue crawler scans for tabular data."
  type        = string
  default     = "datasets/"
}

# ------------------------------------------------------------------- Glue

variable "enable_glue_job" {
  description = "Create an example PySpark ETL job. The job resource is free; running it bills ~$0.44/DPU-hr with a 1-minute minimum."
  type        = bool
  default     = false
}

variable "glue_script_prefix" {
  description = "Prefix holding the job's PySpark script. The script must be uploaded separately — Terraform creates a job pointing at it either way."
  type        = string
  default     = "scripts/"
}

# ----------------------------------------------------------------- Aurora

variable "enable_aurora" {
  description = "Aurora PostgreSQL Serverless v2 with pgvector. ~$0.12/ACU-hr with a 0.5 ACU floor — about $43/month if left running, roughly an eighth of the OpenSearch Serverless collection."
  type        = bool
  default     = false
}

variable "aurora_engine_version" {
  description = "PostgreSQL version. pgvector ships in 15.5+ and 16.x; older versions cannot install it at all."
  type        = string
  default     = "16.4"
}

variable "aurora_min_acu" {
  description = "Serverless v2 floor in ACUs. 0.5 is the minimum, and it does not scale to zero — the cluster bills continuously while it exists."
  type        = number
  default     = 0.5
}

variable "aurora_max_acu" {
  description = "Serverless v2 ceiling in ACUs."
  type        = number
  default     = 2
}

variable "aurora_publicly_accessible" {
  description = "Give the instance a public IP. Needed to run enable_pgvector.py from a laptop; combine with allowed_cidr_blocks or nothing can reach it anyway."
  type        = bool
  default     = false
}

variable "allowed_cidr_blocks" {
  description = "CIDR ranges allowed to reach Postgres on 5432. Empty means no ingress rule at all. Never set this to 0.0.0.0/0."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.allowed_cidr_blocks, "0.0.0.0/0")
    error_message = "Refusing to open PostgreSQL to the entire internet. Use your own address, e.g. 203.0.113.4/32."
  }
}

variable "vpc_id" {
  description = "VPC for the cluster. Null uses the default VPC."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnets for the DB subnet group. Empty uses all subnets in the chosen VPC."
  type        = list(string)
  default     = []
}
