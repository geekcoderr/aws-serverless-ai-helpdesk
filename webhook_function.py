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
                    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #333333; line-height: 1.5; max-width: 600px; margin: 0 auto; border: 1px solid #e1e4e8; border-radius: 6px; overflow: hidden; background-color: #ffffff;">
                        <div style="padding: 24px; border-bottom: 1px solid #e1e4e8;">
                            <h2 style="margin: 0; font-size: 18px; font-weight: 600; color: #24292e;">Incident Resolved</h2>
                        </div>
                        <div style="padding: 24px;">
                            <p style="margin-top: 0;">Hello,</p>
                            <p>The incident associated with your support request has been successfully resolved.</p>
                            
                            <div style="background-color: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px; padding: 16px; margin: 20px 0;">
                                <div style="margin-bottom: 12px;">
                                    <span style="color: #586069; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;">Reference ID</span><br>
                                    <span style="font-size: 15px; font-weight: 500;">{issue_key}</span>
                                </div>
                                <div>
                                    <span style="color: #586069; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;">Final Status</span><br>
                                    <span style="font-size: 15px; font-weight: 500; color: #22863a;">{issue_status_name}</span>
                                </div>
                            </div>
                            
                            <p>If you continue to experience issues, please submit a new support request by replying to this email.</p>
                            
                            <p style="margin-bottom: 0; color: #586069;">Best regards,<br><strong style="color: #24292e;">{sender_name}</strong></p>
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
