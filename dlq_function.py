import json
import os
import boto3
import urllib.parse

# Initialize AWS SDK Clients
s3_client = boto3.client('s3')
# We need SES to send the alert email
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received DLQ event:", json.dumps(event))
    
    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
    sender_email = os.environ.get('SENDER_EMAIL', admin_email) # Must be a verified SES identity
    
    for record in event.get('Records', []):
        try:
            # Parse the original S3 event embedded inside the DLQ SQS message body
            s3_event = json.loads(record['body'])
            if 'Records' not in s3_event or len(s3_event['Records']) == 0:
                print("No S3 records found in DLQ message. Skipping.")
                continue
                
            s3_record = s3_event['Records'][0]
            bucket_name = s3_record['s3']['bucket']['name']
            object_key = urllib.parse.unquote_plus(s3_record['s3']['object']['key'])
            
            print(f"DLQ Alert: Failed to process email from Bucket: {bucket_name}, Key: {object_key}")
            
            # 1. Send Alert Email to Admin
            try:
                ses_client.send_email(
                    Source=sender_email,
                    Destination={'ToAddresses': [admin_email]},
                    Message={
                        'Subject': {'Data': 'ALERT: Email-to-Jira Automation Failure'},
                        'Body': {
                            'Text': {'Data': f"An inbound email failed to process 3 times and was sent to the DLQ.\n\nS3 Bucket: {bucket_name}\nS3 Key: {object_key}\n\nPlease check the CloudWatch logs for the main Lambda function to debug the error. The file has been moved to the failed/ directory in S3."}
                        }
                    }
                )
                print("Alert email sent to admin.")
            except Exception as ses_e:
                print(f"Warning: Failed to send alert email (check SES verification): {ses_e}")
            
            # 2. Move the file in S3 to a 'failed/' folder so it isn't lost but is out of the way
            new_key = object_key.replace('incoming/', 'failed/', 1)
            if 'failed/' not in new_key:
                new_key = f"failed/{object_key}"
                
            print(f"Moving S3 object from {object_key} to {new_key}")
            
            # Copy to new location
            s3_client.copy_object(
                Bucket=bucket_name,
                CopySource={'Bucket': bucket_name, 'Key': object_key},
                Key=new_key
            )
            
            # Delete original from incoming/
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            print("Successfully moved file to failed directory.")
            
        except Exception as e:
            print(f"Error processing DLQ record: {e}")
            # If the DLQ processor fails, it will retry based on SQS settings
            raise e
            
    return {
        'statusCode': 200,
        'body': json.dumps('DLQ Processing Complete')
    }
