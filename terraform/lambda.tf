# Dummy lambda deployment for main function
resource "aws_lambda_function" "process_inbound_email" {
  function_name = "ProcessInboundEmail"
  role          = aws_iam_role.lambda_exec_role.arn
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  timeout       = 60
  memory_size   = 128

  # CI/CD handles code/image updates, terraform ignores them
  lifecycle {
    ignore_changes = [image_uri]
  }
}

# Dummy lambda deployment for DLQ function
resource "aws_lambda_function" "process_inbound_email_dlq" {
  function_name = "ProcessInboundEmailDLQ"
  role          = aws_iam_role.lambda_exec_role.arn
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  timeout       = 60
  memory_size   = 128

  # CI/CD handles code/image updates, terraform ignores them
  lifecycle {
    ignore_changes = [image_uri]
  }
}

# Event Source Mapping (SQS -> Main Lambda)
resource "aws_lambda_event_source_mapping" "main_sqs_trigger" {
  event_source_arn = aws_sqs_queue.ingest_fifo.arn
  function_name    = aws_lambda_function.process_inbound_email.arn
  batch_size       = 1
}

# Event Source Mapping (SQS -> DLQ Lambda)
resource "aws_lambda_event_source_mapping" "dlq_sqs_trigger" {
  event_source_arn = aws_sqs_queue.dlq_fifo.arn
  function_name    = aws_lambda_function.process_inbound_email_dlq.arn
  batch_size       = 1
}
