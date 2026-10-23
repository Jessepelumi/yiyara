import uuid

from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        
        email = self.normalize_email(email).lower()
        extra_fields.setdefault("is_active", True)

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)

        return user
    
    def create_superuser(self, email, password=None, **extra_fields):

        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)
    
    def get_by_natural_key(self, username):
        return self.get(email=username)


class User(AbstractUser): 
    id = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )

    username = models.CharField(
        max_length=150,
        blank=True,
        unique=True
    )

    email = models.EmailField(
        unique=True, 
        db_index=True
    )
    is_email_verified = models.BooleanField(default=False)

    google_sub = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )

    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    GENDER_CHOICES = [
        ("M", "Male"),
        ("F", "Female"),
        ("O", "Other"),
    ]
    gender = models.CharField(
        max_length=1,
        choices=GENDER_CHOICES,
        blank=True
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ['email']

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()

        if not self.username and self.email:
            base = self.email.split("@")[0]
            username = base
            counter = 1

            while User.objects.filter(
                username=username
            ).exclude(pk=self.pk).exists():
                username = f"{base}{counter}"
                counter += 1

            self.username = username

        super().save(*args, **kwargs)

    def __str__(self):
        return self.email
    
