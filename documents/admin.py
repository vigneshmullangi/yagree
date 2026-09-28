from django.contrib import admin

from .models import ActivityLog, APIClient, Document, OutboundWebhookLog, Signer, WebhookEvent


class SignerInline(admin.TabularInline):
    model = Signer
    extra = 0


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "api_client", "external_ref", "status", "signer_count", "signed_count", "created_at")
    list_filter = ("status", "api_client")
    search_fields = ("name", "signyu_document_id", "external_ref")
    inlines = [SignerInline]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "document", "processed", "received_at")
    list_filter = ("processed", "event_type")


@admin.register(APIClient)
class APIClientAdmin(admin.ModelAdmin):
    """Clients are created with `manage.py create_api_client` so the key can be shown once."""

    list_display = ("name", "owner", "key_prefix", "is_active", "webhook_url", "last_used_at")
    readonly_fields = ("key_prefix", "last_used_at", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(OutboundWebhookLog)
class OutboundWebhookLogAdmin(admin.ModelAdmin):
    list_display = ("event", "api_client", "document", "success", "response_status", "attempts", "created_at")
    list_filter = ("success", "event", "api_client")
    readonly_fields = [f.name for f in OutboundWebhookLog._meta.fields]


admin.site.register(ActivityLog)
