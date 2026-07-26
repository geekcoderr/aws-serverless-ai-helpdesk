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
    table = dynamodb.Table(dynamodb_table_name) if dynamodb_table_name else None
    
    jira_domain = os.environ.get('JIRA_DOMAIN')
    jira_email = os.environ.get('JIRA_EMAIL')
    jira_api_token = os.environ.get('JIRA_API_TOKEN')
    jira_project_key = os.environ.get('JIRA_PROJECT_KEY', 'SUP')
    sender_email = os.environ.get('SENDER_EMAIL')
    sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
    
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
                
            # 2. Download raw .eml
            response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            eml_content = response['Body'].read()
            
            # 3. Parse email
            parsed_email = email.message_from_bytes(eml_content, policy=policy.default)
            subject = parsed_email.get('subject', 'No Subject')
            from_address = parsed_email.get('from', 'Unknown Sender')
            message_id = parsed_email.get('Message-ID')
            
            email_match = re.search(r'<([^>]+)>', from_address)
            raw_email = email_match.group(1) if email_match else from_address
            
            # 4. Dedup Check
            if table and message_id:
                dedup_key = f"MSGID#{message_id}"
                if 'Item' in table.get_item(Key={'PK': dedup_key, 'SK': 'DEDUP'}):
                    print(f"Duplicate Message-ID detected: {message_id}. Skipping.")
                    s3_client.delete_object(Bucket=bucket_name, Key=object_key)
                    continue
                
                table.put_item(Item={
                    'PK': dedup_key,
                    'SK': 'DEDUP',
                    'processed_at': datetime.now().isoformat(),
                    'ttl': int(time.time()) + (7 * 24 * 60 * 60)
                })

            text_body = ""
            attachments = []
            
            for part in parsed_email.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition'))
                if part.is_multipart():
                    continue
                if 'attachment' in content_disposition or 'inline' in content_disposition or part.get_filename():
                    if part.get_filename():
                        payload = part.get_payload(decode=True)
                        if payload:
                            attachments.append((part.get_filename(), payload, content_type))
                elif content_type == 'text/plain' and not text_body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        text_body = payload.decode('utf-8', errors='ignore')
            
            if not text_body:
                text_body = "No text content found in email."
                
            safe_body = text_body if len(text_body) < 30000 else text_body[:30000] + "... [TRUNCATED]"
            safe_body = safe_body.replace('\x00', '[NULL]')

            # 5. Fetch Active Jira Issues for Contextual AI Matching
            recent_issues_context = ""
            if jira_domain and jira_email and jira_api_token:
                auth_string = f"{jira_email}:{jira_api_token}"
                auth_encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                headers = {"Authorization": f"Basic {auth_encoded}", "Content-Type": "application/json"}
                
                jql = f'project = {jira_project_key} AND statusCategory != Done ORDER BY created DESC'
                # Note: Jira recently deprecated /rest/api/2/search via GET, requires POST to /rest/api/3/search/jql or GET /rest/api/3/search
                search_payload = {
                    "jql": jql,
                    "maxResults": 15,
                    "fields": ["summary"]
                }
                search_res = requests.post(f"https://{jira_domain}/rest/api/3/search/jql", json=search_payload, headers=headers)
                if search_res.ok:
                    issues = search_res.json().get('issues', [])
                    print(f"DEBUG JQL found {len(issues)} issues.")
                    if issues:
                        recent_issues_context = "Currently Open Tickets:\n"
                        for iss in issues:
                            recent_issues_context += f"ID: {iss['key']} | Summary: {iss['fields']['summary']}\n"
                else:
                    print(f"DEBUG JQL Error: {search_res.text}")
            
            # 6. Call Bedrock
            bedrock_prompt = f"""You are an IT Support triage AI. Analyze the incoming email and compare it against the currently open tickets.
Output ONLY a JSON object.

Fields:
- "intent": Always use "new_ticket" if the user is reporting a problem/issue. Use "update_existing" ONLY if the user is explicitly replying to an existing ticket.
- "ticket_id": 
   - IF the user explicitly mentions a ticket ID (e.g. SUP-123) in their email, extract it.
   - ELSE IF the user's issue is strongly related to or describing the SAME underlying incident/bug as one of the "Currently Open Tickets" provided below, output that matching ticket ID. Use semantic reasoning. If multiple users report login failures, database timeouts, etc. in different words, they should be grouped to the same ticket_id.
   - ELSE return null.
- "priority": High, Medium, Low
- "category": Bug, Access, Billing, General
- "summary": Max 2 sentences summarizing the request.

{recent_issues_context if recent_issues_context else 'No currently open tickets.'}

Incoming Email Subject: {subject}
Incoming Email Body: {text_body}"""

            print(f"DEBUG Bedrock Prompt:\n{bedrock_prompt}")

            bedrock_response = bedrock_client.converse(
                modelId="amazon.nova-lite-v1:0",
                messages=[{"role": "user", "content": [{"text": bedrock_prompt}]}],
                inferenceConfig={"maxTokens": 300, "temperature": 0.0}
            )

            ai_text = bedrock_response['output']['message']['content'][0]['text']
            print(f"DEBUG Bedrock Response:\n{ai_text}")
            ai_analysis = {"intent": "new_ticket", "ticket_id": None, "priority": "Medium", "category": "General", "summary": subject}
            try:
                start_idx = ai_text.find('{')
                end_idx = ai_text.rfind('}') + 1
                if start_idx != -1 and end_idx != -1:
                    ai_analysis.update(json.loads(ai_text[start_idx:end_idx]))
            except Exception as e:
                print(f"Failed to parse AI JSON: {e}")
                
            intent = ai_analysis.get('intent', 'new_ticket')
            ticket_id = ai_analysis.get('ticket_id')
            
            # 7. Action Logic
            if jira_domain:
                ticket_is_open = False
                issue_summary = ""
                
                if ticket_id:
                    issue_res = requests.get(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}", headers=headers)
                    if issue_res.ok:
                        issue_data = issue_res.json()
                        issue_summary = issue_data['fields']['summary']
                        issue_status_name = issue_data['fields']['status']['name']
                        status_category = issue_data['fields']['status']['statusCategory']['key']
                        
                        if status_category != 'done':
                            ticket_is_open = True
                        else:
                            print(f"Ticket {ticket_id} is CLOSED. Forcing new_ticket intent.")
                            intent = 'new_ticket'
                            ticket_id = None
                    else:
                        intent = 'new_ticket'
                        ticket_id = None
                        
                # A: Status Request or Update to existing
                if intent in ['status_request', 'update_existing'] and ticket_id and ticket_is_open:
                    comment_payload = {"body": f"User {from_address} sent an email update:\n\n{safe_body}"}
                    requests.post(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}/comment", json=comment_payload, headers=headers)
                    
                    if attachments:
                        attach_headers = {"Authorization": f"Basic {auth_encoded}", "X-Atlassian-Token": "no-check"}
                        for filename, content_bytes, content_type in attachments:
                            m_data = MultipartEncoder(fields={'file': (filename, content_bytes, content_type)})
                            attach_headers['Content-Type'] = m_data.content_type
                            requests.post(f"https://{jira_domain}/rest/api/3/issue/{ticket_id}/attachments", data=m_data, headers=attach_headers)
                            
                    if sender_email:
                        html_body = f"""
                        <div style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden;">
                            <div style="background-color: #0052CC; padding: 15px 20px; color: #fff;">
                                <h2 style="margin: 0; font-size: 20px;">Support Request Updated</h2>
                            </div>
                            <div style="padding: 20px;">
                                <p>Hello,</p>
                                <p>We have successfully received your latest message and added it to the active support ticket.</p>
                                <ul style="background-color: #f5f5f5; padding: 15px 15px 15px 35px; border-radius: 4px;">
                                    <li><strong>Reference ID:</strong> {ticket_id}</li>
                                    <li><strong>Status:</strong> {issue_status_name}</li>
                                </ul>
                                <p>Our engineering team is actively investigating and working on a resolution for this reported issue.</p>
                                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                                <p style="font-size: 12px; color: #777;">Best regards,<br><strong>{sender_name}</strong></p>
                            </div>
                        </div>
                        """
                        ses_client.send_email(
                            Source=f"{sender_name} <{sender_email}>",
                            Destination={'ToAddresses': [from_address]},
                            Message={
                                'Subject': {'Data': f"Re: {subject} [{ticket_id}]"},
                                'Body': {'Html': {'Data': html_body}}
                            }
                        )
                        
                # B: Known Incident (AI found a match for a new issue)
                elif intent == 'new_ticket' and ticket_id and ticket_is_open:
                    print(f"AI matched this email to existing incident: {ticket_id}")
                    
                    watcher_count = 0
                    if table:
                        table.put_item(Item={
                            'PK': f"INCIDENT#{ticket_id}",
                            'SK': f"REPORTER#{raw_email}",
                            'notified_at': datetime.now().isoformat()
                        })
                        try:
                            ddb_query = table.query(KeyConditionExpression=Key('PK').eq(f"INCIDENT#{ticket_id}"))
                            watcher_count = ddb_query.get('Count', 1) - 1 # Exclude the original reporter
                        except Exception:
                            pass
                            
                    if sender_email:
                        count_text = f"{watcher_count} other users have" if watcher_count > 1 else "Another user has" if watcher_count == 1 else "Other users have"
                        html_body = f"""
                        <div style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden;">
                            <div style="background-color: #FF991F; padding: 15px 20px; color: #fff;">
                                <h2 style="margin: 0; font-size: 20px;">Incident Tracked</h2>
                            </div>
                            <div style="padding: 20px;">
                                <p>Hello,</p>
                                <p>This issue is currently tracking under an active master incident. {count_text} also reported this event.</p>
                                <ul style="background-color: #f5f5f5; padding: 15px 15px 15px 35px; border-radius: 4px;">
                                    <li><strong>Reference ID:</strong> {ticket_id}</li>
                                    <li><strong>Status:</strong> {issue_status_name}</li>
                                </ul>
                                <p>We have linked your report to the master incident. Our engineering team is actively working on a resolution, and you will be notified automatically when it is resolved.</p>
                                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                                <p style="font-size: 12px; color: #777;">Best regards,<br><strong>{sender_name}</strong></p>
                            </div>
                        </div>
                        """
                        ses_client.send_email(
                            Source=f"{sender_name} <{sender_email}>",
                            Destination={'ToAddresses': [from_address]},
                            Message={
                                'Subject': {'Data': f"Incident Tracked: {subject} [{ticket_id}]"},
                                'Body': {'Html': {'Data': html_body}}
                            }
                        )

                # C: Brand New Ticket
                elif intent == 'new_ticket' and not ticket_id:
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
                    
                    if issue_key and attachments:
                        attach_headers = {"Authorization": f"Basic {auth_encoded}", "X-Atlassian-Token": "no-check"}
                        for filename, content_bytes, content_type in attachments:
                            m_data = MultipartEncoder(fields={'file': (filename, content_bytes, content_type)})
                            attach_headers['Content-Type'] = m_data.content_type
                            requests.post(f"https://{jira_domain}/rest/api/3/issue/{issue_key}/attachments", data=m_data, headers=attach_headers)
                    
                    if table and issue_key:
                        table.put_item(Item={
                            'PK': f"EMAIL#{raw_email}",
                            'SK': f"TICKET#{issue_key}",
                            'status': 'Open'
                        })
                        table.put_item(Item={
                            'PK': f"INCIDENT#{issue_key}",
                            'SK': f"REPORTER#{raw_email}",
                            'notified_at': datetime.now().isoformat()
                        })
                    
                    if sender_email:
                        html_body = f"""
                        <div style="font-family: Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 5px; overflow: hidden;">
                            <div style="background-color: #00875A; padding: 15px 20px; color: #fff;">
                                <h2 style="margin: 0; font-size: 20px;">Support Request Received</h2>
                            </div>
                            <div style="padding: 20px;">
                                <p>Hello,</p>
                                <p>Your support request has been acknowledged and is securely in our queue for review.</p>
                                <ul style="background-color: #f5f5f5; padding: 15px 15px 15px 35px; border-radius: 4px;">
                                    <li><strong>Reference ID:</strong> {issue_key}</li>
                                    <li><strong>Status:</strong> Open</li>
                                    <li><strong>Category:</strong> {ai_analysis.get('category', 'General')}</li>
                                    <li><strong>Priority:</strong> {ai_analysis.get('priority', 'Medium')}</li>
                                </ul>
                                <p>Our team will look into this and provide updates accordingly.</p>
                                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                                <p style="font-size: 12px; color: #777;">Best regards,<br><strong>{sender_name}</strong></p>
                            </div>
                        </div>
                        """
                        ses_client.send_email(
                            Source=f"{sender_name} <{sender_email}>",
                            Destination={'ToAddresses': [from_address]},
                            Message={
                                'Subject': {'Data': f"Support Request Received: {issue_key}"},
                                'Body': {'Html': {'Data': html_body}}
                            }
                        )
            
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            
        except Exception as e:
            print(f"Error processing record: {e}")
            raise e
            
    return {'statusCode': 200, 'body': json.dumps('Success')}
