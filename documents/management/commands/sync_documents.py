import logging

from django.core.management.base import BaseCommand

from documents.models import Document
from documents.services.notify import retry_failed
from documents.services.signyu import SignYuAPIError, SignYuClient
from documents.services.workflow import sync_document


class Command(BaseCommand):
    help = (
        "Reconcile documents that are still out for signing with SignYu, then retry failed "
        "webhook deliveries to API clients. Run it every few minutes (e.g. a Render cron job) "
        "so a missed SignYu webhook never leaves a document stuck."
    )

    def handle(self, *args, **options):
        logger = logging.getLogger(__name__)
        client = SignYuClient()
        pending = Document.objects.filter(
            status__in=[Document.Status.SENT, Document.Status.PARTIALLY_SIGNED]
        ).exclude(signyu_document_id="")

        checked = changed = failed = 0
        for document in pending:
            checked += 1
            try:
                _, did_change = sync_document(document, client, source="scheduled sync")
                changed += int(did_change)
            except SignYuAPIError:
                failed += 1
                logger.exception("Scheduled sync failed for %s", document.id)

        tried, delivered = retry_failed()
        self.stdout.write(
            f"Documents checked: {checked}, updated: {changed}, failed: {failed}. "
            f"Webhook retries: {tried}, delivered: {delivered}."
        )
