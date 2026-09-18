variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
}

variable "account_id" {
  description = "AWS account id, for the service trust condition."
  type        = string
}

variable "bucket_arn" {
  description = "Project bucket ARN holding training data and model output."
  type        = string
}

variable "kms_key_arn" {
  description = "Project key for decrypting training data."
  type        = string
}

variable "language_code" {
  description = "Language of the training corpus."
  type        = string
  default     = "en"
}

variable "enable_rekognition_project" {
  description = "Create a Custom Labels project. Free to exist; training a model version costs ~$1/hr and is a separate, manual step."
  type        = bool
  default     = false
}

variable "classifier_training_s3_uri" {
  description = "S3 URI of the labeled CSV for a custom document classifier. Null means no classifier is created — Terraform cannot invent a labeled dataset. Training costs ~$3/hr."
  type        = string
  default     = null
}

variable "recognizer_documents_s3_uri" {
  description = "S3 URI of the raw documents for a custom entity recognizer. Both this and recognizer_entity_list_s3_uri must be set."
  type        = string
  default     = null
}

variable "recognizer_entity_list_s3_uri" {
  description = "S3 URI of the entity list CSV pairing text with entity types."
  type        = string
  default     = null
}

variable "recognizer_entity_types" {
  description = "Entity type names the recognizer learns, e.g. [\"EXAM_TOPIC\", \"AWS_SERVICE\"]."
  type        = list(string)
  default     = ["AWS_SERVICE"]
}
