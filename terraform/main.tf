data "aws_caller_identity" "current" {}

# 1. DynamoDB Table
resource "aws_dynamodb_table" "ticket_deduplication" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# 2. S3 Bucket
resource "aws_s3_bucket" "email_ingest" {
  bucket = var.s3_bucket_name
}

resource "aws_s3_bucket_notification" "bucket_notification" {
  bucket      = aws_s3_bucket.email_ingest.id
  eventbridge = true
}

resource "aws_s3_bucket_policy" "email_ingest_policy" {
  bucket = aws_s3_bucket.email_ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSESPuts"
        Effect    = "Allow"
        Principal = { Service = "ses.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.email_ingest.arn}/*"
        Condition = {
          StringEquals = {
            "aws:Referer" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

# 3. SQS FIFO Queues
resource "aws_sqs_queue" "dlq_fifo" {
  name                        = "EmailIngestDLQ.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 360 # 6 times the lambda timeout
}

resource "aws_sqs_queue" "ingest_fifo" {
  name                        = "EmailIngestQueue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 360

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq_fifo.arn
    maxReceiveCount     = 3
  })
}

# SQS Queue Policy for EventBridge
resource "aws_sqs_queue_policy" "ingest_fifo_policy" {
  queue_url = aws_sqs_queue.ingest_fifo.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeToSendMessage"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.ingest_fifo.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.s3_to_fifo.arn
          }
        }
      }
    ]
  })
}

# 4. EventBridge Rule
resource "aws_cloudwatch_event_rule" "s3_to_fifo" {
  name        = "RouteS3EmailsToFIFO"
  description = "Routes ObjectCreated events from S3 to SQS FIFO"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = {
        name = [aws_s3_bucket.email_ingest.id]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "sqs_target" {
  rule      = aws_cloudwatch_event_rule.s3_to_fifo.name
  target_id = "SendToSQSFifo"
  arn       = aws_sqs_queue.ingest_fifo.arn

  sqs_target {
    message_group_id = "inbound-emails"
  }
}
