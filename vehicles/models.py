from decimal import Decimal
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Vehicle(models.Model):
    class VehicleType(models.TextChoices):
        HATCHBACK = "hatchback", "Hatchback"
        SEDAN = "sedan", "Sedan"
        SUV = "suv", "SUV"
        MUV = "muv", "MUV"
        LCV = "lcv", "LCV"
        LUXURY = "luxury", "Luxury"
        VAN = "van", "Van"

    class Transmission(models.TextChoices):
        MANUAL = "manual", "Manual"
        AUTOMATIC = "automatic", "Automatic"

    class Fuel(models.TextChoices):
        PETROL = "petrol", "Petrol"
        DIESEL = "diesel", "Diesel"
        CNG = "cng", "CNG"
        ELECTRIC = "electric", "Electric"
        HYBRID = "hybrid", "Hybrid"

    owner = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="vehicles")
    current_driver = models.ForeignKey(
        "accounts.Driver", on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_vehicles"
    )

    vehicle_type = models.CharField(max_length=20, choices=VehicleType.choices)
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    year = models.PositiveIntegerField()
    color = models.CharField(max_length=30, blank=True)
    registration_number = models.CharField(max_length=20, unique=True)
    seat_capacity = models.PositiveIntegerField(default=4)
    ac = models.BooleanField(default=True)
    transmission = models.CharField(max_length=10, choices=Transmission.choices, default=Transmission.MANUAL)
    fuel_type = models.CharField(max_length=10, choices=Fuel.choices, default=Fuel.PETROL)

    fitness_cert_expiry = models.DateField(blank=True, null=True)
    insurance_expiry = models.DateField(blank=True, null=True)
    permit_expiry = models.DateField(blank=True, null=True)

    per_km_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00"),
        help_text="Fare per km when vehicle hired with driver"
    )
    per_min_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00"),
        help_text="Fare per minute (optional)"
    )
    
    verified = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["registration_number"])]

    def __str__(self):
        return f"{self.make} {self.model} ({self.registration_number})"

class VehicleImage(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="vehicle_images/")
    caption = models.CharField(max_length=120, blank=True)
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "is_primary"],
                condition=Q(is_primary=True),
                name="unique_primary_image_per_vehicle",
            )
        ]

    def __str__(self):
        return f"Image for {self.vehicle.registration_number} ({'primary' if self.is_primary else 'extra'})"
