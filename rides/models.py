from decimal import Decimal
from django.db import models
from django.db.models import Q
from django.utils import timezone



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

class RideRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        COMPLETED = "completed", "Completed"
        AUTO_CANCELLED = "auto_cancelled", "Auto Cancelled"

    ride = models.ForeignKey("rides.Ride", on_delete=models.CASCADE, related_name="driver_requests")
    driver = models.ForeignKey("accounts.Driver", on_delete=models.CASCADE, related_name="ride_requests")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    requested_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("ride", "driver")  # prevent duplicate requests

    def __str__(self):
        return f"Ride #{self.ride_id} -> {self.driver.user.name} ({self.status})"


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
