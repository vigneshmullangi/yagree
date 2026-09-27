from django.contrib.auth.forms import AuthenticationForm
from django.forms import TextInput, PasswordInput


class YagreeLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget = TextInput(
            attrs={"class": "form-input", "placeholder": "Username", "autofocus": True}
        )
        self.fields["password"].widget = PasswordInput(
            attrs={"class": "form-input", "placeholder": "Password"}
        )
