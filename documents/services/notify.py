"""
Outbound webhooks: Yagree -> the API client's system (e.g. the HRMS).

Every delivery is logged in OutboundWebhookLog (request payload, response
status/body, attempts) so both sides can compare what was sent and received.

Signature: HMAC-SHA256 over  "<timestamp>." + <raw request body>  using the
client's webhook_secret, sent as `X-Yagree-Signature: sha256=<hex>` along
with `X-Yagree-Timestamp`. Receivers should recompute it over the raw body
and reject requests with a bad signature or an old timestamp.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

import requests
from django.utils import timezone

from documents.models import OutboundWebhookLog

from .serializers import serialize_document, serialize_signer

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 8


def sign_body(secret, timestamp, body):
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _deliver(log):
    client = log.api_client
    body = json.dumps(log.payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Yagree-Webhooks/1.0",
        "X-Yagree-Event": log.event,
        "X-Yagree-Delivery": str(log.event_id),
        "X-Yagree-Timestamp": timestamp,
        "X-Yagree-Signature": "sha256=" + sign_body(client.webhook_secret, timestamp, body),
    }

    log.attempts += 1
    log.last_attempt_at = timezone.now()
    try:
        response = requests.post(
            client.webhook_url, data=body, headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False,
        )
        log.response_status = response.status_code
        log.response_body = response.text[:2000]
        log.success = 200 <= response.status_code < 300
        log.error = "" if log.success else f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        log.response_status = None
        log.response_body = ""
        log.success = False
        log.error = str(exc)[:255]
        logger.warning("Webhook delivery to %s failed: %s", client.name, exc)
    log.save()
    return log.success


def notify(document, event, signer=None):
    """Send `event` for `document` to its API client, if it has one with a webhook URL."""
    client = document.api_client
    if not client or not client.is_active or not client.webhook_url:
        return None

    data = {"document": serialize_document(document)}
    if signer is not None:
        data["signer"] = serialize_signer(signer)

    log = OutboundWebhookLog(api_client=client, document=document, event=event)
    log.payload = {
        "event": event,
        "event_id": str(log.event_id),
        "created_at": timezone.now().isoformat(),
        "data": data,
    }
    log.save()
    _deliver(log)
    return log


def retry_failed(max_age_hours=24):
    """Re-send deliveries that failed, up to MAX_ATTEMPTS. Returns (tried, succeeded)."""
    cutoff = timezone.now() - timedelta(hours=max_age_hours)
    pending = (
        OutboundWebhookLog.objects.filter(
            success=False, attempts__lt=MAX_ATTEMPTS, created_at__gte=cutoff, api_client__is_active=True
        )
        .exclude(api_client__webhook_url="")
        .select_related("api_client")
    )
    tried = succeeded = 0
    for log in pending:
        tried += 1
        if _deliver(log):
            succeeded += 1
    return tried, succeeded
