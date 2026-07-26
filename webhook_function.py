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
        
        # Only process if status is Done (Closed)
        if status_category != 'done':
            print(f"Ticket {issue_key} updated but not closed. Ignoring.")
            return {'statusCode': 200, 'body': 'Ignored mid-state update'}
            
        print(f"Ticket {issue_key} CLOSED. Processing notifications.")
        
        watchers = []
        try:
            ddb_query = table.query(KeyConditionExpression=Key('PK').eq(f"INCIDENT#{issue_key}"))
            for item in ddb_query.get('Items', []):
                if item['SK'].startswith('REPORTER#'):
                    email = item['SK'].split('REPORTER#')[1]
                    watchers.append(email)
                    
                    # Update DynamoDB state to closed
                    table.update_item(
                        Key={'PK': item['PK'], 'SK': item['SK']},
                        UpdateExpression='SET #s = :val',
                        ExpressionAttributeNames={'#s': 'status'},
                        ExpressionAttributeValues={':val': 'Closed'}
                    )
                    
                    # Also try to update the EMAIL# mapping
                    try:
                        table.update_item(
                            Key={'PK': f"EMAIL#{email}", 'SK': f"TICKET#{issue_key}"},
                            UpdateExpression='SET #s = :val',
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':val': 'Closed'}
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error querying DynamoDB for incident watchers: {e}")
            
        print(f"Found {len(watchers)} users watching {issue_key}")
        
        # Send resolution emails
        for user_email in watchers:
            try:
                html_body = f"""
                <html><body>
                <p>The incident associated with <strong>{issue_key}</strong> has been successfully resolved.</p>
                <p>If you continue to experience issues, please submit a new support request.</p>
                <p>Best,<br>{sender_name}</p>
                </body></html>
                """
                ses_client.send_email(
                    Source=f"{sender_name} <{sender_email}>",
                    Destination={'ToAddresses': [user_email]},
                    Message={
                        'Subject': {'Data': f"Resolved: {issue_key}"},
                        'Body': {'Html': {'Data': html_body}}
                    }
                )
                print(f"Resolution email sent to {user_email}")
            except Exception as e:
                print(f"Failed to send resolution email to {user_email}: {e}")
                
        return {'statusCode': 200, 'body': 'Successfully processed webhook'}
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return {'statusCode': 500, 'body': 'Internal Server Error'}
