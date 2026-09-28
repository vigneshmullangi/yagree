from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.models import APIClient


class Command(BaseCommand):
    help = "Create an API client (API key + webhook secret) for an external system such as the HRMS."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Client name, e.g. HRMS")
        parser.add_argument("--owner", help="Username of the Yagree user who will own the documents it creates")
        parser.add_argument("--webhook-url", default="", help="URL Yagree sends events to (optional)")
        parser.add_argument("--rotate", action="store_true", help="Issue a new API key for an existing client")

    def handle(self, *args, **options):
        webhook_url = options["webhook_url"]
        if webhook_url and not webhook_url.startswith(("http://", "https://")):
            raise CommandError("--webhook-url must start with http:// or https://")

        if options["rotate"]:
            client = APIClient.objects.filter(name=options["name"]).first()
            if not client:
                raise CommandError(f"No API client named '{options['name']}'.")
            raw_key = APIClient.new_raw_key()
            client.key_prefix = raw_key[:12]
            client.key_hash = APIClient.hash_key(raw_key)
            if webhook_url:
                client.webhook_url = webhook_url
            client.save()
        else:
            if not options["owner"]:
                raise CommandError("--owner is required when creating a client.")
            owner = get_user_model().objects.filter(username=options["owner"]).first()
            if not owner:
                raise CommandError(f"No user with username '{options['owner']}'.")
            if APIClient.objects.filter(name=options["name"]).exists():
                raise CommandError(f"An API client named '{options['name']}' already exists (use --rotate for a new key).")
            client, raw_key = APIClient.generate(name=options["name"], owner=owner, webhook_url=webhook_url)

        self.stdout.write(self.style.SUCCESS(f"API client '{client.name}' ready."))
        self.stdout.write(f"  API key (shown once, store it safely): {raw_key}")
        self.stdout.write(f"  Webhook secret (for verifying Yagree's events): {client.webhook_secret}")
        self.stdout.write(f"  Webhook URL: {client.webhook_url or '(not set - set it in Django admin)'}")
