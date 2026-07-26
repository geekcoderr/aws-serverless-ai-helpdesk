variable "aws_region" {
  description = "AWS region for the resources"
  type        = string
  default     = "us-east-1"
}

variable "s3_bucket_name" {
  description = "Name of the S3 bucket receiving inbound emails"
  type        = string
  default     = "support-inbound-email-ephemeral"
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table for deduplication and watcher tracking"
  type        = string
  default     = "TicketDeduplication"
}

variable "lambda_image_uri" {
  description = "ECR Image URI for the Lambda functions. Managed by CI/CD."
  type        = string
  default     = "123456789012.dkr.ecr.us-east-1.amazonaws.com/aws-serverless-ai-helpdesk:latest" # Placeholder
}
