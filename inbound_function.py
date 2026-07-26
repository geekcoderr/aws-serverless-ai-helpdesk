import json
import os
import boto3
import email
from email import policy
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
import base64
import urllib.parse
import re
import time
from datetime import datetime
from boto3.dynamodb.conditions import Key

# Initialize AWS SDK Clients
s3_client = boto3.client('s3')
bedrock_client = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received SQS event:", json.dumps(event))
    
    dynamodb_table_name = os.environ.get('DYNAMODB_TABLE')
    table = None
    if dynamodb_table_name:
        table = dynamodb.Table(dynamodb_table_name)
    
    for record in event.get('Records', []):
        try:
            # 1. Parse the S3 event
            s3_event = json.loads(record['body'])
            if 'Records' not in s3_event or len(s3_event['Records']) == 0:
                continue
                
            s3_record = s3_event['Records'][0]
            bucket_name = s3_record['s3']['bucket']['name']
            object_key = urllib.parse.unquote_plus(s3_record['s3']['object']['key'])
            
            if not object_key.startswith('incoming/'):
                continue
                
            print(f"Processing email from Bucket: {bucket_name}, Key: {object_key}")
            
            # 2. Download raw .eml
            response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            eml_content = response['Body'].read()
            
            # 3. Parse email
            parsed_email = email.message_from_bytes(eml_content, policy=policy.default)
            subject = parsed_email.get('subject', 'No Subject')
            from_address = parsed_email.get('from', 'Unknown Sender')
            message_id = parsed_email.get('Message-ID')
            
            # Extract raw email address
            email_match = re.search(r'<([^>]+)>', from_address)
            raw_email = email_match.group(1) if email_match else from_address
            
            # 3.5 Dedup Check
            if table and message_id:
                dedup_key = f"MSGID#{message_id}"
                ddb_res = table.get_item(Key={'PK': dedup_key, 'SK': 'DEDUP'})
                if 'Item' in ddb_res:
                    print(f"Duplicate Message-ID detected: {message_id}. Skipping.")
                    # Still need to delete from S3
                    s3_client.delete_object(Bucket=bucket_name, Key=object_key)
                    continue
                
                # Write dedup record immediately to prevent race conditions
                table.put_item(Item={
                    'PK': dedup_key,
                    'SK': 'DEDUP',
                    'processed_at': datetime.now().isoformat(),
                    'ttl': int(time.time()) + (7 * 24 * 60 * 60)
                })

            # ... Parse body and attachments ...
            text_body = ""
            attachments = []
            
            for part in parsed_email.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition'))
                
                if part.is_multipart():
                    continue
                    
                if 'attachment' in content_disposition or 'inline' in content_disposition or part.get_filename():
                    filename = part.get_filename()
                    if filename:
                        payload = part.get_payload(decode=True)
                        if payload:
                            attachments.append((filename, payload, content_type))
                elif content_type == 'text/plain' and not text_body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        text_body = payload.decode('utf-8', errors='ignore')
            
            if not text_body:
                text_body = "No text content found in email."
                
            # 4. Call Bedrock
            bedrock_prompt = f"""You are an IT Support triage AI. Analyze the email and output ONLY a JSON object.
Fields:
- "intent": Determine if the user is asking for a new ticket ("new_ticket"), asking for a status update on an existing issue ("status_request"), or replying with more information ("update_existing").
- "ticket_id": If a Jira issue key (e.g. SUP-123 or IT-45) is mentioned in the subject or body, extract it. Otherwise, return null.
- "priority": High, Medium, Low
- "category": Bug, Access, Billing, General
- "summary": Max 2 sentences summarizing the request.
- "fingerprint": A short 2-4 word normalized string describing the core problem (e.g. "vpn-connection-failure", "password-reset", "billing-question"). Use lowercase and hyphens.

Email Subject: {subject}
Email Body: {text_body}"""

            print("Invoking Bedrock (Amazon Nova Lite)...")
            bedrock_response = bedrock_client.converse(
                modelId="amazon.nova-lite-v1:0",
                messages=[{"role": "user", "content": [{"text": bedrock_prompt}]}],
                inferenceConfig={"maxTokens": 300, "temperature": 0.0}
            )

            ai_text = bedrock_response['output']['message']['content'][0]['text']
            print("Bedrock Response:", ai_text)
            
            ai_analysis = {"intent": "new_ticket", "ticket_id": None, "priority": "Medium", "category": "General", "summary": subject, "fingerprint": "general-issue"}
            try:
                start_idx = ai_text.find('{')
                end_idx = ai_text.rfind('}') + 1
                if start_idx != -1 and end_idx != -1:
                    ai_analysis.update(json.loads(ai_text[start_idx:end_idx]))
            except Exception as e:
                print(f"Failed to parse AI JSON: {e}")
                
            intent = ai_analysis.get('intent', 'new_ticket')
            ticket_id = ai_analysis.get('ticket_id')
            fingerprint = ai_analysis.get('fingerprint', 'general-issue')
            
            # 5. Jira Integration
            jira_domain = os.environ.get('JIRA_DOMAIN')
            jira_email = os.environ.get('JIRA_EMAIL')
            jira_api_token = os.environ.get('JIRA_API_TOKEN')
            jira_project_key = os.environ.get('JIRA_PROJECT_KEY', 'SUP')
            sender_email = os.environ.get('SENDER_EMAIL')
            sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
            
            if jira_domain and jira_email and jira_api_token:
                auth_string = f"{jira_email}:{jira_api_token}"
                auth_encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                headers = {"Authorization": f"Basic {auth_encoded}", "Content-Type": "application/json"}
                
                safe_body = text_body if len(text_body) < 30000 else text_body[:30000] + "... [TRUNCATED]"
                safe_body = safe_body.replace('\x00', '[NULL]')
                
                # Check Jira status if ticket_id is provided
                ticket_is_open = False
                issue_summary = ""
                current_status = ""
                
                if ticket_id:
                    issue_res = requests.get(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}", headers=headers)
                    if issue_res.ok:
                        issue_data = issue_res.json()
                        current_status = issue_data['fields']['status']['name']
                        issue_summary = issue_data['fields']['summary']
                        status_category = issue_data['fields']['status']['statusCategory']['key']
                        
                        if status_category != 'done':
                            ticket_is_open = True
                        else:
                            print(f"Ticket {ticket_id} is CLOSED. Converting to new_ticket.")
                            intent = 'new_ticket'
                    else:
                        print(f"Ticket {ticket_id} not found in Jira, falling back to new_ticket.")
                        intent = 'new_ticket'
                        ticket_id = None
                        
                # ACTION: update_existing or status_request
                if intent in ['status_request', 'update_existing'] and ticket_id and ticket_is_open:
                    print(f"Handling existing open ticket: {ticket_id}")
                    # Add comment
                    comment_payload = {"body": f"User {from_address} sent an email update:\n\n{safe_body}"}
                    requests.post(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}/comment", json=comment_payload, headers=headers)
                    
                    # Upload attachments
                    if attachments:
                        attach_headers = {"Authorization": f"Basic {auth_encoded}", "X-Atlassian-Token": "no-check"}
                        for filename, content_bytes, content_type in attachments:
                            m_data = MultipartEncoder(fields={'file': (filename, content_bytes, content_type)})
                            attach_headers['Content-Type'] = m_data.content_type
                            requests.post(f"https://{jira_domain}/rest/api/3/issue/{ticket_id}/attachments", data=m_data, headers=attach_headers)
                            
                    # Reply with status
                    if sender_email:
                        html_body = f"""
                        <html><body>
                        <h2>Update on your ticket: {ticket_id}</h2>
                        <p>Hi there,</p>
                        <p>Here is the current status of your request:</p>
                        <ul>
                            <li><strong>Ticket ID:</strong> {ticket_id}</li>
                            <li><strong>Summary:</strong> {issue_summary}</li>
                            <li><strong>Current Status:</strong> <strong style="color:blue;">{current_status}</strong></li>
                        </ul>
                        <p>We have added your latest message to the ticket. Our team is actively reviewing it!</p>
                        <p>Best,<br>{sender_name}</p>
                        </body></html>
                        """
                        ses_client.send_email(
                            Source=f"{sender_name} <{sender_email}>",
                            Destination={'ToAddresses': [from_address]},
                            Message={
                                'Subject': {'Data': f"Re: {subject} [{ticket_id}]"},
                                'Body': {'Html': {'Data': html_body}}
                            }
                        )
                        
                # ACTION: new_ticket
                elif intent == 'new_ticket':
                    incident_matched_ticket = None
                    
                    # Check DynamoDB for existing incident fingerprint
                    if table and fingerprint and fingerprint != "general-issue":
                        try:
                            ddb_query = table.query(
                                IndexName='fingerprint-index',
                                KeyConditionExpression=Key('fingerprint').eq(fingerprint)
                            )
                            # Find the META record which holds the ticket_id
                            for item in ddb_query.get('Items', []):
                                if item['PK'].startswith('INCIDENT#') and item['SK'] == 'META':
                                    # Ensure the ticket is still open in Jira
                                    inc_ticket = item.get('ticket_id')
                                    if inc_ticket:
                                        t_res = requests.get(f"https://{jira_domain}/rest/api/2/issue/{inc_ticket}", headers=headers)
                                        if t_res.ok and t_res.json()['fields']['status']['statusCategory']['key'] != 'done':
                                            incident_matched_ticket = inc_ticket
                                            current_status = t_res.json()['fields']['status']['name']
                                            issue_summary = t_res.json()['fields']['summary']
                                            break
                        except Exception as e:
                            print(f"Error querying DynamoDB for incident: {e}")
                    
                    if incident_matched_ticket:
                        # THIS IS A KNOWN INCIDENT (Duplicate)
                        print(f"Matched active incident: {fingerprint} -> {incident_matched_ticket}")
                        
                        if table:
                            # Add user as watcher
                            table.put_item(Item={
                                'PK': f"INCIDENT#{fingerprint}",
                                'SK': f"REPORTER#{raw_email}",
                                'ticket_id': incident_matched_ticket,
                                'notified_at': datetime.now().isoformat(),
                                'fingerprint': fingerprint
                            })
                            # Add mapping
                            table.put_item(Item={
                                'PK': f"EMAIL#{raw_email}",
                                'SK': f"TICKET#{incident_matched_ticket}",
                                'status': current_status,
                                'subject': subject,
                                'created_at': datetime.now().isoformat()
                            })
                            
                        # Email user that we are tracking it
                        if sender_email:
                            html_body = f"""
                            <html><body>
                            <h2>Incident Already Tracked</h2>
                            <p>Hi there,</p>
                            <p>We are already tracking this issue under an active incident. We have linked your report to the master ticket.</p>
                            <ul>
                                <li><strong>Ticket ID:</strong> {incident_matched_ticket}</li>
                                <li><strong>Incident:</strong> {issue_summary}</li>
                                <li><strong>Current Status:</strong> <strong style="color:blue;">{current_status}</strong></li>
                            </ul>
                            <p>We will keep you updated as we work to resolve this.</p>
                            <p>Best,<br>{sender_name}</p>
                            </body></html>
                            """
                            ses_client.send_email(
                                Source=f"{sender_name} <{sender_email}>",
                                Destination={'ToAddresses': [from_address]},
                                Message={
                                    'Subject': {'Data': f"Incident Tracked: {subject} [{incident_matched_ticket}]"},
                                    'Body': {'Html': {'Data': html_body}}
                                }
                            )
                            
                    else:
                        # THIS IS A BRAND NEW PROBLEM
                        print("Creating brand new ticket...")
                        issue_type_name = "Task"
                        proj_response = requests.get(f"https://{jira_domain}/rest/api/2/project/{jira_project_key}", headers=headers)
                        if proj_response.ok:
                            valid_types = [it['name'] for it in proj_response.json().get('issueTypes', []) if not it.get('subtask', False)]
                            if valid_types and "Task" not in valid_types:
                                issue_type_name = valid_types[0]

                        jira_payload = {
                            "fields": {
                                "project": {"key": jira_project_key},
                                "summary": f"[{ai_analysis.get('category', 'General')}] {subject}",
                                "description": f"From: {from_address}\nPriority: {ai_analysis.get('priority', 'Medium')}\n\nAI Summary: {ai_analysis.get('summary', '')}\n\nOriginal Email:\n{safe_body}",
                                "issuetype": {"name": issue_type_name},
                                "priority": {"name": ai_analysis.get('priority', 'Medium')}
                            }
                        }

                        jira_response = requests.post(f"https://{jira_domain}/rest/api/2/issue", json=jira_payload, headers=headers)
                        jira_response.raise_for_status()
                        issue_key = jira_response.json().get('key')
                        print(f"Jira ticket created successfully: {issue_key}")
                        
                        if issue_key and attachments:
                            attach_headers = {"Authorization": f"Basic {auth_encoded}", "X-Atlassian-Token": "no-check"}
                            for filename, content_bytes, content_type in attachments:
                                m_data = MultipartEncoder(fields={'file': (filename, content_bytes, content_type)})
                                attach_headers['Content-Type'] = m_data.content_type
                                requests.post(f"https://{jira_domain}/rest/api/3/issue/{issue_key}/attachments", data=m_data, headers=attach_headers)
                        
                        # Record in DynamoDB
                        if table and issue_key:
                            table.put_item(Item={
                                'PK': f"EMAIL#{raw_email}",
                                'SK': f"TICKET#{issue_key}",
                                'status': 'Open',
                                'subject': subject,
                                'created_at': datetime.now().isoformat()
                            })
                            if fingerprint and fingerprint != "general-issue":
                                table.put_item(Item={
                                    'PK': f"INCIDENT#{fingerprint}",
                                    'SK': 'META',
                                    'ticket_id': issue_key,
                                    'status': 'Open',
                                    'fingerprint': fingerprint,
                                    'created_at': datetime.now().isoformat()
                                })
                                table.put_item(Item={
                                    'PK': f"INCIDENT#{fingerprint}",
                                    'SK': f"REPORTER#{raw_email}",
                                    'ticket_id': issue_key,
                                    'notified_at': datetime.now().isoformat(),
                                    'fingerprint': fingerprint
                                })
                        
                        if sender_email:
                            html_body = f"""
                            <html><body>
                            <h2>Support Ticket Created</h2>
                            <p>Hi there,</p>
                            <p>We have successfully received your request and our AI assistant has triaged it for our human team.</p>
                            <ul>
                                <li><strong>Ticket ID:</strong> {issue_key}</li>
                                <li><strong>Category:</strong> {ai_analysis.get('category', 'General')}</li>
                                <li><strong>Priority:</strong> {ai_analysis.get('priority', 'Medium')}</li>
                            </ul>
                            <p>Our team will look into this and get back to you shortly.</p>
                            <p>Best,<br>{sender_name}</p>
                            </body></html>
                            """
                            ses_client.send_email(
                                Source=f"{sender_name} <{sender_email}>",
                                Destination={'ToAddresses': [from_address]},
                                Message={
                                    'Subject': {'Data': f"Support Request Received: {issue_key}"},
                                    'Body': {'Html': {'Data': html_body}}
                                }
                            )
            
            print(f"Deleting {object_key} from S3...")
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            print("S3 deletion successful.")
            
        except Exception as e:
            print(f"Error processing record: {e}")
            raise e
            
    return {
        'statusCode': 200,
        'body': json.dumps('Success')
    }
