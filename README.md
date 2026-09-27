# Yagree

Django e-signature middleware in front of the SignYu API.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SIGNYU_API_KEY and your MySQL credentials

# In XAMPP, create a database named `yagree` (or whatever DB_NAME you set)

python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser    # this is your admin/document-sender login

python manage.py runserver
```

Visit `http://127.0.0.1:8000/accounts/login/`.

## What's built

- Admin login (Django auth, custom `User` model with a `role` field for future use)
- Dashboard → Create Document → Add Signers (1–6) → Review & Send
- Document detail page with per-signer status and an activity log
- Webhook endpoint at `POST /api/webhooks/signyu/`
- Download of the completed signed PDF
- Every SignYu HTTP call lives in one file: `documents/services/signyu.py`

Not built (intentionally, out of scope for the acceptance criteria in the
brief): a separate signer login portal inside Yagree. Your own workflow
mockup shows SignYu emailing signers directly and hosting the actual
signing page — Yagree's job is document management, not re-hosting that
experience. Say the word if you want a signer portal added.

## ⚠️ Three things still need to be confirmed against the real SignYu Postman collection

These are isolated to `documents/services/signyu.py`, `documents/services/status_map.py`,
and `.env` so they're a small fix once confirmed — nothing else in the codebase
should need to change:

1. **Auth header.** Currently assumed to be `Authorization: Bearer <key>`
   (configurable via `SIGNYU_AUTH_HEADER` / `SIGNYU_AUTH_SCHEME` in `.env`).
   Check the Authorization tab on any request in the real collection.
2. **Create-document request.** Currently assumed to be
   `POST /documents` as `multipart/form-data` with fields `name` and `file`.
   This was never shown in the screenshots you shared — it's the single
   highest-risk assumption in this codebase and should be checked first.
3. **Webhook payload shape.** `documents/views.py::signyu_webhook` stores
   every raw payload unconditionally (nothing is ever lost — see
   `WebhookEvent.payload`), and does a best-effort lookup for document ID /
   signer ID / status under several plausible key names. Once you trigger a
   real webhook (or see it in the collection), tighten the key names in
   that view and in `status_map.py`.

Everything else (list documents, add signers, send, the 409 conflict
shape) matches exactly what was visible in your screenshots.

## Testing the webhook locally

SignYu needs a public URL to reach your webhook. Use ngrok:

```bash
ngrok http 8000
```

Then point SignYu's webhook config at
`https://<your-ngrok-subdomain>.ngrok.io/api/webhooks/signyu/`.

## Not yet tested end-to-end

This was built without network access to the real SignYu API, so while
every endpoint/method/body for the *confirmed* calls matches your
screenshots exactly, the full create → add signers → send → webhook →
complete → download flow has not been run against the live API yet. Run
through Section 38 of your own spec (the numbered test list) once your
`.env` is filled in — start with **Create Document**, since that's where
the one unconfirmed assumption (#2 above) will surface immediately as a
clear 4xx from SignYu if it's wrong, rather than a silent failure.
