"""
Thin client around the SignYu API. Views must never call `requests`
directly - everything goes through SignYuClient so there's one place
to fix once the exact contract is confirmed.

CONFIRMED from provided Postman screenshots:
    GET  {base}/documents                              -> list documents
    POST {base}/documents/{document_id}/signers         -> add signers
         body: {"signers": [{"name", "email", "phone"}]}
    POST {base}/documents/{document_id}/send             -> send for signing
    409 error shape: {"error": "invalid_state", "message": "..."}

ASSUMPTIONS (not visible in the screenshots - verify against the real
Postman collection and fix here, this is the ONLY file that should need
changes once you confirm them):
    - Auth: `Authorization: Bearer <SIGNYU_API_KEY>` header
      (settings.SIGNYU_AUTH_HEADER / SIGNYU_AUTH_SCHEME)
    - Create document: POST {base}/documents as multipart/form-data with
      fields "name" and "file"
    - Get single document: GET {base}/documents/{document_id}
    - Download signed document: GET {base}/documents/{document_id}/download
    - Successful add-signers / send response bodies beyond what was shown
      (e.g. where per-signer sign_url lives) - this client reads several
      likely key names defensively and always stores the full raw response
      on the Document row so nothing is lost even if parsing misses a field.
"""
import requests
from django.conf import settings


class SignYuAPIError(Exception):
    """Raised for any non-2xx response or transport failure."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SignYuConflictError(SignYuAPIError):
    """Raised specifically for 409 responses (e.g. signers added after send)."""


class SignYuClient:
    def __init__(self):
        self.base_url = settings.SIGNYU_BASE_URL.rstrip("/")
        self.api_key = settings.SIGNYU_API_KEY
        if not self.api_key:
            raise SignYuAPIError("SIGNYU_API_KEY is not configured.")

    def _auth_headers(self):
        header_name = settings.SIGNYU_AUTH_HEADER
        scheme = settings.SIGNYU_AUTH_SCHEME
        value = f"{scheme} {self.api_key}".strip() if scheme else self.api_key
        return {header_name: value}

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        try:
            response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise SignYuAPIError(f"Could not reach SignYu: {exc}") from exc

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"raw": response.text}

        if response.status_code == 409:
            raise SignYuConflictError(
                data.get("message", "Conflict with current document state."),
                status_code=409,
                payload=data,
            )
        if not response.ok:
            raise SignYuAPIError(
                data.get("message") or f"SignYu returned HTTP {response.status_code}",
                status_code=response.status_code,
                payload=data,
            )
        return data

    # ------------------------------------------------------------------
    # Document lifecycle
    # ------------------------------------------------------------------

    def create_document(self, name: str, file_obj) -> dict:
        """
        ASSUMPTION: POST /documents as multipart/form-data with "name" and
        "file" fields. Confirm against Postman and adjust field names /
        content type here if different.
        """
        file_obj.seek(0)
        files = {"file": (file_obj.name, file_obj.read(), "application/pdf")}
        data = {"name": name}
        return self._request("POST", "/documents", data=data, files=files)

    def add_signers(self, signyu_document_id: str, signers: list[dict]) -> dict:
        """
        CONFIRMED endpoint/body shape:
            POST /documents/{id}/signers
            {"signers": [{"name": ..., "email": ..., "phone": ...}]}
        """
        payload = {
            "signers": [
                {"name": s["name"], "email": s["email"], "phone": s.get("phone", "")}
                for s in signers
            ]
        }
        return self._request("POST", f"/documents/{signyu_document_id}/signers", json=payload)

    def send_document(self, signyu_document_id: str) -> dict:
        """CONFIRMED endpoint: POST /documents/{id}/send"""
        return self._request("POST", f"/documents/{signyu_document_id}/send")

    def get_document(self, signyu_document_id: str) -> dict:
        """ASSUMPTION: GET /documents/{id}. Confirm response shape."""
        return self._request("GET", f"/documents/{signyu_document_id}")

    def download_document(self, signyu_document_id: str) -> bytes:
        """
        DEPRECATED GUESS - kept only as a last-resort fallback. CONFIRMED
        reality (from GET /documents/{id}): completed documents carry a
        real `downloadUrl` field directly in the document payload. Views
        should call `download_from_url()` with that value instead of this
        method whenever a downloadUrl is available.
        """
        url = f"{self.base_url}/documents/{signyu_document_id}/download"
        headers = self._auth_headers()
        try:
            response = requests.get(url, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise SignYuAPIError(f"Could not reach SignYu: {exc}") from exc
        if not response.ok:
            raise SignYuAPIError(
                f"SignYu returned HTTP {response.status_code} for download",
                status_code=response.status_code,
            )
        return response.content

    def download_from_url(self, url: str) -> bytes:
        """
        CONFIRMED: fetches the signed PDF from the real `downloadUrl`
        returned by GET /documents/{id}. Sends the API key header in case
        it's required - harmless if the URL is already pre-signed and
        doesn't need it.
        """
        headers = self._auth_headers()
        try:
            response = requests.get(url, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise SignYuAPIError(f"Could not reach SignYu: {exc}") from exc
        if not response.ok:
            raise SignYuAPIError(
                f"SignYu returned HTTP {response.status_code} for download",
                status_code=response.status_code,
            )
        return response.content

    # ------------------------------------------------------------------
    # Response parsing helpers - defensive because the exact successful
    # response shapes for add_signers/send beyond the one example we've
    # seen are not confirmed.
    # ------------------------------------------------------------------

    @staticmethod
    def extract_document_payload(response: dict) -> dict:
        """Unwrap the {"documents": [...]} list-wrapper seen in screenshots,
        or return the dict itself if it's already a single document."""
        if isinstance(response, dict) and "documents" in response:
            docs = response["documents"]
            return docs[0] if docs else {}
        return response or {}

    @staticmethod
    def extract_signers_payload(response: dict) -> list:
        """Best-effort extraction of a signers list from an add_signers
        response. Falls back to an empty list - callers should still keep
        the raw response for audit even if this can't find anything."""
        if not isinstance(response, dict):
            return []
        for key in ("signers", "data", "results"):
            if key in response and isinstance(response[key], list):
                return response[key]
        return []