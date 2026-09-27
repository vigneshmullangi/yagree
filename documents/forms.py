from django import forms
from django.conf import settings

from .models import Document, Signer


class CreateDocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["name", "original_file"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. Employment Agreement"}),
            "original_file": forms.ClearableFileInput(attrs={"class": "form-file", "accept": "application/pdf"}),
        }

    def clean_original_file(self):
        file = self.cleaned_data["original_file"]
        if not file.name.lower().endswith(".pdf") or file.content_type not in (
            "application/pdf",
            "application/x-pdf",
        ):
            raise forms.ValidationError("Only PDF files are allowed.")
        max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if file.size > max_bytes:
            raise forms.ValidationError(f"File is too large. Max size is {settings.MAX_UPLOAD_SIZE_MB} MB.")
        return file


class SignerForm(forms.ModelForm):
    class Meta:
        model = Signer
        fields = ["name", "email", "phone", "signing_order"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input", "placeholder": "Full name"}),
            "email": forms.EmailInput(attrs={"class": "form-input", "placeholder": "email@example.com"}),
            "phone": forms.TextInput(attrs={"class": "form-input", "placeholder": "Phone (optional)"}),
            "signing_order": forms.NumberInput(attrs={"class": "form-input"}),
        }


SignerFormSet = forms.modelformset_factory(
    Signer,
    form=SignerForm,
    extra=1,
    max_num=settings.MAX_SIGNERS_PER_DOCUMENT,
    validate_max=True,
    can_delete=True,
)
