import json
import os
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received Webhook Event:", json.dumps(event))
    
    dynamodb_table_name = os.environ.get('DYNAMODB_TABLE')
    table = dynamodb.Table(dynamodb_table_name) if dynamodb_table_name else None
    sender_email = os.environ.get('SENDER_EMAIL')
    sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
    
    if not table or not sender_email:
        print("Missing env vars")
        return {'statusCode': 500, 'body': 'Configuration Error'}
        
    webhook_secret = os.environ.get('WEBHOOK_SECRET')
    if webhook_secret:
        query_params = event.get('queryStringParameters', {}) or {}
        provided_token = query_params.get('token')
        if provided_token != webhook_secret:
            print("Authentication failed: Invalid or missing webhook token.")
            return {'statusCode': 403, 'body': 'Forbidden'}
        
    try:
        body = json.loads(event.get('body', '{}'))
        webhook_event = body.get('webhookEvent')
        
        if webhook_event != 'jira:issue_updated':
            return {'statusCode': 200, 'body': 'Ignored event type'}
            
        issue = body.get('issue', {})
        issue_key = issue.get('key')
        status_category = issue.get('fields', {}).get('status', {}).get('statusCategory', {}).get('key')
        
        issue_status_name = issue.get('fields', {}).get('status', {}).get('name', 'Updated')
        
        print(f"Ticket {issue_key} updated to {issue_status_name}. Processing notifications.")
        
        watchers = []
        try:
            ddb_query = table.query(KeyConditionExpression=Key('PK').eq(f"INCIDENT#{issue_key}"))
            for item in ddb_query.get('Items', []):
                if item['SK'].startswith('REPORTER#'):
                    email = item['SK'].split('REPORTER#')[1]
                    watchers.append(email)
                    
                    # Update DynamoDB state
                    table.update_item(
                        Key={'PK': item['PK'], 'SK': item['SK']},
                        UpdateExpression='SET #s = :val',
                        ExpressionAttributeNames={'#s': 'status'},
                        ExpressionAttributeValues={':val': issue_status_name}
                    )
                    
                    # Also try to update the EMAIL# mapping
                    try:
                        table.update_item(
                            Key={'PK': f"EMAIL#{email}", 'SK': f"TICKET#{issue_key}"},
                            UpdateExpression='SET #s = :val',
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':val': issue_status_name}
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error querying DynamoDB for incident watchers: {e}")
            
        print(f"Found {len(watchers)} users watching {issue_key}")
        
        # Send update emails ONLY if the incident is resolved
        if status_category == 'done':
            for user_email in watchers:
                try:
                    html_body = f"""
                    <div style="font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1f2937; line-height: 1.6; max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03); overflow: hidden;">
                        <div style="background-color: #f9fafb; padding: 24px 32px; border-bottom: 1px solid #f3f4f6;">
                            <h2 style="margin: 0; font-size: 20px; font-weight: 600; color: #111827; letter-spacing: -0.01em;">Incident Resolved</h2>
                        </div>
                        <div style="padding: 32px;">
                            <p style="margin-top: 0; font-size: 16px; color: #374151;">Hello,</p>
                            <p style="font-size: 16px; color: #4b5563;">The incident associated with your support request has been successfully resolved.</p>
                            
                            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin: 28px 0;">
                                <table style="width: 100%; border-collapse: collapse;">
                                    <tr>
                                        <td style="padding-bottom: 12px; width: 50%;">
                                            <span style="color: #64748b; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;">Reference ID</span><br>
                                            <span style="font-size: 15px; font-weight: 600; color: #0f172a;">{issue_key}</span>
                                        </td>
                                        <td style="padding-bottom: 12px; width: 50%;">
                                            <span style="color: #64748b; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;">Final Status</span><br>
                                            <span style="font-size: 14px; font-weight: 500; color: #065f46; background-color: #d1fae5; padding: 4px 10px; border-radius: 16px;">{issue_status_name}</span>
                                        </td>
                                    </tr>
                                </table>
                            </div>
                            
                            <p style="font-size: 15px; color: #4b5563;">If you continue to experience issues, please submit a new support request by replying to this email.</p>
                            
                            <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #f3f4f6;">
                                <p style="margin: 0; font-size: 14px; color: #6b7280;">Best regards,<br><strong style="color: #111827; font-weight: 600;">{sender_name}</strong></p>
                            </div>
                        </div>
                    </div>
                    """
                    ses_client.send_email(
                        Source=f"{sender_name} <{sender_email}>",
                        Destination={'ToAddresses': [user_email]},
                        Message={
                            'Subject': {'Data': f"Resolved: {issue_key}"},
                            'Body': {'Html': {'Data': html_body}}
                        }
                    )
                    print(f"Update email sent to {user_email}")
                except Exception as e:
                    print(f"Failed to send update email to {user_email}: {e}")
                
        return {'statusCode': 200, 'body': 'Successfully processed webhook'}
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}
