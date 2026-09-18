output "rekognition_collection_id" {
  description = "Face collection id, for IndexFaces and SearchFacesByImage."
  value       = aws_rekognition_collection.faces.collection_id
}

output "rekognition_collection_arn" {
  description = "Face collection ARN."
  value       = aws_rekognition_collection.faces.arn
}

output "rekognition_project_arn" {
  description = "Custom Labels project ARN, or null when disabled."
  value       = var.enable_rekognition_project ? aws_rekognition_project.custom_labels[0].arn : null
}

output "comprehend_role_arn" {
  description = "Role Comprehend assumes to read training data."
  value       = aws_iam_role.comprehend.arn
}

output "classifier_arn" {
  description = "Custom classifier ARN, or null when no training data was supplied."
  value       = var.classifier_training_s3_uri != null ? aws_comprehend_document_classifier.this[0].arn : null
}

output "entity_recognizer_arn" {
  description = "Custom entity recognizer ARN, or null when no training data was supplied."
  value = (
    var.recognizer_documents_s3_uri != null && var.recognizer_entity_list_s3_uri != null
    ? aws_comprehend_entity_recognizer.this[0].arn
    : null
  )
}
