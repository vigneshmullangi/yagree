from django.contrib import admin

from .models import ActivityLog, Document, Signer, WebhookEvent


class SignerInline(admin.TabularInline):
    model = Signer
    extra = 0


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "signer_count", "signed_count", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "signyu_document_id")
    inlines = [SignerInline]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "document", "processed", "received_at")
    list_filter = ("processed", "event_type")


admin.site.register(ActivityLog)
