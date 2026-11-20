"""SNS -> Microsoft Teams incoming webhook.

Handles three message shapes arriving on the alerts topic:
  * CloudWatch alarm JSON (AlarmName / NewStateValue / NewStateReason)
  * Neonbee internal alerts JSON published by other Lambdas ({"source": ..., "title": ..., "text": ...})
  * Plain text (AWS Budgets notifications and anything else)

The webhook URL lives in Secrets Manager (TEAMS_WEBHOOK_SECRET_ARN) and is cached
per container. Stdlib + boto3 only (boto3 ships with the Lambda runtime).
"""
import json
import os
import urllib.request

_cache = {}

STATE_COLOURS = {"ALARM": "attention", "OK": "good", "INSUFFICIENT_DATA": "warning"}


def _default_get_secret(secret_id):
    import boto3  # lazy: unit tests inject a fake instead

    return boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]


def _default_post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec - URL comes from our own secret
        return resp.status


def webhook_url(get_secret):
    if "url" not in _cache:
        value = (get_secret(os.environ["TEAMS_WEBHOOK_SECRET_ARN"]) or "").strip()
        if not value.startswith("https://") or value == "REPLACE_ME":
            raise RuntimeError("Teams webhook secret is not set to an https URL")
        _cache["url"] = value
    return _cache["url"]


def format_message(subject, message):
    """Return (title, text, severity) for one SNS message."""
    env = os.environ.get("ENV_NAME", "nb-prod")
    try:
        body = json.loads(message)
    except (TypeError, ValueError):
        body = None

    if isinstance(body, dict) and "AlarmName" in body:
        state = body.get("NewStateValue", "UNKNOWN")
        title = "[{}] {} is {}".format(env, body["AlarmName"], state)
        text = body.get("NewStateReason") or body.get("AlarmDescription") or ""
        return title, text, STATE_COLOURS.get(state, "warning")

    if isinstance(body, dict) and "title" in body:
        return (
            "[{}] {}".format(env, body["title"]),
            body.get("text", ""),
            body.get("severity", "attention"),
        )

    title = "[{}] {}".format(env, subject or "AWS notification")
    return title, message or "", "warning"


def build_card(title, text, severity):
    """Adaptive Card wrapped the way Teams incoming webhooks / Workflows expect."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder",
                         "size": "Medium", "color": severity, "wrap": True},
                        {"type": "TextBlock", "text": text[:3500], "wrap": True},
                    ],
                },
            }
        ],
    }


def handler(event, context, get_secret=None, post=None):
    get_secret = get_secret or _default_get_secret
    post = post or _default_post
    url = webhook_url(get_secret)
    sent = 0
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        title, text, severity = format_message(sns.get("Subject"), sns.get("Message"))
        status = post(url, build_card(title, text, severity))
        if status is not None and int(status) >= 300:
            raise RuntimeError("Teams webhook returned HTTP {}".format(status))
        sent += 1
    return {"sent": sent}
