from django.shortcuts import render
from accounts.models import Driver
from accounts.views import login_required_role
import uuid
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.db import transaction as db_transaction
from django.utils import timezone
from django.contrib import messages
from django.db.models import Sum
from .models import  Payment
from rides.models import Ride, RideRequest



# show payment page for a completed ride
@login_required_role(allowed_roles=['customer'])
def payment_page(request, ride_id):
    uid = request.session.get('user_id')
    ride = get_object_or_404(Ride, pk=ride_id, customer_id=uid)

    ride_request = ride.driver_requests.filter(status=RideRequest.Status.COMPLETED).first()

    if not ride_request:
        messages.error(request, "Payment is available only after the driver has completed the ride.")
        return redirect('trip_detail', ride_id=ride.id)

    # Ensure fare is calculated
    if ride.total_amount is None:
        # calculate_fare sets base_fare, tax_amount and total_amount on self
        ride.calculate_fare()
        ride.save(update_fields=['base_fare', 'tax_amount', 'total_amount', 'updated_at'])

    payments = ride.payments.all().order_by('-created_at')  # recent payments for the ride

    context = {
        'ride': ride,
        'payments': payments,
        'amount': str(ride.total_amount),  # string for JS usage
    }
    return render(request, 'payment_page.html', context)



# create transaction (called by JS to begin a payment)
@require_POST
@login_required_role(allowed_roles=['customer'])
def create_transaction(request):
    uid = request.session.get('user_id')
    ride_id = request.POST.get('ride_id')
    method = request.POST.get('method')  # 'upi', 'card', 'qr', ...
    amount = request.POST.get('amount')

    if not ride_id or not method or not amount:
        return JsonResponse({'error': 'missing parameters'}, status=400)

    ride = get_object_or_404(Ride, pk=ride_id)
    ride.status = ride.Status.COMPLETED
    

    if ride.customer_id != uid:
        return HttpResponseForbidden("Not allowed")

    ride_request = ride.driver_requests.filter(status=RideRequest.Status.COMPLETED).first()

    if not ride_request:
        messages.error(request, "Payment is available only after the driver has completed the ride.")
        return redirect('trip_detail', ride_id=ride.id)

    try:
        amount_dec = Decimal(amount)
    except Exception:
        return JsonResponse({'error': 'invalid amount'}, status=400)

    # check if already paid (simple check: any SUCCESS payments covering the amount)
    paid_sum = ride.payments.filter(status=Payment.Status.SUCCESS).aggregate(
    total_amount_sum=Sum('amount')
    )['total_amount_sum'] or Decimal('0')
        
    if paid_sum >= amount_dec:
        return JsonResponse({'error': 'ride already paid'}, status=400)

    with db_transaction.atomic():
        order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
        payment = Payment.objects.create(
            customer_id=uid,
            ride=ride,
            amount=amount_dec,
            currency='INR',
            method=method,
            order_id=order_id,
            status=Payment.Status.PENDING,
        )

    # Simulate provider-specific payloads:
    ride.save()
    upi_deeplink = f"upi://pay?pa=merchant@upi&pn=Ride+Payment&am={amount_dec}&cu=INR&tr={payment.id}"
    response = {
        'tx_id': payment.id,
        'amount': str(payment.amount),
        'upi_deeplink': upi_deeplink,
    }
    return JsonResponse(response)

@require_POST
@login_required_role(allowed_roles=['customer'])
def finalize_transaction(request):
    uid = request.session.get('user_id')
    tx_id = request.POST.get('tx_id')
    provider_txn_id = request.POST.get('provider_txn_id')

    if not tx_id:
        return JsonResponse({'error': 'missing tx_id'}, status=400)

    try:
        tx_id_int = int(str(tx_id).strip())
    except (ValueError, TypeError):

        return JsonResponse({'error': 'invalid tx_id'}, status=400)

    payment = get_object_or_404(Payment, pk=tx_id_int, customer_id=uid)

    if payment.status == Payment.Status.SUCCESS:
        return JsonResponse({'ok': True, 'message': 'already paid', 'tx_id': payment.id})

    payment.status = Payment.Status.SUCCESS
    payment.transaction_id = provider_txn_id or f"SIM-{uuid.uuid4().hex[:10].upper()}"
    payment.paid_at = timezone.now()
    payment.save(update_fields=['status', 'transaction_id', 'paid_at', 'updated_at'])

    return JsonResponse({'ok': True, 'tx_id': payment.id, 'paid_at': payment.paid_at.isoformat()})


@login_required_role(allowed_roles=['customer'])
def customer_payment_history(request):
    uid = request.session.get('user_id')  # get logged-in customer ID
    payments = Payment.objects.filter(customer_id=uid).order_by('-created_at')

    context = {
        'payments': payments,
        'user_role': 'customer',
    }
    return render(request, 'customer_payment_history.html', context)

# View for Driver Payment History
@login_required_role(allowed_roles=['driver'])
def driver_payment_history(request):
    try:
        # Get the driver's profile
        driver = request.session.get('user_id')
        # Fetch payments associated with rides driven by this driver
        payments = Payment.objects.filter(ride__driver=driver).order_by('-created_at')
        context = {
            'payments': payments,
            'user_role': 'driver',
        }
        return render(request, 'driver_payment_history.html' , context)
    except Driver.DoesNotExist:
        messages.error(request, "Driver profile not found.")
        return redirect('login')