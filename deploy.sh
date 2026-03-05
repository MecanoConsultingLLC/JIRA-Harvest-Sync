#!/usr/bin/env bash
#
# Deploy jira-harvest-sync Lambda to Mecano Consulting AWS account
#
# Usage: bash deploy.sh
#
set -euo pipefail

PROFILE="mecanoConsulting"
REGION="us-east-1"
ACCOUNT_ID="214070120103"
FUNCTION_NAME="jira-harvest-sync"
ROLE_NAME="jira-harvest-sync-role"
RULE_NAME="jira-harvest-sync-schedule"
SSM_PREFIX="/mecano/jira-harvest-sync"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

AWS="aws --profile $PROFILE --region $REGION"

echo "=== Jira-Harvest Sync Lambda Deployment ==="
echo "Account: $ACCOUNT_ID | Region: $REGION"
echo ""

# ---------------------------------------------------------------------------
# 1. Create SSM Parameters
# ---------------------------------------------------------------------------
echo ">>> Step 1: Creating SSM parameters..."

# Read credentials from local config files
JIRA_CONFIG="$HOME/mecanoConsulting/.claude/scripts/jira_config.json"
HARVEST_CONFIG="$HOME/mecanoConsulting/.claude/scripts/harvest_config.json"

JIRA_EMAIL=$(python3 -c "import json; print(json.load(open('$JIRA_CONFIG'))['JIRA_EMAIL'])")
JIRA_TOKEN=$(python3 -c "import json; print(json.load(open('$JIRA_CONFIG'))['JIRA_API_TOKEN'])")
HARVEST_ACCOUNT_ID=$(python3 -c "import json; print(json.load(open('$HARVEST_CONFIG'))['HARVEST_ACCOUNT_ID'])")
HARVEST_TOKEN=$(python3 -c "import json; print(json.load(open('$HARVEST_CONFIG'))['HARVEST_API_TOKEN'])")

$AWS ssm put-parameter \
    --name "$SSM_PREFIX/jira-email" \
    --type SecureString \
    --value "$JIRA_EMAIL" \
    --overwrite \
    --description "Jira email for API auth"

$AWS ssm put-parameter \
    --name "$SSM_PREFIX/jira-api-token" \
    --type SecureString \
    --value "$JIRA_TOKEN" \
    --overwrite \
    --description "Jira API token"

$AWS ssm put-parameter \
    --name "$SSM_PREFIX/harvest-account-id" \
    --type SecureString \
    --value "$HARVEST_ACCOUNT_ID" \
    --overwrite \
    --description "Harvest account ID"

$AWS ssm put-parameter \
    --name "$SSM_PREFIX/harvest-api-token" \
    --type SecureString \
    --value "$HARVEST_TOKEN" \
    --overwrite \
    --description "Harvest API token"

# Initialize last-alert-time to 0 (allow first alert immediately)
$AWS ssm put-parameter \
    --name "$SSM_PREFIX/last-alert-time" \
    --type String \
    --value "0" \
    --overwrite \
    --description "Timestamp of last missing-project alert email"

echo "    SSM parameters created."

# ---------------------------------------------------------------------------
# 2. Verify SES email identity
# ---------------------------------------------------------------------------
echo ">>> Step 2: Verifying SES email identity..."

$AWS ses verify-email-identity --email-address "samir@mecanoconsulting.com" 2>/dev/null || true
echo "    SES verification email sent (if not already verified)."

# ---------------------------------------------------------------------------
# 3. Create IAM Role + Inline Policy
# ---------------------------------------------------------------------------
echo ">>> Step 3: Creating IAM role..."

TRUST_POLICY='{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}'

# Create role (ignore error if exists)
$AWS iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "IAM role for jira-harvest-sync Lambda" 2>/dev/null || echo "    Role already exists, updating policy..."

INLINE_POLICY='{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:us-east-1:'"$ACCOUNT_ID"':*"
        },
        {
            "Sid": "SSMGetParameters",
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameter",
                "ssm:GetParameters"
            ],
            "Resource": "arn:aws:ssm:us-east-1:'"$ACCOUNT_ID"':parameter/mecano/jira-harvest-sync/*"
        },
        {
            "Sid": "SSMPutAlertTimestamp",
            "Effect": "Allow",
            "Action": "ssm:PutParameter",
            "Resource": "arn:aws:ssm:us-east-1:'"$ACCOUNT_ID"':parameter/mecano/jira-harvest-sync/last-alert-time"
        },
        {
            "Sid": "KMSDecrypt",
            "Effect": "Allow",
            "Action": "kms:Decrypt",
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "kms:ViaService": "ssm.us-east-1.amazonaws.com"
                }
            }
        },
        {
            "Sid": "SESsendEmail",
            "Effect": "Allow",
            "Action": "ses:SendEmail",
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "ses:FromAddress": "samir@mecanoconsulting.com"
                }
            }
        }
    ]
}'

$AWS iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "jira-harvest-sync-policy" \
    --policy-document "$INLINE_POLICY"

echo "    IAM role and policy configured."

# Wait for role to propagate
echo "    Waiting 10s for IAM role propagation..."
sleep 10

# ---------------------------------------------------------------------------
# 4. Zip and Create/Update Lambda Function
# ---------------------------------------------------------------------------
echo ">>> Step 4: Creating Lambda function..."

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Create zip
cd "$SCRIPT_DIR"
zip -j /tmp/jira-harvest-sync.zip lambda_function.py

# Try to create, fall back to update if exists
if $AWS lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --runtime python3.12 \
    --handler lambda_function.handler \
    --role "$ROLE_ARN" \
    --zip-file fileb:///tmp/jira-harvest-sync.zip \
    --timeout 300 \
    --memory-size 128 \
    --description "Syncs Jira issues to Harvest tasks every 5 minutes" 2>/dev/null; then
    echo "    Lambda function created."
else
    echo "    Lambda exists, updating code..."
    $AWS lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/jira-harvest-sync.zip
    echo "    Lambda function code updated."
fi

# Wait for function to be active
echo "    Waiting for function to become active..."
$AWS lambda wait function-active-v2 --function-name "$FUNCTION_NAME"

# ---------------------------------------------------------------------------
# 5. Create EventBridge Rule + Target + Lambda Permission
# ---------------------------------------------------------------------------
echo ">>> Step 5: Creating EventBridge schedule..."

$AWS events put-rule \
    --name "$RULE_NAME" \
    --schedule-expression "rate(5 minutes)" \
    --state ENABLED \
    --description "Trigger jira-harvest-sync Lambda every 5 minutes"

LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

$AWS events put-targets \
    --rule "$RULE_NAME" \
    --targets "Id=1,Arn=$LAMBDA_ARN"

# Add permission for EventBridge to invoke Lambda (ignore if exists)
$AWS lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --statement-id "eventbridge-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" 2>/dev/null || echo "    Permission already exists."

echo "    EventBridge schedule configured."

# ---------------------------------------------------------------------------
# 6. Test Invoke
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 6: Test invoke..."

$AWS lambda invoke \
    --function-name "$FUNCTION_NAME" \
    --payload '{}' \
    --cli-binary-format raw-in-base64-out \
    /dev/stdout

echo ""
echo ""
echo "=== Deployment complete ==="
echo "Function: $FUNCTION_NAME"
echo "Schedule: every 5 minutes via $RULE_NAME"
echo "Logs: https://$REGION.console.aws.amazon.com/cloudwatch/home?region=$REGION#logsV2:log-groups/log-group/\$252Faws\$252Flambda\$252F$FUNCTION_NAME"
