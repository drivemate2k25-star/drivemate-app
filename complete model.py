from decimal import Decimal
from django.db import models
from django.db import models
from django.db.models import Q
from django.utils import timezone
from datetime import time as _time

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






class RidePurpose(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=160, blank=True)

    def __str__(self):
        return self.name



class Ride(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        ACCEPTED = "accepted", "Accepted"
        ONGOING = "ongoing", "Ongoing"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Mode(models.TextChoices):
        DRIVER_ONLY = "driver_only", "Driver Only"  # fixed shift pricing
        CAR_WITH_DRIVER = "car_with_driver", "Car with Driver"  # per km/min pricing

    customer = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="rides")
    driver = models.ForeignKey("accounts.Driver", on_delete=models.SET_NULL, null=True, blank=True, related_name="rides")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.SET_NULL, null=True, blank=True, related_name="rides")

    ride_mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.DRIVER_ONLY)

    start_location = models.CharField(max_length=255)
    start_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    start_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    end_location = models.CharField(max_length=255)
    end_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    end_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)

    female_driver_preference = models.BooleanField(default=False)

    purpose = models.ForeignKey(RidePurpose, on_delete=models.SET_NULL, null=True, blank=True, related_name="rides")

    # Distance/time (entered by driver after trip ends)
    actual_distance_km = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    actual_duration_min = models.PositiveIntegerField(null=True, blank=True)

    # Fare fields (computed after trip)
    base_fare = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "start_time"])]

    def __str__(self):
        return f"Ride #{self.pk} - {self.customer.name} ({self.get_status_display()})"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.ride_mode == Ride.Mode.CAR_WITH_DRIVER and self.vehicle is None:
            raise ValidationError("Vehicle is required when ride_mode is 'car_with_driver'.")

    def calculate_fare(self):
        if self.ride_mode == Ride.Mode.DRIVER_ONLY:
            # Decide day or night fare based on start_time
            if 6 <= self.start_time.hour < 18:
                self.base_fare = self.driver.day_fixed_charge
            else:
                self.base_fare = self.driver.night_fixed_charge

        elif self.ride_mode == Ride.Mode.CAR_WITH_DRIVER and self.vehicle:
            distance = self.actual_distance_km or Decimal("0")
            duration = self.actual_duration_min or 0
            self.base_fare = (distance * self.vehicle.per_km_rate) + (duration * self.vehicle.per_min_rate)

        # Apply taxes/discounts
        self.tax_amount = (self.base_fare or 0) * Decimal("0.05")  # Example 5% GST
        self.total_amount = (self.base_fare or 0) + (self.tax_amount or 0) - (self.discount_amount or 0)

        return self.total_amount


class SubscriptionPlan(models.Model):
    BILLING_PERIOD_CHOICES = (
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("yearly", "Yearly"),
    )

    name = models.CharField(max_length=100)
    description = models.TextField()
    monthly_fee = models.DecimalField(max_digits=10, decimal_places=2)
    hours_included = models.PositiveIntegerField(help_text="Total hours per month")
    billing_period = models.CharField(max_length=10, choices=BILLING_PERIOD_CHOICES, default="monthly")

    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - ₹{self.monthly_fee}"


class Subscription(models.Model):
    customer = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.CASCADE)

    # Optional dedicated driver/vehicle for the subscription
    driver = models.ForeignKey("accounts.Driver", on_delete=models.SET_NULL, null=True, blank=True)
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.SET_NULL, null=True, blank=True)

    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(blank=True, null=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.customer.name} - {self.plan.name}"



class Rating(models.Model):
    ride = models.OneToOneField(Ride, on_delete=models.CASCADE, related_name="rating")
    customer = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="given_ratings")
    driver = models.ForeignKey("accounts.Driver", on_delete=models.CASCADE, related_name="received_ratings")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.SET_NULL, null=True, blank=True, related_name="ratings")
    score = models.PositiveIntegerField(default=5)  # 1..5
    feedback = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Rating {self.score} for {self.driver.user.name} (Ride #{self.ride_id})"



class RideTracking(models.Model):
    ride = models.ForeignKey(Ride, on_delete=models.CASCADE, related_name="tracking_points")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    speed_kmph = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    heading_deg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["ride", "timestamp"])]

    def __str__(self):
        return f"Tracking Ride #{self.ride_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}"


class SOSAlert(models.Model):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="sos_alerts")
    ride = models.ForeignKey(Ride, on_delete=models.SET_NULL, null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    triggered_at = models.DateTimeField(default=timezone.now)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"SOS by {self.user.name} at {self.triggered_at:%Y-%m-%d %H:%M:%S}"



class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    class Method(models.TextChoices):
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        NETBANKING = "netbanking", "NetBanking"
        WALLET = "wallet", "Wallet"
        CASH = "cash", "Cash"

    customer = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="payments")
    ride = models.ForeignKey("rides.Ride", on_delete=models.CASCADE, null=True, blank=True, related_name="payments")
    subscription = models.ForeignKey(
        "rides.Subscription", on_delete=models.CASCADE, null=True, blank=True, related_name="payments"
    )

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    method = models.CharField(max_length=12, choices=Method.choices)
    order_id = models.CharField(max_length=80, blank=True)
    transaction_id = models.CharField(max_length=80, blank=True)
    receipt_number = models.CharField(max_length=40, blank=True)

    tip_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(Q(ride__isnull=False) | Q(subscription__isnull=False)),
                name="payment_has_target",
            )
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["order_id"]),
            models.Index(fields=["transaction_id"]),
        ]

    def __str__(self):
        target = f"Ride #{self.ride_id}" if self.ride_id else f"Subscription #{self.subscription_id}"
        return f"Payment {self.status} - {self.amount} {self.currency} for {target}"



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
