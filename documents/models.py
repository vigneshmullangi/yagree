import uuid

from django.conf import settings
from django.db import models


def document_upload_path(instance, filename):
    return f"documents/{instance.id}/{filename}"


def signed_upload_path(instance, filename):
    return f"documents/{instance.id}/signed/{filename}"


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        PARTIALLY_SIGNED = "PARTIALLY_SIGNED", "Partially Signed"
        COMPLETED = "COMPLETED", "Completed"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=255)
    original_file = models.FileField(upload_to=document_upload_path)
    signed_file = models.FileField(upload_to=signed_upload_path, null=True, blank=True)

    signyu_document_id = models.CharField(max_length=255, blank=True, db_index=True)
    # Raw status string as returned by SignYu, kept alongside our mapped
    # `status` field so nothing is lost if SignYu uses values we haven't
    # mapped yet (see documents/services/status_map.py).
    signyu_raw_status = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # Full raw JSON responses from SignYu for create/send, kept for audit so
    # a successful API response is never silently lost (see Section 19 of spec).
    signyu_create_response = models.JSONField(null=True, blank=True)
    signyu_send_response = models.JSONField(null=True, blank=True)

    # CONFIRMED via GET /documents/{id}: completed documents carry a real
    # downloadUrl (and certificateUrl) - this replaces the earlier guess of
    # a separate /documents/{id}/download endpoint.
    signyu_download_url = models.URLField(max_length=1000, blank=True)
    signyu_certificate_url = models.URLField(max_length=1000, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def signer_count(self):
        return self.signers.count()

    @property
    def signed_count(self):
        return self.signers.filter(status=Signer.Status.SIGNED).count()


class Signer(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SIGNED = "SIGNED", "Signed"
        DECLINED = "DECLINED", "Declined"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="signers")
    signyu_signer_id = models.CharField(max_length=255, blank=True, db_index=True)

    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)

    signing_order = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    sign_url = models.URLField(max_length=1000, blank=True)

    signed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["signing_order", "created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}> - {self.document.name}"


class WebhookEvent(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="webhook_events", null=True, blank=True
    )
    event_type = models.CharField(max_length=100, blank=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)
    processing_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_type or 'unknown'} @ {self.received_at:%Y-%m-%d %H:%M}"


class ActivityLog(models.Model):
    """Simple audit trail shown on the document detail page (Section 28)."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="activities")
    message = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]