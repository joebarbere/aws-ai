# Vision and custom NLP: the two services on the exam that have both a zero-infrastructure
# API and a trainable, provisionable side.
#
# COST. A Rekognition collection is free to exist and bills per image indexed or searched
# (~$0.001/image). Comprehend custom training is ~$3/hr of training time — a small classifier
# trains in well under an hour — and a custom model costs nothing at rest as long as you do not
# create a real-time endpoint for it (this module deliberately does not).

# ------------------------------------------------------------- Rekognition

# A face collection: server-side storage of face vectors, searchable with SearchFacesByImage.
# This is the part of Rekognition that is infrastructure; DetectLabels and friends are pure API.
resource "aws_rekognition_collection" "faces" {
  collection_id = replace("${var.name_prefix}-faces", "-", "_")
}

# A Custom Labels project. Training a model version needs a labeled manifest, which is why the
# project exists here but no model version does.
resource "aws_rekognition_project" "custom_labels" {
  count = var.enable_rekognition_project ? 1 : 0

  name        = replace("${var.name_prefix}-labels", "-", "_")
  auto_update = "DISABLED"
  feature     = "CUSTOM_LABELS"
}

# ---------------------------------------------------------------- Comprehend
#
# Both custom models need TRAINING DATA that already exists in S3, in Comprehend's manifest
# format. Terraform creates the training job; it cannot invent a labeled dataset. So both are
# gated on you supplying a URI, and the gate is the honest form — a classifier pointed at a
# missing prefix fails minutes into training, not at plan time.

data "aws_iam_policy_document" "comprehend_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["comprehend.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "comprehend" {
  name               = "${var.name_prefix}-comprehend-training"
  assume_role_policy = data.aws_iam_policy_document.comprehend_trust.json
}

data "aws_iam_policy_document" "comprehend" {
  statement {
    sid       = "ReadTrainingData"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.bucket_arn, "${var.bucket_arn}/*"]
  }

  statement {
    sid       = "WriteModelOutput"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.bucket_arn}/*"]
  }

  statement {
    sid       = "UseKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "comprehend" {
  name   = "${var.name_prefix}-comprehend-training"
  role   = aws_iam_role.comprehend.id
  policy = data.aws_iam_policy_document.comprehend.json
}

# Classifies whole documents into labels you define — the custom counterpart to the built-in
# sentiment API.
resource "aws_comprehend_document_classifier" "this" {
  count = var.classifier_training_s3_uri != null ? 1 : 0

  name                 = "${var.name_prefix}-classifier"
  data_access_role_arn = aws_iam_role.comprehend.arn
  language_code        = var.language_code

  input_data_config {
    s3_uri = var.classifier_training_s3_uri
  }

  depends_on = [aws_iam_role_policy.comprehend]
}

# Finds entity types the built-in model does not know about. Needs both the documents and
# either an annotation set or an entity list.
resource "aws_comprehend_entity_recognizer" "this" {
  count = var.recognizer_documents_s3_uri != null && var.recognizer_entity_list_s3_uri != null ? 1 : 0

  name                 = "${var.name_prefix}-recognizer"
  data_access_role_arn = aws_iam_role.comprehend.arn
  language_code        = var.language_code

  input_data_config {
    documents {
      s3_uri = var.recognizer_documents_s3_uri
    }

    entity_list {
      s3_uri = var.recognizer_entity_list_s3_uri
    }

    dynamic "entity_types" {
      for_each = var.recognizer_entity_types

      content {
        type = entity_types.value
      }
    }
  }

  depends_on = [aws_iam_role_policy.comprehend]
}
