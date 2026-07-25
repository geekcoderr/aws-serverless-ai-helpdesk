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

# Initialize AWS SDK Clients
s3_client = boto3.client('s3')
bedrock_client = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received SQS event:", json.dumps(event))
    
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
                
            # 4. Call Bedrock to classify and extract intent
            bedrock_prompt = f"""You are an IT Support triage AI. Analyze the email and output ONLY a JSON object.
Fields:
- "intent": Determine if the user is asking for a new ticket ("new_ticket"), asking for a status update on an existing issue ("status_request"), or replying with more information ("update_existing").
- "ticket_id": If a Jira issue key (e.g. SUP-123 or IT-45) is mentioned in the subject or body, extract it. Otherwise, return null.
- "priority": High, Medium, Low
- "category": Bug, Access, Billing, General
- "summary": Max 2 sentences summarizing the request.

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
            
            ai_analysis = {"intent": "new_ticket", "ticket_id": None, "priority": "Medium", "category": "General", "summary": subject}
            try:
                start_idx = ai_text.find('{')
                end_idx = ai_text.rfind('}') + 1
                if start_idx != -1 and end_idx != -1:
                    ai_analysis = json.loads(ai_text[start_idx:end_idx])
            except Exception as e:
                print(f"Failed to parse AI JSON: {e}")
                
            intent = ai_analysis.get('intent', 'new_ticket')
            ticket_id = ai_analysis.get('ticket_id')
            
            # 5. Jira Integration
            jira_domain = os.environ.get('JIRA_DOMAIN')
            jira_email = os.environ.get('JIRA_EMAIL')
            jira_api_token = os.environ.get('JIRA_API_TOKEN')
            jira_project_key = os.environ.get('JIRA_PROJECT_KEY', 'SUP')
            sender_email = os.environ.get('SENDER_EMAIL')
            
            if jira_domain and jira_email and jira_api_token:
                auth_string = f"{jira_email}:{jira_api_token}"
                auth_encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                headers = {"Authorization": f"Basic {auth_encoded}", "Content-Type": "application/json"}
                
                # Check for duplicate if it's a new ticket
                if intent == 'new_ticket':
                    print("Checking for duplicate tickets...")
                    # Escape quotes in JQL
                    safe_subject = subject.replace('"', '\\"')
                    safe_from = from_address.replace('"', '\\"')
                    
                    # Extract email address between < >
                    email_match = re.search(r'<([^>]+)>', safe_from)
                    if email_match:
                        raw_email = email_match.group(1)
                    else:
                        raw_email = safe_from
                        
                    jql = f'project = {jira_project_key} AND description ~ "{raw_email}" AND statusCategory != Done ORDER BY created DESC'
                    search_res = requests.get(f"https://{jira_domain}/rest/api/2/search", params={'jql': jql, 'maxResults': 5}, headers=headers)
                    if search_res.ok:
                        issues = search_res.json().get('issues', [])
                        # Look for a ticket with a very similar subject
                        for iss in issues:
                            if iss['fields']['summary'].lower() in subject.lower() or subject.lower() in iss['fields']['summary'].lower():
                                print(f"Found existing ticket {iss['key']} matching this request. Converting to update_existing.")
                                intent = 'update_existing'
                                ticket_id = iss['key']
                                break
                                
                safe_body = text_body if len(text_body) < 30000 else text_body[:30000] + "... [TRUNCATED]"
                safe_body = safe_body.replace('\x00', '[NULL]')
                
                if intent in ['status_request', 'update_existing'] and ticket_id:
                    print(f"Handling existing ticket: {ticket_id}")
                    # 1. Fetch current status
                    issue_res = requests.get(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}", headers=headers)
                    if issue_res.ok:
                        issue_data = issue_res.json()
                        current_status = issue_data['fields']['status']['name']
                        issue_summary = issue_data['fields']['summary']
                        
                        # 2. Add comment
                        comment_payload = {"body": f"User {from_address} sent an email update:\n\n{safe_body}"}
                        requests.post(f"https://{jira_domain}/rest/api/2/issue/{ticket_id}/comment", json=comment_payload, headers=headers)
                        
                        # Upload attachments to existing ticket
                        if attachments:
                            attach_headers = {"Authorization": f"Basic {auth_encoded}", "X-Atlassian-Token": "no-check"}
                            for filename, content_bytes, content_type in attachments:
                                m_data = MultipartEncoder(fields={'file': (filename, content_bytes, content_type)})
                                attach_headers['Content-Type'] = m_data.content_type
                                requests.post(f"https://{jira_domain}/rest/api/3/issue/{ticket_id}/attachments", data=m_data, headers=attach_headers)
                                
                        # 3. Send email reply with status
                        if sender_email:
                            html_body = f"""
                            <html><body>
                            <h2>Update on your ticket: {ticket_id}</h2>
                            <p>Hi there,</p>
                            <p>You recently asked for an update, or replied to an existing ticket. Here is the current status of your request:</p>
                            <ul>
                                <li><strong>Ticket ID:</strong> {ticket_id}</li>
                                <li><strong>Summary:</strong> {issue_summary}</li>
                                <li><strong>Current Status:</strong> <strong style="color:blue;">{current_status}</strong></li>
                            </ul>
                            <p>We have added your latest message to the ticket. Our team is actively reviewing it!</p>
                            <p>Best,<br>Support Team</p>
                            </body></html>
                            """
                            sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
                            ses_client.send_email(
                                Source=f"{sender_name} <{sender_email}>",
                                Destination={'ToAddresses': [from_address]},
                                Message={
                                    'Subject': {'Data': f"Re: {subject} [{ticket_id}]"},
                                    'Body': {'Html': {'Data': html_body}}
                                }
                            )
                    else:
                        print(f"Ticket {ticket_id} not found in Jira, falling back to new ticket.")
                        intent = 'new_ticket' # Fallback
                        
                if intent == 'new_ticket':
                    print("Creating new ticket...")
                    # Get valid issue type
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
                        <p>Best,<br>Support Team</p>
                        </body></html>
                        """
                        sender_name = os.environ.get('ORGANIZATION_NAME', 'IT Helpdesk')
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
