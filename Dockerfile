# Use the official AWS Lambda Python 3.12 base image (from Docker Hub mirror to avoid ECR 403 errors)
FROM amazon/aws-lambda-python:3.12

# Copy requirements.txt
COPY requirements.txt ./

# Install the Python dependencies
RUN pip install -r requirements.txt

# Copy all Lambda handler codes
COPY inbound_function.py ./
COPY dlq_function.py ./
COPY webhook_function.py ./

# Set the default CMD to the main handler.
# The DLQ Lambda will override this via the AWS Console configuration.
CMD ["inbound_function.lambda_handler"]
