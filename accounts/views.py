from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.urls import reverse_lazy

from .forms import YagreeLoginForm


class YagreeLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = YagreeLoginForm
    redirect_authenticated_user = True


def logout_view(request):
    logout(request)
    return redirect(reverse_lazy("accounts:login"))
