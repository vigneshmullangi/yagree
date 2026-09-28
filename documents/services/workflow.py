"""
Document lifecycle against SignYu, shared by the web UI, the public API,
the scheduled sync command and the inbound SignYu webhook.
"""
import logging

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from documents.models import ActivityLog, Document, Signer

from .notify import notify
from .signyu import SignYuClient
from .status_map import map_document_status

logger = logging.getLogger(__name__)

# Yagree status -> event sent to the API client's webhook.
STATUS_EVENTS = {
    Document.Status.COMPLETED: "document.completed",
    Document.Status.DECLINED: "document.declined",
    Document.Status.EXPIRED: "document.expired",
    Document.Status.CANCELLED: "document.cancelled",
}
TERMINAL_NEGATIVE = (Document.Status.DECLINED, Document.Status.EXPIRED, Document.Status.CANCELLED)


def _log(document, message):
    ActivityLog.objects.create(document=document, message=message)


def _parse_timestamp(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _by_email(signers_payload):
    return {
        s["email"].lower(): s
        for s in signers_payload or []
        if isinstance(s, dict) and s.get("email")
    }


def send_document_to_signyu(document, client=None):
    """
    Send an already-created document (with its signers added) for signing and
    record SignYu's response, including each signer's ID and signing link.
    Raises SignYuAPIError if SignYu refuses.
    """
    client = client or SignYuClient()
    raw = client.send_document(document.signyu_document_id)
    payload = client.extract_document_payload(raw)

    document.signyu_send_response = raw
    document.signyu_raw_status = payload.get("status", document.signyu_raw_status)
    document.status = map_document_status(payload.get("status", "")) or Document.Status.SENT
    document.sent_at = timezone.now()
    document.save()

    returned = _by_email(payload.get("signers"))
    for signer in document.signers.all():
        match = returned.get(signer.email.lower())
        if match:
            signer.signyu_signer_id = match.get("signerId", signer.signyu_signer_id)
            signer.sign_url = match.get("signUrl", signer.sign_url)
            signer.save(update_fields=["signyu_signer_id", "sign_url"])

    _log(document, "Document sent for signing")
    notify(document, "document.sent")
    return document


def sync_document(document, client=None, source="checked manually"):
    """
    Pull the current state from SignYu and update Yagree to match. Events are
    only produced on real changes (a signer newly signed, the status moved),
    so calling this repeatedly, or from several places at once, never sends
    duplicate notifications. Returns (document, changed). Raises SignYuAPIError.
    """
    client = client or SignYuClient()
    raw = client.get_document(document.signyu_document_id)
    payload = client.extract_document_payload(raw)
    returned = _by_email(payload.get("signers"))

    newly_signed = []
    document_event = None
    changed = False

    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document.pk)

        for signer in document.signers.all():
            match = returned.get(signer.email.lower())
            if not match:
                continue
            fields = []
            if match.get("signerId") and not signer.signyu_signer_id:
                signer.signyu_signer_id = match["signerId"]
                fields.append("signyu_signer_id")
            if match.get("signUrl") and not signer.sign_url:
                signer.sign_url = match["signUrl"]
                fields.append("sign_url")
            if match.get("hasSigned") and signer.status != Signer.Status.SIGNED:
                signer.status = Signer.Status.SIGNED
                signer.signed_at = _parse_timestamp(match.get("signedAt")) or timezone.now()
                fields += ["status", "signed_at"]
                newly_signed.append(signer)
                _log(document, f"{signer.name} signed ({source})")
                changed = True
            if fields:
                signer.save(update_fields=fields)

        total = document.signers.count()
        signed = document.signers.filter(status=Signer.Status.SIGNED).count()
        raw_status = payload.get("status", "")
        mapped = map_document_status(raw_status)

        if mapped in TERMINAL_NEGATIVE:
            new_status = mapped
        elif mapped == Document.Status.COMPLETED or (total > 0 and signed == total):
            # SignYu kept saying "SENT" in testing even when everyone had
            # signed, so completion is also derived from the signers.
            new_status = Document.Status.COMPLETED
        elif total > 0 and signed > 0:
            new_status = Document.Status.PARTIALLY_SIGNED
        else:
            new_status = mapped or document.status

        document.signyu_download_url = payload.get("downloadUrl") or document.signyu_download_url
        document.signyu_certificate_url = payload.get("certificateUrl") or document.signyu_certificate_url

        if new_status != document.status:
            document.status = new_status
            document.signyu_raw_status = raw_status
            if new_status == Document.Status.SENT and not document.sent_at:
                document.sent_at = timezone.now()
            if new_status == Document.Status.COMPLETED and not document.completed_at:
                document.completed_at = _parse_timestamp(payload.get("completedAt")) or timezone.now()
            document_event = STATUS_EVENTS.get(new_status)
            _log(document, f"Status changed to {document.get_status_display()} ({source})")
            changed = True

        document.save()

    for signer in newly_signed:
        notify(document, "signer.signed", signer)
    if document_event:
        notify(document, document_event)
    return document, changed


def fetch_signed_pdf(document, client=None):
    """Return the signed PDF (a FieldFile), downloading and storing it on first use."""
    if document.signed_file:
        return document.signed_file

    client = client or SignYuClient()
    if not document.signyu_download_url:
        document, _ = sync_document(document, client, source="download")
    if document.signyu_download_url:
        content = client.download_from_url(document.signyu_download_url)
    else:
        content = client.download_document(document.signyu_document_id)

    document.signed_file.save(f"{document.name}-signed.pdf", ContentFile(content), save=True)
    return document.signed_file
