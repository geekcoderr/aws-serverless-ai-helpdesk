import json
import os
import boto3
import email
from email import policy
import urllib.parse
from datetime import datetime
import time

# Initialize AWS SDK Clients
s3_client = boto3.client('s3')
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received DLQ event:", json.dumps(event))
    
    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
    sender_email = os.environ.get('SENDER_EMAIL', admin_email) 
    dynamodb_table_name = os.environ.get('DYNAMODB_TABLE')
    table = dynamodb.Table(dynamodb_table_name) if dynamodb_table_name else None
    sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
    
    for record in event.get('Records', []):
        try:
            s3_event = json.loads(record['body'])
            
            # Handle EventBridge format
            if 'detail-type' in s3_event and s3_event.get('source') == 'aws.s3':
                bucket_name = s3_event['detail']['bucket']['name']
                object_key = urllib.parse.unquote_plus(s3_event['detail']['object']['key'])
            # Handle standard S3 Event Notification format
            elif 'Records' in s3_event and len(s3_event['Records']) > 0:
                s3_record = s3_event['Records'][0]
                bucket_name = s3_record['s3']['bucket']['name']
                object_key = urllib.parse.unquote_plus(s3_record['s3']['object']['key'])
            else:
                continue
            
            print(f"DLQ Alert: Failed to process email from Bucket: {bucket_name}, Key: {object_key}")
            
            # Try to read email to notify user and mark dedup
            user_email = None
            try:
                response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
                eml_content = response['Body'].read()
                parsed_email = email.message_from_bytes(eml_content, policy=policy.default)
                user_email = parsed_email.get('from')
                message_id = parsed_email.get('Message-ID')
                
                # Mark as failed in DDB to avoid duplicate retries from users later
                if table and message_id:
                    dedup_key = f"MSGID#{message_id}"
                    table.put_item(Item={
                        'PK': dedup_key,
                        'SK': 'DEDUP',
                        'processed_at': datetime.now().isoformat(),
                        'status': 'FAILED',
                        'ttl': int(time.time()) + (7 * 24 * 60 * 60)
                    })
            except Exception as e:
                print(f"Could not parse email for user notification: {e}")

            # 1. Send Alert Email to Admin
            try:
                ses_client.send_email(
                    Source=f"{sender_name} <{sender_email}>",
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
                print(f"Warning: Failed to send alert email: {ses_e}")
                
            # 2. Notify the user
            if user_email and sender_email:
                try:
                    ses_client.send_email(
                        Source=f"{sender_name} <{sender_email}>",
                        Destination={'ToAddresses': [user_email]},
                        Message={
                            'Subject': {'Data': 'Unable to process your support request'},
                            'Body': {
                                'Html': {'Data': f"""
                                <div style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden;">
                                    <div style="background-color: #DE350B; padding: 15px 20px; color: #fff;">
                                        <h2 style="margin: 0; font-size: 20px;">System Processing Error</h2>
                                    </div>
                                    <div style="padding: 20px;">
                                        <p>Hello,</p>
                                        <p>We received your email, but our automated ticketing system encountered an unexpected error while trying to process it.</p>
                                        <ul style="background-color: #f5f5f5; padding: 15px 15px 15px 35px; border-radius: 4px;">
                                            <li><strong>Status:</strong> Processing Failed</li>
                                            <li><strong>Action Taken:</strong> Administrators Notified</li>
                                        </ul>
                                        <p>Our administration team has been notified and will look into this issue. Please try submitting your request again later.</p>
                                        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                                        <p style="font-size: 12px; color: #777;">Best regards,<br><strong>{sender_name}</strong></p>
                                    </div>
                                </div>
                                """}
                            }
                        }
                    )
                    print(f"Apology email sent to {user_email}")
                except Exception as ses_e:
                    print(f"Warning: Failed to send user apology email: {ses_e}")
            
            # 3. Move the file in S3 to a 'failed/' folder
            new_key = object_key.replace('incoming/', 'failed/', 1)
            if 'failed/' not in new_key:
                new_key = f"failed/{object_key}"
                
            print(f"Moving S3 object from {object_key} to {new_key}")
            
            s3_client.copy_object(
                Bucket=bucket_name,
                CopySource={'Bucket': bucket_name, 'Key': object_key},
                Key=new_key
            )
            
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            print("Successfully moved file to failed directory.")
            
        except Exception as e:
            print(f"Error processing DLQ record: {e}")
            raise e
            
    return {
        'statusCode': 200,
        'body': json.dumps('DLQ Processing Complete')
    }
