from django.db import models

from django.db.models import Q
from django.utils import timezone
from decimal import Decimal
from datetime import time as _time
import datetime

class User(models.Model):
    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("driver", "Driver"),
        ("admin", "Admin"),
    )

    GENDER_CHOICES = (
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    )

    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, unique=True)
    password = models.CharField(max_length=255)  # store hashed password only
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="customer")
    language_preference = models.CharField(max_length=30, default="en")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.role})"


class Driver(models.Model):
    user = models.OneToOneField("accounts.User", on_delete=models.CASCADE, related_name="driver_profile")
    license_number = models.CharField(max_length=50, unique=True)
    license_expiry = models.DateField(blank=True, null=True)
    experience_years = models.PositiveIntegerField(default=0)
    verified = models.BooleanField(default=False)
    background_check_passed = models.BooleanField(default=False)
    rating = models.FloatField(default=0.0)
    is_available = models.BooleanField(default=True)
    profile_pic = models.FileField(upload_to='driver_profile/', null=True, blank=True)
    id_proof = models.FileField(upload_to='id_proofs/', null=True, blank=True)
    last_location = models.CharField(max_length=255,null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Fixed charges (owner/driver sets their own)
    # Day = 06:00 - 18:00, Night otherwise (defaults below)
    day_fixed_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                           help_text="Fixed charge for daytime (6:00–18:00)")
    night_fixed_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"),
                                             help_text="Fixed charge for night (18:00–6:00)")

    # Optional per-driver custom night window (defaults to 18:00–06:00)
    night_start = models.TimeField(default=_time(hour=18, minute=0))
    night_end = models.TimeField(default=_time(hour=6, minute=0))

    def __str__(self):
        return f"Driver: {self.user.name} ({'Verified' if self.verified else 'Pending'})"
    def set_availability(self, value: bool):
        self.is_available = bool(value)
        self.save(update_fields=["is_available"])



