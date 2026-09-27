"""
Maps SignYu's document status strings to Yagree's own Document.Status enum.

Only "SENT" has actually been observed (in the provided screenshots).
Everything else below is a reasonable guess at likely SignYu values and
MUST be corrected once the real values are seen (from the Postman
collection, or from live API/webhook responses). Unknown values fall
through to the raw string being stored on `signyu_raw_status` and the
Yagree `status` field being left unchanged, rather than being guessed
into the wrong bucket.
"""

# ASSUMPTION - verify these against real SignYu responses.
SIGNYU_TO_YAGREE_STATUS = {
    "PENDING": "DRAFT",  # CONFIRMED: real status returned by POST /documents (create)
    "DRAFT": "DRAFT",
    "SENT": "SENT",  # CONFIRMED: real status returned by POST /documents/{id}/send
    "IN_PROGRESS": "PARTIALLY_SIGNED",
    "PARTIALLY_SIGNED": "PARTIALLY_SIGNED",
    "COMPLETED": "COMPLETED",
    "SIGNED": "COMPLETED",
    "DECLINED": "DECLINED",
    "REJECTED": "DECLINED",
    "EXPIRED": "EXPIRED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
}

SIGNYU_SIGNER_STATUS = {
    "PENDING": "PENDING",
    "WAITING": "PENDING",
    "SIGNED": "SIGNED",
    "COMPLETED": "SIGNED",
    "DECLINED": "DECLINED",
    "REJECTED": "DECLINED",
}


def map_document_status(signyu_status: str):
    if not signyu_status:
        return None
    return SIGNYU_TO_YAGREE_STATUS.get(signyu_status.upper())


def map_signer_status(signyu_status: str):
    if not signyu_status:
        return None
    return SIGNYU_SIGNER_STATUS.get(signyu_status.upper())