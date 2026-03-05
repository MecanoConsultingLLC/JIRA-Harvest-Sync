# Jira-to-Harvest Task Sync

Automatically creates Harvest tasks from Jira issues so consultants can log time against them.

## How It Works

Every 5 minutes (via EventBridge), the Lambda:

1. Fetches all Jira projects and active Harvest projects
2. Queries Jira for issues created in the last 24 hours
3. For each issue, matches the Jira project to a Harvest project (by name, case-insensitive)
4. Creates a Harvest task named `{KEY}: {Summary}` and assigns it to the project
5. Skips issues that already have a matching task (idempotent)
6. Sends an email alert if any Jira projects have no matching Harvest project

## Manual Backfill

Invoke the Lambda with a `since` parameter to sync all issues created after a specific date:

```bash
aws lambda invoke \
    --function-name jira-harvest-sync \
    --payload '{"since": "2026-01-01"}' \
    --cli-binary-format raw-in-base64-out \
    --profile mecanoConsulting --region us-east-1 \
    /dev/stdout
```

When tasks are created during a manual run, an email summary is sent listing every task created.

## AWS Resources

| Resource | Name |
|----------|------|
| Lambda | `jira-harvest-sync` (Python 3.12, 128 MB, 300s timeout) |
| IAM Role | `jira-harvest-sync-role` |
| EventBridge Rule | `jira-harvest-sync-schedule` (`rate(5 minutes)`) |
| SSM Parameters | `/mecano/jira-harvest-sync/{jira-email,jira-api-token,harvest-account-id,harvest-api-token,last-alert-time}` |
| SES Identity | `samir@mecanoconsulting.com` |

Account: Mecano Consulting (214070120103), us-east-1.

## Deploying Changes

```bash
# Update code only
cd ~/mecanoConsulting/tools/jiraHarvestSync
zip -j /tmp/jira-harvest-sync.zip lambda_function.py
aws lambda update-function-code \
    --function-name jira-harvest-sync \
    --zip-file fileb:///tmp/jira-harvest-sync.zip \
    --profile mecanoConsulting --region us-east-1

# Full deploy (creates all resources if missing)
bash deploy.sh
```

## Project Matching

Jira project names are matched to Harvest project names case-insensitively. If no name match is found, it falls back to matching against the Harvest project's client name. Unmatched projects are reported via email.

## Edge Cases

- **Idempotent**: task existence is checked by issue key prefix before creating
- **Harvest name uniqueness**: task names are globally unique in Harvest (case-insensitive). If a name conflict occurs, the existing task is reused
- **Task name length**: truncated to 255 chars, preserving the ticket key prefix
- **Alert rate-limiting**: unmapped project emails are limited to once per 24 hours (scheduled runs only)
- **Partial failures**: errors on individual issues are logged but don't stop the sync
