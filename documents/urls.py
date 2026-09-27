from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("documents/create/", views.create_document, name="create_document"),
    path("documents/<uuid:pk>/signers/", views.add_signers, name="add_signers"),
    path("documents/<uuid:pk>/review/", views.review_send, name="review_send"),
    path("documents/<uuid:pk>/", views.document_detail, name="document_detail"),
    path("documents/<uuid:pk>/signers/<int:signer_id>/sign-embedded/", views.sign_embedded, name="sign_embedded"),
    path("documents/<uuid:pk>/download/", views.download_document, name="download_document"),
    path("documents/<uuid:pk>/sync/", views.sync_status, name="sync_status"),
    path("api/webhooks/signyu/", views.signyu_webhook, name="signyu_webhook"),
]