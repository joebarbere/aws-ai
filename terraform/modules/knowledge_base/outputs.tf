output "collection_arn" {
  description = "OpenSearch Serverless collection ARN."
  value       = aws_opensearchserverless_collection.this.arn
}

output "collection_endpoint" {
  description = "Collection endpoint — the host create_vector_index.py talks to."
  value       = aws_opensearchserverless_collection.this.collection_endpoint
}

output "vector_index_name" {
  description = "Index name the knowledge base expects to find."
  value       = var.vector_index_name
}

output "role_arn" {
  description = "Role the knowledge base assumes to embed, read S3, and query the collection."
  value       = aws_iam_role.kb.arn
}

output "knowledge_base_id" {
  description = "Knowledge base id, or null until vector_index_ready is true."
  value       = var.vector_index_ready ? aws_bedrockagent_knowledge_base.this[0].id : null
}

output "data_source_id" {
  description = "S3 data source id, or null until vector_index_ready is true."
  value       = var.vector_index_ready ? aws_bedrockagent_data_source.s3[0].data_source_id : null
}
