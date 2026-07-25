import json
import os
import boto3
import email
from email import policy
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
import base64
import urllib.parse

# Initialize AWS SDK Clients
s3_client = boto3.client('s3')
bedrock_client = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
ses_client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

def lambda_handler(event, context):
    print("Received SQS event:", json.dumps(event))
    
    for record in event.get('Records', []):
        try:
            # 1. Parse the S3 event embedded inside the SQS message body
            s3_event = json.loads(record['body'])
            if 'Records' not in s3_event or len(s3_event['Records']) == 0:
                print("No S3 records found in SQS message. Skipping.")
                continue
                
            s3_record = s3_event['Records'][0]
            bucket_name = s3_record['s3']['bucket']['name']
            object_key = urllib.parse.unquote_plus(s3_record['s3']['object']['key'])
            
            # SAFEGUARD: Prevent infinite loops if S3 Event Notifications aren't filtered by prefix
            if not object_key.startswith('incoming/'):
                print(f"Ignoring {object_key} (not in 'incoming/' directory).")
                continue
                
            print(f"Processing email from Bucket: {bucket_name}, Key: {object_key}")
            
            # 2. Download the raw .eml file from S3
            response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            eml_content = response['Body'].read()
            
            # 3. Parse the email and extract attachments
            parsed_email = email.message_from_bytes(eml_content, policy=policy.default)
            subject = parsed_email.get('subject', 'No Subject')
            from_address = parsed_email.get('from', 'Unknown Sender')
            
            text_body = ""
            attachments = [] # List of tuples: (filename, content_bytes, content_type)
            
            for part in parsed_email.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition'))
                
                # Skip multipart containers
                if part.is_multipart():
                    continue
                    
                # Identify attachments or inline images
                if 'attachment' in content_disposition or 'inline' in content_disposition or part.get_filename():
                    filename = part.get_filename()
                    if filename:
                        payload = part.get_payload(decode=True)
                        if payload:
                            attachments.append((filename, payload, content_type))
                # Identify the main text body (ignore HTML for now to keep Jira clean)
                elif content_type == 'text/plain' and not text_body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        text_body = payload.decode('utf-8', errors='ignore')
            
            if not text_body:
                text_body = "No text content found in email (possibly HTML only or empty)."
                
            print(f"Email parsed. From: {from_address}. Found {len(attachments)} attachments.")
            
            # 4. Call Bedrock (Amazon Titan) to classify the email
            bedrock_prompt = f"You are an IT Support triage AI. Analyze the following email and output ONLY a JSON object with 'priority' (High, Medium, Low), 'category' (Bug, Access, Billing, General), and a brief 'summary' (max 2 sentences).\n\nEmail Subject: {subject}\nEmail Body: {text_body}"
            
            print("Invoking Bedrock (Amazon Nova Lite)...")
            bedrock_response = bedrock_client.converse(
                modelId="amazon.nova-lite-v1:0",
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": bedrock_prompt}]
                    }
                ],
                inferenceConfig={
                    "maxTokens": 300,
                    "temperature": 0.0
                }
            )

            ai_text = bedrock_response['output']['message']['content'][0]['text']
            
            print("Bedrock Response:", ai_text)
            
            # Extract JSON from Bedrock response securely
            ai_analysis = {"priority": "Medium", "category": "General", "summary": subject}
            try:
                start_idx = ai_text.find('{')
                end_idx = ai_text.rfind('}') + 1
                if start_idx != -1 and end_idx != -1:
                    json_str = ai_text[start_idx:end_idx]
                    ai_analysis = json.loads(json_str)
            except Exception as e:
                print(f"Failed to parse AI JSON, falling back to defaults. Error: {e}")
                
            # 5. Call Jira REST API (Step 1: Create Issue)
            jira_domain = os.environ.get('JIRA_DOMAIN') # e.g., "company.atlassian.net"
            jira_email = os.environ.get('JIRA_EMAIL')
            jira_api_token = os.environ.get('JIRA_API_TOKEN')
            jira_project_key = os.environ.get('JIRA_PROJECT_KEY', 'SUP')
            
            if jira_domain and jira_email and jira_api_token:
                print(f"Creating Jira ticket in project key: '{jira_project_key}'...")
                auth_string = f"{jira_email}:{jira_api_token}"
                auth_encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

                headers = {
                    "Authorization": f"Basic {auth_encoded}",
                    "Content-Type": "application/json"
                }

                # Auto-detect valid issue type
                issue_type_name = "Task"
                project_url = f"https://{jira_domain}/rest/api/2/project/{jira_project_key}"
                proj_response = requests.get(project_url, headers=headers)
                
                if proj_response.ok:
                    proj_data = proj_response.json()
                    valid_types = [it['name'] for it in proj_data.get('issueTypes', []) if not it.get('subtask', False)]
                    print(f"Project '{jira_project_key}' found. Available issue types: {valid_types}")
                    if valid_types and "Task" not in valid_types:
                        issue_type_name = valid_types[0] # Fallback to the first available issue type (e.g., 'Story', 'Bug')
                else:
                    print(f"WARNING: Could not fetch project '{jira_project_key}'. Error: {proj_response.text}")

                print(f"Using issue type: {issue_type_name}")

                jira_url = f"https://{jira_domain}/rest/api/2/issue"
                
                # Truncate text_body if it's ridiculously long to avoid Jira limits (32k limit)
                safe_body = text_body if len(text_body) < 30000 else text_body[:30000] + "... [TRUNCATED]"
                
                # Jira API strictly rejects null characters (\u0000) in text fields.
                # Corrupted emails or binary attachments parsed as text can contain these.
                safe_body = safe_body.replace('\x00', '[NULL]')

                jira_payload = {
                    "fields": {
                        "project": {"key": jira_project_key},
                        "summary": f"[{ai_analysis.get('category', 'General')}] {subject}",
                        "description": f"From: {from_address}\nPriority: {ai_analysis.get('priority', 'Medium')}\n\nAI Summary: {ai_analysis.get('summary', '')}\n\nOriginal Email:\n{safe_body}",
                        "issuetype": {"name": issue_type_name},
                        "priority": {"name": ai_analysis.get('priority', 'Medium')}
                    }
                }

                jira_response = requests.post(jira_url, json=jira_payload, headers=headers)
                
                if not jira_response.ok:
                    print(f"Jira Error Response: {jira_response.text}")
                    print(f"Payload sent: {json.dumps(jira_payload)}")
                
                jira_response.raise_for_status()
                issue_key = jira_response.json().get('key')
                print(f"Jira ticket created successfully: {issue_key}")
                
                # 6. Call Jira REST API (Step 2: Upload Attachments)
                if issue_key and attachments:
                    print(f"Uploading {len(attachments)} attachments to Jira ticket {issue_key}...")
                    attachment_url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}/attachments"
                    
                    # Jira requires X-Atlassian-Token: no-check for attachment uploads
                    attach_headers = {
                        "Authorization": f"Basic {auth_encoded}",
                        "X-Atlassian-Token": "no-check"
                    }
                    
                    for filename, content_bytes, content_type in attachments:
                        # Multipart form-data encoding using requests_toolbelt
                        multipart_data = MultipartEncoder(
                            fields={
                                'file': (filename, content_bytes, content_type)
                            }
                        )
                        attach_headers['Content-Type'] = multipart_data.content_type
                        
                        attach_res = requests.post(attachment_url, data=multipart_data, headers=attach_headers)
                        
                        if attach_res.status_code == 200:
                            print(f"Successfully uploaded attachment: {filename}")
                        else:
                            print(f"Failed to upload attachment {filename}: {attach_res.status_code} - {attach_res.text}")
                            
                # 6.5 Send Auto-Responder Email via SES
                sender_email = os.environ.get('SENDER_EMAIL')
                if sender_email and from_address and from_address != 'Unknown Sender':
                    try:
                        print(f"Sending confirmation email to {from_address}...")
                        html_body = f"""
                        <html>
                        <head></head>
                        <body>
                          <h2>Support Ticket Created</h2>
                          <p>Hi there,</p>
                          <p>Thank you for reaching out! We have successfully received your request and our AI assistant has triaged it for our human team.</p>
                          <ul>
                              <li><strong>Ticket ID:</strong> {issue_key}</li>
                              <li><strong>Category:</strong> {ai_analysis.get('category', 'General')}</li>
                              <li><strong>Priority:</strong> {ai_analysis.get('priority', 'Medium')}</li>
                          </ul>
                          <p>Our team will look into this and get back to you shortly.</p>
                          <p>Best,<br>Support Team</p>
                        </body>
                        </html>
                        """
                        
                        ses_client.send_email(
                            Source=sender_email,
                            Destination={'ToAddresses': [from_address]},
                            Message={
                                'Subject': {'Data': f"Support Request Received: {issue_key}"},
                                'Body': {'Html': {'Data': html_body}}
                            }
                        )
                        print("Auto-responder email sent successfully.")
                    except Exception as email_err:
                        # Log but do NOT crash the lambda if auto-reply fails
                        print(f"Failed to send auto-responder email to {from_address}: {email_err}")
                        
            else:
                print("Jira credentials not provided in environment variables. Skipping Jira creation.")
                
            # 7. Data Lifecycle: Delete the email from S3 on success
            print(f"Deleting {object_key} from S3...")
            s3_client.delete_object(Bucket=bucket_name, Key=object_key)
            print("S3 deletion successful.")
            
        except Exception as e:
            print(f"Error processing record: {e}")
            # Raise the exception to fail the Lambda invocation so SQS retries
            raise e
            
    return {
        'statusCode': 200,
        'body': json.dumps('Success')
    }
