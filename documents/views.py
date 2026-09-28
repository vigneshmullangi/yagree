import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import CreateDocumentForm, SignerFormSet
from .models import ActivityLog, Document, Signer, WebhookEvent
from .services.signyu import SignYuAPIError, SignYuClient, SignYuConflictError
from .services.status_map import map_document_status
from .services.workflow import fetch_signed_pdf, send_document_to_signyu, sync_document

logger = logging.getLogger(__name__)


def _log(document, message):
    ActivityLog.objects.create(document=document, message=message)


def _friendly_error(exc: SignYuAPIError) -> str:
    if isinstance(exc, SignYuConflictError):
        return str(exc) or "This action can't be completed in the document's current state."
    if exc.status_code == 401 or exc.status_code == 403:
        return "Yagree could not authenticate with SignYu. Please contact support."
    if exc.status_code == 404:
        return "The document could not be found on SignYu."
    if exc.status_code == 429:
        return "Too many requests right now. Please try again shortly."
    return "Something went wrong talking to SignYu. Please try again."


@login_required
def dashboard(request):
    docs = Document.objects.filter(owner=request.user)
    stats = {
        "total": docs.count(),
        "draft": docs.filter(status=Document.Status.DRAFT).count(),
        "sent": docs.filter(status=Document.Status.SENT).count(),
        "in_progress": docs.filter(status=Document.Status.PARTIALLY_SIGNED).count(),
        "completed": docs.filter(status=Document.Status.COMPLETED).count(),
    }
    recent = docs[:10]
    return render(request, "documents/dashboard.html", {"stats": stats, "documents": recent})


@login_required
def create_document(request):
    if request.method == "POST":
        form = CreateDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            document = form.save(commit=False)
            document.owner = request.user
            document.save()  # save first so original_file has a path/id

            client = SignYuClient()
            try:
                raw = client.create_document(document.name, document.original_file)
            except SignYuAPIError as exc:
                logger.exception("SignYu create_document failed for %s", document.id)
                document.delete()
                messages.error(request, _friendly_error(exc))
                return render(request, "documents/create_document.html", {"form": form})

            payload = client.extract_document_payload(raw)
            document.signyu_document_id = payload.get("documentId", "")
            document.signyu_raw_status = payload.get("status", "")
            document.signyu_create_response = raw
            mapped = map_document_status(payload.get("status", ""))
            if mapped:
                document.status = mapped
            document.save()
            _log(document, "Document created")
            messages.success(request, "Document created. Now add signers.")
            return redirect("documents:add_signers", pk=document.pk)
    else:
        form = CreateDocumentForm()
    return render(request, "documents/create_document.html", {"form": form})


@login_required
def add_signers(request, pk):
    document = get_object_or_404(Document, pk=pk, owner=request.user)

    if document.status != Document.Status.DRAFT:
        messages.error(request, "Signers can only be added before the document is sent.")
        return redirect("documents:document_detail", pk=document.pk)

    queryset = document.signers.all()

    if request.method == "POST":
        formset = SignerFormSet(request.POST, queryset=queryset)
        action = request.POST.get("action", "continue")
        if formset.is_valid():
            instances = formset.save(commit=False)
            for obj in formset.deleted_objects:
                obj.delete()
            for i, instance in enumerate(instances, start=1):
                instance.document = document
                if not instance.signing_order:
                    instance.signing_order = i
                instance.save()

            if action == "add_row":
                # Just persist locally and re-render with a fresh empty row -
                # no SignYu call yet, that only happens once below.
                formset = SignerFormSet(queryset=document.signers.all())
                return render(request, "documents/add_signers.html", {"formset": formset, "document": document})

            signers = list(document.signers.all())
            if not signers:
                messages.error(request, "Add at least one signer.")
                return render(request, "documents/add_signers.html", {"formset": formset, "document": document})
            if len(signers) > 6:
                messages.error(request, "A document can have a maximum of 6 signers.")
                return render(request, "documents/add_signers.html", {"formset": formset, "document": document})

            client = SignYuClient()
            try:
                raw = client.add_signers(
                    document.signyu_document_id,
                    [{"name": s.name, "email": s.email, "phone": s.phone} for s in signers],
                )
            except SignYuAPIError as exc:
                logger.exception("SignYu add_signers failed for %s", document.id)
                messages.error(request, _friendly_error(exc))
                formset = SignerFormSet(queryset=document.signers.all())
                return render(request, "documents/add_signers.html", {"formset": formset, "document": document})

            # Defensive match-back of SignYu signer IDs by email, since the
            # exact successful response shape wasn't confirmed (see signyu.py).
            returned = client.extract_signers_payload(raw)
            by_email = {r.get("email"): r for r in returned if isinstance(r, dict) and r.get("email")}
            for s in signers:
                match = by_email.get(s.email)
                if match:
                    s.signyu_signer_id = match.get("signerId") or match.get("id", "")
                    s.sign_url = match.get("signUrl") or match.get("sign_url", "")
                    s.save(update_fields=["signyu_signer_id", "sign_url"])

            _log(document, f"{len(signers)} signer(s) added")
            messages.success(request, "Signers saved.")
            return redirect("documents:review_send", pk=document.pk)
    else:
        formset = SignerFormSet(queryset=queryset)

    return render(request, "documents/add_signers.html", {"formset": formset, "document": document})


@login_required
def review_send(request, pk):
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    signers = document.signers.all()

    if not signers.exists():
        messages.error(request, "Add at least one signer before sending.")
        return redirect("documents:add_signers", pk=document.pk)

    if request.method == "POST":
        if document.status != Document.Status.DRAFT:
            messages.error(request, "This document has already been sent.")
            return redirect("documents:document_detail", pk=document.pk)

        try:
            send_document_to_signyu(document)
        except SignYuAPIError as exc:
            logger.exception("SignYu send_document failed for %s", document.id)
            messages.error(request, _friendly_error(exc))
            return render(request, "documents/review_send.html", {"document": document, "signers": signers})

        messages.success(request, "Document sent for signing.")
        return redirect("documents:document_detail", pk=document.pk)

    return render(request, "documents/review_send.html", {"document": document, "signers": signers})


@login_required
def sign_embedded(request, pk, signer_id):
    """
    Attempts to load a signer's SignYu signing page inside an iframe on a
    Yagree-branded page, instead of sending them straight to SignYu.

    NOT CONFIRMED to work: SignYu has no documented embed/iframe support
    (see documents/services/signyu.py notes) - their signing page may set
    X-Frame-Options or a frame-ancestors CSP that silently blocks framing,
    and the Aadhaar OTP step goes through UIDAI, which commonly refuses to
    run inside a frame at all as a security measure. If the iframe below
    renders blank, that's what's happening - use the fallback link instead.
    """
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    signer = get_object_or_404(Signer, pk=signer_id, document=document)

    if not signer.sign_url:
        messages.error(request, "No signing link is available for this signer yet.")
        return redirect("documents:document_detail", pk=document.pk)

    return render(
        request,
        "documents/sign_embedded.html",
        {"document": document, "signer": signer},
    )


@login_required
def document_detail(request, pk):
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    return render(
        request,
        "documents/document_detail.html",
        {
            "document": document,
            "signers": document.signers.all(),
            "activities": document.activities.all(),
        },
    )


@login_required
def download_document(request, pk):
    document = get_object_or_404(Document, pk=pk, owner=request.user)
    if document.status != Document.Status.COMPLETED:
        messages.error(request, "This document is not completed yet.")
        return redirect("documents:document_detail", pk=document.pk)

    try:
        signed_file = fetch_signed_pdf(document)
    except SignYuAPIError as exc:
        logger.exception("SignYu download failed for %s", document.id)
        messages.error(request, _friendly_error(exc))
        return redirect("documents:document_detail", pk=document.pk)

    return FileResponse(signed_file.open("rb"), as_attachment=True, filename=f"{document.name}-signed.pdf")


@login_required
@require_POST
def sync_status(request, pk):
    """
    Pulls the current status from SignYu on demand. Yagree calls out to
    SignYu, so this works even where SignYu can't reach Yagree.
    """
    document = get_object_or_404(Document, pk=pk, owner=request.user)

    if not document.signyu_document_id:
        messages.error(request, "This document has no SignYu ID to check.")
        return redirect("documents:document_detail", pk=document.pk)

    try:
        document, changed = sync_document(document)
    except SignYuAPIError as exc:
        logger.exception("SignYu get_document failed for %s", document.id)
        messages.error(request, _friendly_error(exc))
        return redirect("documents:document_detail", pk=document.pk)

    messages.success(request, "Status updated from SignYu." if changed else "Checked SignYu - no changes yet.")
    return redirect("documents:document_detail", pk=document.pk)


@csrf_exempt
@require_POST
def signyu_webhook(request):
    """
    Inbound webhook from SignYu. The payload shape is NOT confirmed, so it is
    stored as-is (WebhookEvent) and only used to find the document; the real
    state is then read from SignYu's GET /documents/{id}, whose shape is
    confirmed. If that call fails, the scheduled `sync_documents` command
    reconciles the document later.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    if settings_webhook_secret_mismatch(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    event_type = payload.get("eventType") or payload.get("event_type") or payload.get("event") or ""
    signyu_document_id = payload.get("documentId") or payload.get("document_id") or payload.get("documentID") or ""

    event = WebhookEvent.objects.create(event_type=event_type, payload=payload)

    document = Document.objects.filter(signyu_document_id=signyu_document_id).first() if signyu_document_id else None
    if not document:
        event.processing_note = "No matching document found for this event."
        event.save(update_fields=["processing_note"])
        return JsonResponse({"status": "received", "matched": False})

    event.document = document
    try:
        sync_document(document, source="via SignYu webhook")
        event.processed = True
    except SignYuAPIError:
        logger.exception("Sync after SignYu webhook failed for %s", document.id)
        event.processing_note = "Could not read the document state from SignYu; the scheduled sync will retry."
    event.save(update_fields=["processed", "document", "processing_note"])
    return JsonResponse({"status": "received", "matched": True})


def settings_webhook_secret_mismatch(request) -> bool:
    secret = settings.SIGNYU_WEBHOOK_SECRET
    if not secret:
        return False  # no secret configured - accepted (SignYu's HMAC header is not confirmed yet)
    provided = request.headers.get("X-Signyu-Secret", "")
    return provided != secret
