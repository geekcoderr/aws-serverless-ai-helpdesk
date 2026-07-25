# AWS Serverless AI Helpdesk

**AWS Serverless AI Helpdesk** is an automated, event-driven IT support pipeline built natively on AWS. It ingests inbound customer support emails via Amazon SES and S3, leverages Amazon Bedrock (AI) to intelligently categorize and prioritize the requests, and automatically provisions tickets with attachments in Jira.

Designed for zero-downtime, it features an automated Dead Letter Queue (DLQ) fallback system and automated SES customer response receipts, providing a seamless, highly-scalable, and serverless solution for modern DevOps and support teams.

## Architecture

1. **Email Ingestion:** Amazon SES receives an inbound email (e.g. `support@yourdomain.com`) and writes the raw `.eml` file to an Amazon S3 bucket.
2. **Event Trigger:** S3 sends an `ObjectCreated` event to an Amazon SQS Queue.
3. **Processing:** The main AWS Lambda function (`ProcessInboundEmail`) polls the queue. It downloads the email, extracts the text and attachments, and sends the text to **Amazon Bedrock (Nova Lite)** for AI classification (Priority, Category, Summary).
4. **Jira Sync:** The Lambda function authenticates with Jira via REST API, automatically detects valid issue types, creates the ticket, and uploads any attachments (images, PDFs) directly to the ticket.
5. **Auto-Responder:** The Lambda sends a beautifully formatted HTML confirmation email back to the customer via Amazon SES.
6. **Fallback (DLQ):** If any step fails 3 times, the SQS message is moved to a Dead Letter Queue. A secondary Lambda function (`EmailDLQProcessor`) alerts the admin and moves the corrupted email to a `failed/` directory in S3 for manual review.

---

## Configuration

Duplicate the `.env.example` file and rename it to `.env` (this file is ignored by Git). Fill in your specific variables to test locally or use as a reference for your AWS Lambda Environment Variables.

See the `.env.example` file for details on what each variable does.

## Security & IAM Setup

To deploy this securely using GitHub Actions, you must set up an OpenID Connect (OIDC) Identity Provider in your AWS Account. **Do not store AWS Access Keys in GitHub.**

### 1. Create the OIDC Provider
In the AWS IAM Console, create an Identity Provider:
- **Provider Type:** OpenID Connect
- **Provider URL:** `https://token.actions.githubusercontent.com`
- **Audience:** `sts.amazonaws.com`

### 2. Create the GitHub Actions Role
Create a new IAM Role. For the **Trust Relationship**, use the JSON template provided in `docs/iam_oidc_trust_policy.json`. Replace the dummy variables with your Account ID, GitHub Username, and Repository Name.

For the **Permissions**, attach a new inline policy using the JSON template provided in `docs/iam_github_actions_policy.json`. This grants GitHub Actions permission to push to ECR and update your specific Lambda functions.

---

## Deployment (CI/CD)

This repository includes a smart, custom-built GitHub Actions pipeline located at `.github/workflows/deploy.yml`.

### The "ACTION: Deployment" Trigger
To prevent unnecessary and costly deployments for minor documentation updates or formatting changes, **the CI/CD pipeline will ONLY deploy your code if your Git commit message contains the exact string:**

`ACTION: Deployment`

**Example Commit Command:**
```bash
git commit -m "Fixed null byte bug. ACTION: Deployment(build and push and deploy on aws lamnbdas)"
```

If you push code without this string in the commit message, the GitHub Actions runner will instantly skip the deployment phase, saving you CI/CD minutes and preventing unnecessary Lambda overwrites.

### CI/CD Workflow Steps
When triggered, the pipeline automatically:
1. Assumes the OIDC Role to get temporary AWS credentials.
2. Logs into Amazon ECR.
3. Builds the Docker container from the `Dockerfile`.
4. Tags and pushes the container to ECR.
5. Issues the `aws lambda update-function-code` command to immediately hot-swap your two Lambda functions with the latest code.
