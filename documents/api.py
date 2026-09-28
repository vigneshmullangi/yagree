"""
Public API v1 - used server-to-server by external systems (e.g. the HRMS).

Authentication: `X-API-Key: <key>` header. Keys belong to an APIClient and
are created with `python manage.py create_api_client`.
Full request/response documentation is in API.md.
"""
import json
import logging
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.core.validators import validate_email
from django.db import IntegrityError
from django.http import FileResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import ActivityLog, APIClient, Document, Signer
from .services.serializers import serialize_document
from .services.signyu import SignYuAPIError, SignYuClient, SignYuConflictError
from .services.status_map import map_document_status
from .services.workflow import fetch_signed_pdf, send_document_to_signyu, sync_document

logger = logging.getLogger(__name__)

REFRESHABLE = (Document.Status.SENT, Document.Status.PARTIALLY_SIGNED)


def api_error(code, message, status, details=None):
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JsonResponse(body, status=status)


def api_key_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        raw_key = request.headers.get("X-API-Key", "").strip()
        if not raw_key:
            return api_error("missing_api_key", "Send your API key in the X-API-Key header.", 401)
        client = (
            APIClient.objects.filter(key_hash=APIClient.hash_key(raw_key), is_active=True)
            .select_related("owner")
            .first()
        )
        if not client:
            return api_error("invalid_api_key", "The API key is invalid or has been disabled.", 401)
        APIClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
        request.api_client = client
        return view(request, *args, **kwargs)

    return wrapper


def _parse_signers(raw):
    """Returns (signers, problems). `signers` is a cleaned list or None."""
    limit = settings.MAX_SIGNERS_PER_DOCUMENT
    if not raw:
        return None, ["This field is required."]
    try:
        items = json.loads(raw)
    except ValueError:
        return None, ["Must be a valid JSON array."]
    if not isinstance(items, list) or not 1 <= len(items) <= limit:
        return None, [f"Provide between 1 and {limit} signers."]

    cleaned, problems, seen = [], [], set()
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            problems.append(f"Signer {position}: must be an object with name and email.")
            continue
        name = str(item.get("name") or "").strip()
        email = str(item.get("email") or "").strip().lower()
        phone = str(item.get("phone") or "").strip()
        if not name:
            problems.append(f"Signer {position}: name is required.")
        try:
            validate_email(email)
        except ValidationError:
            problems.append(f"Signer {position}: a valid email is required.")
        else:
            if email in seen:
                problems.append(f"Signer {position}: email {email} is listed more than once.")
            seen.add(email)
        if len(phone) > 20:
            problems.append(f"Signer {position}: phone is too long.")
        cleaned.append({"name": name, "email": email, "phone": phone})
    return (None, problems) if problems else (cleaned, None)


def _validate_upload(upload):
    if not upload.name.lower().endswith(".pdf"):
        return "Only PDF files are accepted."
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload.size > max_bytes:
        return f"File is too large. Maximum size is {settings.MAX_UPLOAD_SIZE_MB} MB."
    head = upload.read(5)
    upload.seek(0)
    if head != b"%PDF-":
        return "The file is not a valid PDF."
    return None


def _replay(document):
    """Response for a create request whose external_ref already exists."""
    response = JsonResponse(serialize_document(document), status=200)
    response["X-Yagree-Idempotent-Replay"] = "true"
    return response


def _resume_send(document):
    """
    An earlier request created the document but SignYu refused/timed out on
    'send'. Retrying the same request only retries that last step.
    """
    try:
        send_document_to_signyu(document)
    except SignYuConflictError:
        # SignYu already has it as sent (our earlier response was lost): reconcile.
        try:
            document, _ = sync_document(document, source="reconciled after retry")
        except SignYuAPIError:
            logger.exception("Reconcile failed for %s", document.id)
            return api_error("send_failed", "The document exists but could not be sent. Retry the same request.", 502,
                             {"document_id": str(document.id)})
    except SignYuAPIError:
        logger.exception("Resume send failed for %s", document.id)
        return api_error("send_failed", "The document exists but could not be sent. Retry the same request.", 502,
                         {"document_id": str(document.id)})
    return _replay(document)


def _create_document(request):
    client = request.api_client
    name = (request.POST.get("name") or "").strip()
    external_ref = (request.POST.get("external_ref") or "").strip()
    upload = request.FILES.get("file")

    errors = {}
    if not name:
        errors["name"] = "This field is required."
    elif len(name) > 255:
        errors["name"] = "Maximum 255 characters."
    if not external_ref:
        errors["external_ref"] = "This field is required."
    elif len(external_ref) > 100:
        errors["external_ref"] = "Maximum 100 characters."
    if not upload:
        errors["file"] = "A PDF file is required (multipart field 'file')."
    else:
        problem = _validate_upload(upload)
        if problem:
            errors["file"] = problem
    signers, signer_problems = _parse_signers(request.POST.get("signers"))
    if signer_problems:
        errors["signers"] = signer_problems
    if errors:
        return api_error("validation_error", "The request is not valid.", 422, errors)

    existing = Document.objects.filter(api_client=client, external_ref=external_ref).first()
    if existing:
        if existing.status == Document.Status.DRAFT and existing.signyu_document_id and existing.signers.exists():
            return _resume_send(existing)
        return _replay(existing)

    try:
        signyu = SignYuClient()
    except SignYuAPIError:
        logger.exception("SignYu client is not configured")
        return api_error("provider_error", "The signing provider is not available.", 502)

    document = Document(owner=client.owner, api_client=client, external_ref=external_ref, name=name)
    document.original_file = upload
    try:
        document.save()
    except IntegrityError:
        # Two identical requests raced; the other one won.
        existing = Document.objects.filter(api_client=client, external_ref=external_ref).first()
        return _replay(existing) if existing else api_error("conflict", "Please retry.", 409)

    # Step 1: create the document at SignYu.
    try:
        raw = signyu.create_document(document.name, document.original_file)
    except SignYuAPIError:
        logger.exception("API create: SignYu create_document failed (%s)", external_ref)
        document.delete()
        return api_error("provider_error", "Unable to create the document. Please try again.", 502)

    payload = signyu.extract_document_payload(raw)
    document.signyu_document_id = payload.get("documentId", "")
    document.signyu_raw_status = payload.get("status", "")
    document.signyu_create_response = raw
    document.status = map_document_status(payload.get("status", "")) or Document.Status.DRAFT
    document.save()
    ActivityLog.objects.create(document=document, message="Document created (API)")

    # Step 2: add the signers.
    for order, data in enumerate(signers, start=1):
        Signer.objects.create(document=document, signing_order=order, **data)
    try:
        signyu.add_signers(document.signyu_document_id, signers)
    except SignYuAPIError:
        logger.exception("API create: SignYu add_signers failed (%s)", external_ref)
        document.delete()
        return api_error("provider_error", "Unable to add the signers. Please try again.", 502)
    ActivityLog.objects.create(document=document, message=f"{len(signers)} signer(s) added (API)")

    # Step 3: send. A failure here keeps the document so a retry resumes at this step.
    try:
        send_document_to_signyu(document, signyu)
    except SignYuAPIError:
        logger.exception("API create: SignYu send failed (%s)", external_ref)
        return api_error("send_failed", "The document was created but could not be sent. Retry the same request.", 502,
                         {"document_id": str(document.id)})

    return JsonResponse(serialize_document(document), status=201)


def _list_documents(request):
    documents = Document.objects.filter(api_client=request.api_client).prefetch_related("signers")
    external_ref = request.GET.get("external_ref")
    status = request.GET.get("status")
    if external_ref:
        documents = documents.filter(external_ref=external_ref)
    if status:
        documents = documents.filter(status=status.upper())
    try:
        page = max(int(request.GET.get("page", 1)), 1)
        page_size = min(max(int(request.GET.get("page_size", 20)), 1), 100)
    except ValueError:
        return api_error("invalid_parameter", "page and page_size must be integers.", 422)

    paginator = Paginator(documents, page_size)
    try:
        results = [serialize_document(d) for d in paginator.page(page)]
    except EmptyPage:
        results = []
    return JsonResponse({"count": paginator.count, "page": page, "page_size": page_size, "results": results})


@csrf_exempt
@api_key_required
def documents_collection(request):
    if request.method == "POST":
        return _create_document(request)
    if request.method == "GET":
        return _list_documents(request)
    return api_error("method_not_allowed", "Use GET or POST.", 405)


def _get_document(request, pk):
    return Document.objects.filter(pk=pk, api_client=request.api_client).prefetch_related("signers").first()


@csrf_exempt
@api_key_required
def document_item(request, pk):
    if request.method != "GET":
        return api_error("method_not_allowed", "Use GET.", 405)
    document = _get_document(request, pk)
    if not document:
        return api_error("not_found", "Document not found.", 404)

    if request.GET.get("refresh", "").lower() in ("1", "true") and document.status in REFRESHABLE:
        try:
            document, _ = sync_document(document, source="API refresh")
        except SignYuAPIError:
            logger.exception("API refresh failed for %s", document.id)
            return api_error("provider_error", "Unable to refresh the status right now. Please try again.", 502)
    return JsonResponse(serialize_document(document))


@csrf_exempt
@api_key_required
def document_download(request, pk):
    if request.method != "GET":
        return api_error("method_not_allowed", "Use GET.", 405)
    document = _get_document(request, pk)
    if not document:
        return api_error("not_found", "Document not found.", 404)
    if document.status != Document.Status.COMPLETED:
        return api_error("not_completed", "The signed PDF is available once the document is completed.", 409)
    try:
        signed_file = fetch_signed_pdf(document)
    except SignYuAPIError:
        logger.exception("API download failed for %s", document.id)
        return api_error("provider_error", "Unable to fetch the signed PDF right now. Please try again.", 502)
    return FileResponse(
        signed_file.open("rb"), as_attachment=True, filename=f"{document.name}-signed.pdf",
        content_type="application/pdf",
    )


@csrf_exempt
@api_key_required
def ping(request):
    """Lets an integrator confirm their API key works."""
    return JsonResponse({"status": "ok", "client": request.api_client.name})
