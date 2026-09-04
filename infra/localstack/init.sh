#!/bin/bash
# LocalStack init hook: create the S3 bucket, DynamoDB table, and SQS queue the
# app expects. Runs automatically when LocalStack reports ready (ready.d).
set -euo pipefail

REGION=us-west-2
BUCKET=survey-art-storage
TABLE=survey-art-jobs
QUEUE=survey-art-jobs

awslocal s3 mb "s3://${BUCKET}" --region "${REGION}" || true

awslocal dynamodb create-table \
  --table-name "${TABLE}" \
  --attribute-definitions AttributeName=jobId,AttributeType=S \
  --key-schema AttributeName=jobId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "${REGION}" || true

awslocal sqs create-queue --queue-name "${QUEUE}" --region "${REGION}" || true

echo "LocalStack init complete: bucket=${BUCKET} table=${TABLE} queue=${QUEUE}"
