"""JSON shapes shared by the public API and the outbound webhooks."""


def _iso(value):
    return value.isoformat() if value else None


def serialize_signer(signer):
    return {
        "name": signer.name,
        "email": signer.email,
        "phone": signer.phone,
        "signing_order": signer.signing_order,
        "status": signer.status,
        "signed_at": _iso(signer.signed_at),
    }


def serialize_document(document):
    signers = list(document.signers.all())
    return {
        "id": str(document.id),
        "external_ref": document.external_ref,
        "name": document.name,
        "status": document.status,
        "created_at": _iso(document.created_at),
        "sent_at": _iso(document.sent_at),
        "completed_at": _iso(document.completed_at),
        "signer_count": len(signers),
        "signed_count": sum(1 for s in signers if s.status == "SIGNED"),
        "signers": [serialize_signer(s) for s in signers],
    }
