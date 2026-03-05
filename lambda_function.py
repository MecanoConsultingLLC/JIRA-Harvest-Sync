"""
Jira-to-Harvest Task Sync Lambda

Every 5 minutes, checks for Jira issues created in the last 24 hours and creates
matching Harvest tasks. Sends an SES alert if a Jira project has no matching
Harvest project (rate-limited to once per 24h).

Uses stdlib only (urllib, json, base64) plus boto3 (available in Lambda runtime).
"""

import json
import logging
import time
import urllib.request
import urllib.error
from base64 import b64encode

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Global cache for SSM secrets (populated once per cold start)
# ---------------------------------------------------------------------------
_secrets = {}

SSM_PREFIX = "/mecano/jira-harvest-sync"
JIRA_BASE_URL = "https://mecano.atlassian.net"
HARVEST_BASE_URL = "https://api.harvestapp.com/v2"
ALERT_FROM_EMAIL = "samir@mecanoconsulting.com"
ALERT_TO_EMAIL = "samir@mecanoconsulting.com"
ALERT_COOLDOWN_SECONDS = 86400  # 24 hours


def get_secrets():
    """Fetch SSM parameters at cold start, cache globally."""
    global _secrets
    if _secrets:
        return _secrets

    ssm = boto3.client("ssm", region_name="us-east-1")
    param_names = [
        f"{SSM_PREFIX}/jira-email",
        f"{SSM_PREFIX}/jira-api-token",
        f"{SSM_PREFIX}/harvest-account-id",
        f"{SSM_PREFIX}/harvest-api-token",
    ]
    resp = ssm.get_parameters(Names=param_names, WithDecryption=True)
    for p in resp["Parameters"]:
        key = p["Name"].rsplit("/", 1)[-1]
        _secrets[key] = p["Value"]

    logger.info("Loaded %d SSM parameters", len(_secrets))
    return _secrets


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def jira_request(method, path, body=None):
    """Make an authenticated Jira REST API call."""
    secrets = get_secrets()
    creds = b64encode(
        f"{secrets['jira-email']}:{secrets['jira-api-token']}".encode()
    ).decode()

    url = f"{JIRA_BASE_URL}/rest/api/3{path}"
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error("Jira %s %s -> %d: %s", method, path, e.code, error_body)
        raise


def harvest_request(method, path, body=None):
    """Make an authenticated Harvest REST API call."""
    secrets = get_secrets()

    url = f"{HARVEST_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {secrets['harvest-api-token']}",
        "Harvest-Account-Id": secrets["harvest-account-id"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Jira-Harvest-Sync-Lambda",
    }

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("Harvest rate limit hit on %s %s", method, path)
            raise
        error_body = e.read().decode() if e.fp else ""
        logger.error("Harvest %s %s -> %d: %s", method, path, e.code, error_body)
        raise


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def get_jira_projects():
    """GET /rest/api/3/project -> {key: name}"""
    projects = jira_request("GET", "/project")
    return {p["key"]: p["name"] for p in projects}


def get_recent_jira_issues():
    """POST /rest/api/3/search/jql with created >= -1d, token-based pagination."""
    issues = []
    next_page_token = None

    while True:
        body = {
            "jql": "created >= -1d ORDER BY created ASC",
            "fields": ["summary", "project"],
            "maxResults": 100,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = jira_request("POST", "/search/jql", body)
        issues.extend(resp.get("issues", []))

        next_page_token = resp.get("nextPageToken")
        if not next_page_token:
            break

    logger.info("Fetched %d Jira issues created in last 24h", len(issues))
    return issues


# ---------------------------------------------------------------------------
# Harvest helpers
# ---------------------------------------------------------------------------

def get_harvest_projects():
    """GET /v2/projects?is_active=true -> {name_lower: {"id": ..., "client_name": ...}}"""
    projects = {}
    page = 1

    while True:
        resp = harvest_request("GET", f"/projects?is_active=true&page={page}&per_page=100")
        for p in resp.get("projects", []):
            client_name = p.get("client", {}).get("name", "") if p.get("client") else ""
            projects[p["name"].lower()] = {
                "id": p["id"],
                "name": p["name"],
                "client_name": client_name,
            }
        total_pages = resp.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    logger.info("Fetched %d active Harvest projects", len(projects))
    return projects


def find_harvest_project(jira_project_name, harvest_projects):
    """Case-insensitive match on project name, fallback to client name."""
    name_lower = jira_project_name.lower()

    # Direct name match
    if name_lower in harvest_projects:
        return harvest_projects[name_lower]

    # Fallback: check if any Harvest project's client name matches
    for hp in harvest_projects.values():
        if hp["client_name"].lower() == name_lower:
            return hp

    return None


def task_exists_in_project(project_id, issue_key):
    """Scan task assignments for a task starting with 'KEY:' prefix (case-insensitive)."""
    prefix_lower = f"{issue_key}:".lower()
    page = 1

    while True:
        resp = harvest_request(
            "GET",
            f"/projects/{project_id}/task_assignments?is_active=true&page={page}&per_page=100",
        )
        for ta in resp.get("task_assignments", []):
            task_name = ta.get("task", {}).get("name", "")
            if task_name.lower().startswith(prefix_lower):
                return True
        total_pages = resp.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    return False


def find_or_create_global_task(task_name):
    """Find an existing global task by name, or create a new one.

    Harvest requires globally unique task names. If a task with the same name
    already exists (active or archived), reuse it instead of creating a duplicate.
    """
    # Try to create — if 422 (name taken), search for the existing task
    try:
        task_resp = harvest_request("POST", "/tasks", {"name": task_name})
        logger.info("Created global task %d: %s", task_resp["id"], task_name)
        return task_resp["id"]
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
        logger.info("Task name already exists, searching: %s", task_name)

    # Search active and archived tasks (case-insensitive — Harvest enforces
    # unique names case-insensitively but stores the original casing)
    task_name_lower = task_name.lower()
    for is_active in ("true", "false"):
        page = 1
        while True:
            resp = harvest_request(
                "GET", f"/tasks?is_active={is_active}&page={page}&per_page=100"
            )
            for t in resp.get("tasks", []):
                if t["name"].lower() == task_name_lower:
                    logger.info(
                        "Found existing global task %d (active=%s): %s",
                        t["id"], is_active, t["name"],
                    )
                    return t["id"]
            total_pages = resp.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

    # Edge case: name conflict but couldn't find the task (shouldn't happen)
    raise RuntimeError(f"Task name conflict but could not find existing task: {task_name}")


def create_harvest_task(project_id, issue_key, summary):
    """Create a global Harvest task (or reuse existing), then assign to project.

    Task name format: '{KEY}: {Summary}' truncated to 255 chars.
    """
    task_name = f"{issue_key}: {summary}"
    if len(task_name) > 255:
        max_summary_len = 255 - len(issue_key) - 2  # ": " = 2 chars
        task_name = f"{issue_key}: {summary[:max_summary_len]}"

    task_id = find_or_create_global_task(task_name)

    # Assign task to project (ignore 422 if already assigned)
    try:
        harvest_request(
            "POST",
            f"/projects/{project_id}/task_assignments",
            {"task_id": task_id},
        )
        logger.info("Assigned task %d to project %d", task_id, project_id)
    except urllib.error.HTTPError as e:
        if e.code == 422:
            logger.info("Task %d already assigned to project %d", task_id, project_id)
        else:
            raise
    return task_id


# ---------------------------------------------------------------------------
# Alert helper
# ---------------------------------------------------------------------------

def send_missing_project_alert(missing_projects):
    """Send SES email listing Jira projects with no Harvest match.

    Rate-limited to once per 24 hours via SSM timestamp parameter.
    """
    ssm = boto3.client("ssm", region_name="us-east-1")
    param_name = f"{SSM_PREFIX}/last-alert-time"

    # Check last alert time
    try:
        resp = ssm.get_parameter(Name=param_name)
        last_alert = float(resp["Parameter"]["Value"])
        if time.time() - last_alert < ALERT_COOLDOWN_SECONDS:
            logger.info("Alert rate-limited, last sent %.0fs ago", time.time() - last_alert)
            return
    except ssm.exceptions.ParameterNotFound:
        pass

    # Build and send email
    project_list = "\n".join(f"  - {p}" for p in sorted(missing_projects))
    subject = f"Jira-Harvest Sync: {len(missing_projects)} unmapped project(s)"
    body = (
        "The following Jira projects have issues created in the last 24 hours "
        "but no matching Harvest project was found:\n\n"
        f"{project_list}\n\n"
        "Please create matching Harvest projects or verify the naming.\n\n"
        "This is an automated message from the jira-harvest-sync Lambda."
    )

    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_email(
        Source=ALERT_FROM_EMAIL,
        Destination={"ToAddresses": [ALERT_TO_EMAIL]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )
    logger.info("Sent missing-project alert for %d projects", len(missing_projects))

    # Update last alert timestamp
    ssm.put_parameter(Name=param_name, Value=str(time.time()), Overwrite=True)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """Orchestrate Jira→Harvest task sync."""
    logger.info("Starting Jira-Harvest sync")

    # Load secrets (cached after cold start)
    get_secrets()

    # Build maps
    jira_projects = get_jira_projects()
    logger.info("Found %d Jira projects", len(jira_projects))

    harvest_projects = get_harvest_projects()

    # Fetch recent issues
    issues = get_recent_jira_issues()

    created = 0
    skipped = 0
    errors = 0
    missing_projects = set()

    for issue in issues:
        issue_key = issue["key"]
        summary = issue["fields"]["summary"]
        project_key = issue_key.rsplit("-", 1)[0]
        jira_project_name = jira_projects.get(project_key, project_key)

        try:
            hp = find_harvest_project(jira_project_name, harvest_projects)
            if not hp:
                missing_projects.add(jira_project_name)
                continue

            project_id = hp["id"]

            if task_exists_in_project(project_id, issue_key):
                skipped += 1
                continue

            create_harvest_task(project_id, issue_key, summary)
            created += 1

        except Exception:
            logger.exception("Error processing %s", issue_key)
            errors += 1

    summary_msg = (
        f"Sync complete: {created} created, {skipped} skipped, "
        f"{errors} errors, {len(missing_projects)} unmapped projects"
    )
    logger.info(summary_msg)

    # Send alert if any projects are unmapped
    if missing_projects:
        try:
            send_missing_project_alert(missing_projects)
        except Exception:
            logger.exception("Failed to send missing-project alert")

    return {
        "statusCode": 200,
        "body": summary_msg,
    }
