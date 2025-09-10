from django.db import models
from django.db.models import Q
from django.utils import timezone


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
