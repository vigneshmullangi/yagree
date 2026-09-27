import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import CreateDocumentForm, SignerFormSet
from .models import ActivityLog, Document, Signer, WebhookEvent
from .services.signyu import SignYuAPIError, SignYuClient, SignYuConflictError
from .services.status_map import map_document_status, map_signer_status

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

        client = SignYuClient()
        try:
            raw = client.send_document(document.signyu_document_id)
        except SignYuAPIError as exc:
            logger.exception("SignYu send_document failed for %s", document.id)
            messages.error(request, _friendly_error(exc))
            return render(request, "documents/review_send.html", {"document": document, "signers": signers})

        payload = client.extract_document_payload(raw)
        document.signyu_send_response = raw
        document.signyu_raw_status = payload.get("status", document.signyu_raw_status)
        document.status = map_document_status(payload.get("status", "")) or Document.Status.SENT
        document.sent_at = timezone.now()
        document.save()

        # CONFIRMED: the send response includes each signer's signUrl and
        # signerId directly (not the add-signers response) - capture them
        # here, matched back to our local rows by email.
        returned_signers = payload.get("signers", [])
        by_email = {s.get("email"): s for s in returned_signers if isinstance(s, dict) and s.get("email")}
        for signer in signers:
            match = by_email.get(signer.email)
            if match:
                signer.signyu_signer_id = match.get("signerId", signer.signyu_signer_id)
                signer.sign_url = match.get("signUrl", signer.sign_url)
                signer.save(update_fields=["signyu_signer_id", "sign_url"])

        _log(document, "Document sent for signing")
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

    if document.signed_file:
        return FileResponse(document.signed_file.open("rb"), as_attachment=True, filename=f"{document.name}-signed.pdf")

    client = SignYuClient()
    from django.core.files.base import ContentFile

    try:
        if document.signyu_download_url:
            # CONFIRMED: real downloadUrl from GET /documents/{id}.
            content = client.download_from_url(document.signyu_download_url)
        else:
            # Fallback to the earlier guessed endpoint if we somehow never
            # captured a downloadUrl (e.g. completed via an old webhook
            # payload that didn't include it).
            content = client.download_document(document.signyu_document_id)
    except SignYuAPIError as exc:
        logger.exception("SignYu download failed for %s", document.id)
        messages.error(request, _friendly_error(exc))
        return redirect("documents:document_detail", pk=document.pk)

    document.signed_file.save(f"{document.name}-signed.pdf", ContentFile(content), save=True)
    return FileResponse(document.signed_file.open("rb"), as_attachment=True, filename=f"{document.name}-signed.pdf")


@login_required
@require_POST
def sync_status(request, pk):
    """
    Polls SignYu directly for the current status instead of waiting on a
    webhook - useful for local development where SignYu has no way to
    reach 127.0.0.1. No public URL required, since this is Yagree calling
    out to SignYu, not the other way around.

    CONFIRMED response shape from GET /documents/{id}:
        {documentId, name, status, createdAt, updatedAt, completedAt,
         downloadUrl, certificateUrl,
         signers: [{signerId, name, email, phone, signingOrder,
                     openedAt, signedAt, hasSigned, signUrl}]}
    Note: there is no per-signer status string - only a `hasSigned`
    boolean and `signedAt` timestamp. And the document-level `status`
    only appears to say "SENT" even when some signers have already
    signed - SignYu does not send an explicit "partially signed" value,
    so Yagree derives PARTIALLY_SIGNED itself below.
    """
    document = get_object_or_404(Document, pk=pk, owner=request.user)

    if not document.signyu_document_id:
        messages.error(request, "This document has no SignYu ID to check.")
        return redirect("documents:document_detail", pk=document.pk)

    client = SignYuClient()
    try:
        raw = client.get_document(document.signyu_document_id)
    except SignYuAPIError as exc:
        logger.exception("SignYu get_document failed for %s", document.id)
        messages.error(request, _friendly_error(exc))
        return redirect("documents:document_detail", pk=document.pk)

    payload = client.extract_document_payload(raw)

    returned_signers = payload.get("signers", [])
    by_email = {s.get("email"): s for s in returned_signers if isinstance(s, dict) and s.get("email")}
    changed = False

    for signer in document.signers.all():
        match = by_email.get(signer.email)
        if not match:
            continue
        if match.get("signerId") and not signer.signyu_signer_id:
            signer.signyu_signer_id = match["signerId"]
            signer.save(update_fields=["signyu_signer_id"])
        if match.get("signUrl") and not signer.sign_url:
            signer.sign_url = match["signUrl"]
            signer.save(update_fields=["sign_url"])
        if match.get("hasSigned") and signer.status != Signer.Status.SIGNED:
            signer.status = Signer.Status.SIGNED
            signer.signed_at = _parse_timestamp(match.get("signedAt")) or timezone.now()
            signer.save()
            _log(document, f"{signer.name} signed (checked manually)")
            changed = True

    document.signyu_download_url = payload.get("downloadUrl") or document.signyu_download_url
    document.signyu_certificate_url = payload.get("certificateUrl") or document.signyu_certificate_url

    signed_count = document.signers.filter(status=Signer.Status.SIGNED).count()
    total = document.signers.count()

    raw_status = payload.get("status", "")
    mapped_doc_status = map_document_status(raw_status)

    if mapped_doc_status == Document.Status.COMPLETED:
        new_status = Document.Status.COMPLETED
    elif total > 0 and signed_count == total:
        # SignYu's own status string stayed "SENT" in testing even once
        # every signer had signed - derive COMPLETED ourselves as a
        # fallback in case a webhook/explicit status never arrives.
        new_status = Document.Status.COMPLETED
    elif total > 0 and signed_count > 0:
        new_status = Document.Status.PARTIALLY_SIGNED
    else:
        new_status = mapped_doc_status or document.status

    if new_status != document.status:
        document.status = new_status
        document.signyu_raw_status = raw_status
        if new_status == Document.Status.COMPLETED and not document.completed_at:
            document.completed_at = _parse_timestamp(payload.get("completedAt")) or timezone.now()
            _log(document, "Document completed (checked manually)")
        changed = True

    document.save()

    if changed:
        messages.success(request, "Status updated from SignYu.")
    else:
        messages.success(request, "Checked SignYu - no changes yet.")
    return redirect("documents:document_detail", pk=document.pk)


def _parse_timestamp(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


@csrf_exempt
@require_POST
def signyu_webhook(request):
    """
    Receives SignYu webhook events. The exact payload shape is NOT
    confirmed - this handler stores the raw payload unconditionally
    (so nothing is ever lost), then makes a best-effort attempt to
    locate the document/signer and update statuses using several
    plausible key names. Once the real payload is known, tighten the
    key lookups below.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    if settings_webhook_secret_mismatch(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    event_type = payload.get("eventType") or payload.get("event_type") or payload.get("event") or ""
    signyu_document_id = (
        payload.get("documentId") or payload.get("document_id") or payload.get("documentID") or ""
    )

    event = WebhookEvent.objects.create(event_type=event_type, payload=payload)

    document = None
    if signyu_document_id:
        document = Document.objects.filter(signyu_document_id=signyu_document_id).first()

    if not document:
        event.processing_note = "No matching document found for this event."
        event.save(update_fields=["processing_note"])
        return JsonResponse({"status": "received", "matched": False})

    event.document = document

    signyu_signer_id = payload.get("signerId") or payload.get("signer_id") or ""
    signer_status_raw = payload.get("signerStatus") or payload.get("status") or ""
    doc_status_raw = payload.get("documentStatus") or payload.get("status") or ""

    if signyu_signer_id:
        signer = document.signers.filter(signyu_signer_id=signyu_signer_id).first()
        mapped_signer_status = map_signer_status(signer_status_raw)
        if signer and mapped_signer_status:
            signer.status = mapped_signer_status
            if mapped_signer_status == Signer.Status.SIGNED and not signer.signed_at:
                signer.signed_at = timezone.now()
            signer.save()
            _log(document, f"{signer.name} status updated to {mapped_signer_status}")

    mapped_doc_status = map_document_status(doc_status_raw)
    if mapped_doc_status:
        document.status = mapped_doc_status
        document.signyu_raw_status = doc_status_raw
        if mapped_doc_status == Document.Status.COMPLETED and not document.completed_at:
            document.completed_at = timezone.now()
            _log(document, "Document completed")
        document.save()
    elif document.signers.exists() and document.signers.filter(status=Signer.Status.SIGNED).count() == document.signers.count():
        # Fallback: if every signer is SIGNED but SignYu didn't send an
        # explicit document-level COMPLETED status in this event, mark it
        # completed ourselves. Remove this if SignYu is confirmed to always
        # send an explicit document status.
        document.status = Document.Status.COMPLETED
        document.completed_at = timezone.now()
        document.save()
        _log(document, "Document completed")

    event.processed = True
    event.save(update_fields=["processed", "document"])
    return JsonResponse({"status": "received", "matched": True})


def settings_webhook_secret_mismatch(request) -> bool:
    from django.conf import settings

    secret = settings.SIGNYU_WEBHOOK_SECRET
    if not secret:
        return False  # no secret configured - accept (unconfirmed mechanism, see Section 23)
    provided = request.headers.get("X-Signyu-Secret", "")
    return provided != secret