from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom user model so we can attach a role now without a painful
    migration later. Only ADMIN accounts are used in this MVP (signers
    are managed as plain email/name/phone records on the Signer model,
    since SignYu handles their signing experience directly).
    """

    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        SIGNER = "SIGNER", "Signer"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.ADMIN)

    def __str__(self):
        return self.username
