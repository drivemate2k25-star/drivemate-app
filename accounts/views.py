from datetime import datetime
import json
from django.http import HttpResponseForbidden, JsonResponse,HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal
from django.views.decorators.http import require_GET,require_POST
import requests
from django.views.decorators.http import require_http_methods
from rides.models import Ride, RideRequest
from .models import User, Driver as DriverModel
from vehicles.models import Vehicle, VehicleImage
from django.db import IntegrityError, transaction
from django.core.files.storage import FileSystemStorage
from django.contrib.auth.hashers import check_password,make_password
from rides.utils import haversine_distance
from django.db.models import Q
from django.db.models import Avg, Count, Prefetch

def health_check(request):
    return JsonResponse({"status": "ok"})

def index(request):
    uid = request.session.get("user_id")
    role = request.session.get("user_role")

    # If logged in → redirect to role-based home
    if uid:
        if role == "customer":
            return redirect("customer_dashboard")
        elif role == "driver":
            return redirect("driver_dashboard")
        elif role == "admin":
            return redirect("admin_dashboard")
        else:
            return redirect("home")  

    return render(request, "index.html")

def terms(request):
    return render(request, "terms.html")

def model(request):
    return render(request, "model.html")

def login_required_role(allowed_roles=None):
    def decorator(view_func):
        def _wrapped(request, *args, **kwargs):
            uid = request.session.get('user_id')
            role = request.session.get('user_role')
            if not uid:
                return redirect('login')
            if allowed_roles and role not in allowed_roles:
                messages.error(request, "You don't have permission to view that page.")
                return redirect('login')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def login_view(request):
    """
    Manual login view:
    - POST: checks email + password (hashed stored in user.password)
    - on success: stores user_id and user_role in session and redirects based on role
    """
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""

        if not email or not password:
            messages.error(request, "Please provide both email and password.")
            return render(request, "accounts/login.html", {"email": email})

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            messages.error(request, "Invalid email or password.")
            return render(request, "accounts/login.html", {"email": email})

        if not user.is_active:
            messages.error(request, "This account is inactive. Contact support.")
            return render(request, "accounts/login.html", {"email": email})

        # check password using Django's hashers (user.password should be a hashed string)
        if check_password(password, user.password):
            # set session values
            request.session['user_id'] = user.id
            request.session['user_role'] = user.role
            request.session['user_name'] = user.name


            if user.role == "customer":
                return redirect("customer_dashboard")
            elif user.role == "driver":
                return redirect("driver_dashboard")
            elif user.role == "admin":
                return redirect("admin_dashboard")
            else:
                return redirect("home")  # fallback
        else:
            messages.error(request, "Invalid email or password.")
            return render(request, "login.html", {"email": email})

    # GET
    return render(request, "login.html")


def logout_view(request):
    request.session.flush()  # clears the session completely
    messages.success(request, "You have been logged out.")
    return redirect("login")



@login_required_role(allowed_roles=["customer"])
def customer_dashboard(request):
    user = User.objects.get(id=request.session['user_id'])

    # --- (existing recent_ride / top_vehicles logic kept as before) ---
    recent_ride = (
        Ride.objects.filter(customer=user, status=Ride.Status.COMPLETED)
        .select_related('driver__user', 'vehicle')
        .prefetch_related(
            Prefetch(
                'vehicle__images',
                queryset=VehicleImage.objects.order_by('-is_primary', '-uploaded_at'),
                to_attr='all_images_ordered'
            )
        )
        .order_by('-end_time', '-start_time')
        .first()
    )

    recent_driver = recent_ride.driver if recent_ride and recent_ride.driver_id else None
    recent_vehicle = recent_ride.vehicle if recent_ride and recent_ride.vehicle_id else None
    recent_vehicle_images = getattr(recent_vehicle, 'all_images_ordered', []) if recent_vehicle else []

    top_n = 6
    top_vehicles_qs = (
        Vehicle.objects.filter(active=True)
        .annotate(
            avg_score=Avg('ratings__score'),
            ratings_count=Count('ratings')
        )
        .filter(ratings_count__gt=0)
        .order_by('-avg_score', '-ratings_count')
        .prefetch_related(
            Prefetch('images', queryset=VehicleImage.objects.all(), to_attr='all_images')
        )[:top_n]
    )



    vehicle_qs_for_driver = Vehicle.objects.filter(active=True).prefetch_related(
        Prefetch('images', queryset=VehicleImage.objects.filter(is_primary=True), to_attr='primary_image')
    )

    top_drivers_qs = (
        Driver.objects
        .annotate(avg_score=Avg('received_ratings__score'), ratings_count=Count('received_ratings'))
        .filter(ratings_count__gt=0)                       # only drivers with >=1 rating
        .order_by('-avg_score', '-ratings_count')[:top_n]  # top N by avg then count
        .select_related('user')                            # so driver.user.name / user fields are cheap
        .prefetch_related(
            Prefetch('assigned_vehicles', queryset=vehicle_qs_for_driver, to_attr='assigned_vehicles_prefetched')
        )
    )

    context = {
        'user': user,
        'recent_ride': recent_ride,
        'recent_driver': recent_driver,
        'recent_vehicle': recent_vehicle,
        'recent_vehicle_images': recent_vehicle_images,
        'top_vehicles': top_vehicles_qs,
        'top_drivers': top_drivers_qs,
    }

    return render(request, "customer_home.html", context)

@login_required_role(allowed_roles=["driver"])
def driver_dashboard(request):
    # get user/driver (keeps your session usage)
    uid = request.session.get("user_id")
    user = get_object_or_404(User, id=uid)
    driver = get_object_or_404(DriverModel, user__pk=uid)

    # --- only pending requests (most recent first) ---
    pending_requests = (
        RideRequest.objects
        .filter(driver=driver, status=RideRequest.Status.PENDING)
        .select_related("ride", "ride__customer", "ride__purpose", "ride__vehicle")
        .order_by("-requested_at")
    )

    # --- determine if driver currently has an active ride (best-effort) ---
    has_active_ride = False
    try:
        # Prefer canonical enum if available (Ride.Status.ACTIVE)
        if hasattr(Ride, "Status") and hasattr(Ride.Status, "ACTIVE"):
            has_active_ride = Ride.objects.filter(driver=driver, status=Ride.Status.ACTIVE).exists()
        else:
            # fallback: check a few common status strings
            has_active_ride = Ride.objects.filter(driver=driver, status__in=["active", "ongoing", "in_progress"]).exists()
    except Exception:
        # If Ride model differs in your app, fall back safely to False
        has_active_ride = False

    return render(
        request,
        "driver_home.html",
        {
            "user": user,
            "driver": driver,
            "ride_requests": pending_requests,
            "has_active_ride": has_active_ride,
        },
    )

@login_required_role(allowed_roles=["admin"])
def admin_dashboard(request):
    user = User.objects.get(id=request.session['user_id'])
    return render(request, "admin_dashboard.html", {"user": user})



def customer_register(request):
    if request.method == "POST":
        name = request.POST.get("name")
        email = request.POST.get("email")
        phone = request.POST.get("phone")
        password = request.POST.get("password") 
        gender = request.POST.get("gender")

        # check duplicate email
        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered")
            return redirect("customer_register")

        # hash the password before saving
        hashed_password = make_password(password)

        user = User.objects.create(
            name=name,
            email=email.lower(),
            phone=phone,
            password=hashed_password,   # hashed value stored here
            gender=gender,
            role="customer",
            created_at=timezone.now()
        )

        messages.success(request, "Customer registered successfully. Please log in.")
        return redirect("login")

    return render(request, "customer_register.html")

from django.core.files.uploadedfile import UploadedFile


MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB - adjust if you want

@transaction.atomic
def driver_register(request):
    if request.method == "POST":
        # --- Personal / user fields ---
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        phone = request.POST.get("phone", "").strip()
        password = request.POST.get("password", "")
        gender = request.POST.get("gender", "")
        
        hashed_password = make_password(password)

        if not (name and email and phone and password):
            messages.error(request, "Please fill required personal fields.")
            return redirect("driver_register")

        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered")
            return redirect("driver_register")

        # create user instance and set password if supported
        user = User(
            name=name,
            email=email,
            phone=phone,
            password=hashed_password,
            gender=gender,
            role="driver",
            created_at=timezone.now()
        )
        
        user.save()

        # --- Driver fields ---
        license_number = request.POST.get("license_number", "").strip()
        try:
            experience_years = int(request.POST.get("experience_years") or 0)
        except (ValueError, TypeError):
            experience_years = 0

        profile_pic = request.FILES.get("profile_pic")
        id_proof = request.FILES.get("id_proof")

        driver = Driver.objects.create(
            user=user,
            license_number=license_number,
            experience_years=experience_years,
            profile_pic=profile_pic,
            id_proof=id_proof,
            verified=False,
            is_available=True
        )

        # --- Optional vehicle registration ---
        if request.POST.get("with_car"):
            vehicle_type = (request.POST.get("vehicle_type") or "").strip()
            make = (request.POST.get("make") or "").strip()
            model_name = (request.POST.get("model") or "").strip()
            reg_no = (request.POST.get("registration_number") or "").strip().upper()
            color = (request.POST.get("color") or "").strip()
            transmission = request.POST.get("transmission") or Vehicle.Transmission.MANUAL
            fuel_type = request.POST.get("fuel_type") or Vehicle.Fuel.PETROL

            # safe numeric parsing
            try:
                year_val = int(request.POST.get("year")) if request.POST.get("year") else None
            except (ValueError, TypeError):
                year_val = None

            try:
                seat_capacity = int(request.POST.get("seat_capacity") or 4)
            except (ValueError, TypeError):
                seat_capacity = 4

            try:
                per_km_rate = Decimal(request.POST.get("per_km_rate") or "0.00")
            except:
                per_km_rate = Decimal("0.00")

            try:
                per_min_rate = Decimal(request.POST.get("per_min_rate") or "0.00")
            except:
                per_min_rate = Decimal("0.00")

            ac = True if request.POST.get("ac") == "yes" else False

            # parse date fields YYYY-MM-DD -> date or None
            def parse_date(name):
                v = request.POST.get(name)
                if not v:
                    return None
                try:
                    return datetime.strptime(v, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    return None

            fitness_expiry = parse_date("fitness_cert_expiry")
            insurance_expiry = parse_date("insurance_expiry")
            permit_expiry = parse_date("permit_expiry")

            # required vehicle checks
            if not (vehicle_type and make and model_name and year_val and reg_no):
                messages.error(request, "Please fill required vehicle fields or uncheck 'Register with Car'.")
                # transaction will rollback due to exception or return
                return redirect("driver_register")

            # create Vehicle, catch duplicate registration_number
            try:
                vehicle = Vehicle.objects.create(
                    owner=user,
                    current_driver=driver,
                    vehicle_type=vehicle_type,
                    make=make,
                    model=model_name,
                    year=year_val,
                    color=color,
                    registration_number=reg_no,
                    seat_capacity=seat_capacity,
                    ac=ac,
                    transmission=transmission,
                    fuel_type=fuel_type,
                    per_km_rate=per_km_rate,
                    per_min_rate=per_min_rate,
                    fitness_cert_expiry=fitness_expiry,
                    insurance_expiry=insurance_expiry,
                    permit_expiry=permit_expiry,
                    verified=False
                )
            except IntegrityError:
                messages.error(request, "A vehicle with this registration number already exists.")
                return redirect("driver_register")

            # save multiple vehicle images
            uploaded_images = request.FILES.getlist("vehicle_images")
            primary_index = None
            try:
                primary_index = int(request.POST.get("primary_image_index")) if request.POST.get("primary_image_index") is not None else None
            except (ValueError, TypeError):
                primary_index = None

            saved_images = []
            for idx, uploaded in enumerate(uploaded_images):
                # basic validation: ensure UploadedFile and size limit
                if not isinstance(uploaded, UploadedFile):
                    continue
                if uploaded.size > MAX_IMAGE_SIZE:
                    # skip too-large files (or you might want to reject whole form)
                    continue
                # create image record
                is_primary = (primary_index is not None and idx == primary_index)
                vi = VehicleImage.objects.create(vehicle=vehicle, image=uploaded, is_primary=is_primary)
                saved_images.append(vi)

            # if none marked primary and at least one image exists, set first as primary
            if saved_images and not VehicleImage.objects.filter(vehicle=vehicle, is_primary=True).exists():
                first = saved_images[0]
                first.is_primary = True
                first.save()

        # success
        messages.success(request, "Driver registered successfully. Please wait for verification.")
        return redirect("/login/")

    # GET
    return render(request, "driver_register.html")



# accounts/views.py
from decimal import Decimal
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.utils import timezone
from django.contrib.auth.hashers import make_password

from .models import User, Driver


@login_required_role(allowed_roles=['customer'])
def customer_profile_view(request):
    uid = request.session.get('user_id')
    user = get_object_or_404(User, id=uid)

    # double-check session role vs DB role (extra safety)
    session_role = request.session.get('user_role')
    if user.role != session_role or user.role != "customer":
        messages.error(request, "Access denied.")
        return redirect("login")

    return render(request, "customer_profile.html", {"user": user})


@login_required_role(allowed_roles=['customer'])
def customer_profile_edit(request):
    uid = request.session.get('user_id')
    user = get_object_or_404(User, id=uid)

    session_role = request.session.get('user_role')
    if user.role != session_role or user.role != "customer":
        messages.error(request, "Access denied.")
        return redirect("login")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        gender = request.POST.get("gender") or ""
        language_preference = request.POST.get("language_preference", "en").strip()
        password = request.POST.get("password", "").strip()

        # Unique checks (exclude current user)
        if User.objects.exclude(id=user.id).filter(email=email).exists():
            messages.error(request, "Email already used by another account.")
            return redirect(reverse("customer_profile_edit"))

        if User.objects.exclude(id=user.id).filter(phone=phone).exists():
            messages.error(request, "Phone number already used by another account.")
            return redirect(reverse("customer_profile_edit"))

        # Apply updates
        user.name = name or user.name
        user.email = email or user.email
        user.phone = phone or user.phone
        user.gender = gender or user.gender
        user.language_preference = language_preference or user.language_preference

        if password:
            user.password = make_password(password)

        user.updated_at = timezone.now()
        user.save()

        messages.success(request, "Profile updated successfully.")
        return redirect(reverse("customer_profile"))

    return render(request, "customer_profile_edit.html", {"user": user})


# ----------------------------
# DRIVER
# ----------------------------
@login_required_role(allowed_roles=['driver'])
def driver_profile_view(request):
    uid = request.session.get('user_id')
    user = get_object_or_404(User, id=uid)

    session_role = request.session.get('user_role')
    if user.role != session_role or user.role != "driver":
        messages.error(request, "Access denied.")
        return redirect("login")

    try:
        driver = user.driver_profile
    except Driver.DoesNotExist:
        messages.error(request, "Driver profile missing. Complete registration first.")
        return redirect("/")  # change as needed

    return render(request, "driver_profile.html", {"user": user, "driver": driver})


@login_required_role(allowed_roles=['driver'])
def driver_profile_edit(request):
    uid = request.session.get('user_id')
    user = get_object_or_404(User, id=uid)

    session_role = request.session.get('user_role')
    if user.role != session_role or user.role != "driver":
        messages.error(request, "Access denied.")
        return redirect("login")

    try:
        driver = user.driver_profile
    except Driver.DoesNotExist:
        messages.error(request, "Driver profile missing.")
        return redirect("/")

    if request.method == "POST":
        # User fields
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        gender = request.POST.get("gender") or ""
        password = request.POST.get("password", "").strip()
        language_preference = request.POST.get("language_preference", "en").strip()

        # Driver fields
        license_number = request.POST.get("license_number", "").strip()
        license_expiry = request.POST.get("license_expiry", "").strip()
        experience_years = request.POST.get("experience_years", "").strip()
        day_fixed_charge = request.POST.get("day_fixed_charge", "").strip()
        night_fixed_charge = request.POST.get("night_fixed_charge", "").strip()
        night_start = request.POST.get("night_start", "").strip()
        night_end = request.POST.get("night_end", "").strip()

        # Unique checks excluding current user/driver
        if User.objects.exclude(id=user.id).filter(email=email).exists():
            messages.error(request, "Email already used by another account.")
            return redirect(reverse("driver_profile_edit"))

        if User.objects.exclude(id=user.id).filter(phone=phone).exists():
            messages.error(request, "Phone already used by another account.")
            return redirect(reverse("driver_profile_edit"))

        if license_number and Driver.objects.exclude(id=driver.id).filter(license_number=license_number).exists():
            messages.error(request, "License number already used by another driver.")
            return redirect(reverse("driver_profile_edit"))

        # Update user
        user.name = name or user.name
        user.email = email or user.email
        user.phone = phone or user.phone
        user.gender = gender or user.gender
        user.language_preference = language_preference or user.language_preference
        if password:
            user.password = make_password(password)
        user.save()

        # Update driver fields (safe parsing)
        if license_number:
            driver.license_number = license_number

        if license_expiry:
            try:
                driver.license_expiry = datetime.strptime(license_expiry, "%Y-%m-%d").date()
            except ValueError:
                messages.error(request, "Invalid license expiry date format (YYYY-MM-DD).")
                return redirect(reverse("driver_profile_edit"))

        if experience_years:
            try:
                driver.experience_years = int(experience_years)
            except ValueError:
                messages.error(request, "Invalid experience years (integer expected).")
                return redirect(reverse("driver_profile_edit"))


        try:
            if day_fixed_charge:
                driver.day_fixed_charge = Decimal(day_fixed_charge)
            if night_fixed_charge:
                driver.night_fixed_charge = Decimal(night_fixed_charge)
        except Exception:
            messages.error(request, "Invalid fixed charge values.")
            return redirect(reverse("driver_profile_edit"))

        try:
            if night_start:
                driver.night_start = datetime.strptime(night_start, "%H:%M").time()
            if night_end:
                driver.night_end = datetime.strptime(night_end, "%H:%M").time()
        except ValueError:
            messages.error(request, "Invalid time format for night window (HH:MM).")
            return redirect(reverse("driver_profile_edit"))

        # Files
        profile_pic = request.FILES.get("profile_pic")
        id_proof = request.FILES.get("id_proof")
        if profile_pic:
            driver.profile_pic = profile_pic
        if id_proof:
            driver.id_proof = id_proof

        driver.save()
        messages.success(request, "Driver profile updated.")
        return redirect(reverse("driver_profile"))

    return render(request, "driver_profile_edit.html", {"user": user, "driver": driver})



@login_required_role(allowed_roles=["driver"])
def driver_requests_list(request):
    uid = request.session.get("user_id")
    driver = get_object_or_404(DriverModel, user__pk=uid)

    # pending requests only (most recent first)
    requests_qs = RideRequest.objects.filter(driver=driver).select_related(
        "ride", "ride__customer", "ride__purpose", "ride__vehicle"
    ).order_by("-requested_at")

    # check if driver already has an active ride
    has_active_ride = RideRequest.objects.filter(
        driver=driver,
        status=RideRequest.Status.ACCEPTED,
        ride__status__in=[Ride.Status.REQUESTED, Ride.Status.ACCEPTED, Ride.Status.ONGOING],
    ).exists()

    context = {
        "driver": driver,
        "ride_requests": requests_qs,
        "has_active_ride": has_active_ride,
    }
    return render(request, "ride_requests_list.html", context)

from payments.models import Payment
@login_required_role(allowed_roles=["driver"])
def driver_request_detail(request, pk):
    uid = request.session.get("user_id")
    driver = get_object_or_404(DriverModel, user__pk=uid)

    ride_request = get_object_or_404(RideRequest.objects.select_related(
        "ride", "ride__customer", "ride__purpose", "ride__vehicle"
    ), pk=pk)

    # Ensure this request belongs to the logged-in driver
    if ride_request.driver_id != driver.id:
        return HttpResponseForbidden("You are not allowed to view this request.")

    # Fetch payment details for the ride
    payment = Payment.objects.filter(
        Q(ride=ride_request.ride) & Q(status__in=[Payment.Status.SUCCESS, Payment.Status.PENDING, Payment.Status.FAILED, Payment.Status.REFUNDED])
    ).select_related("customer").first()

    context = {
        "driver": driver,
        "ride_request": ride_request,
        "payment": payment
    }
    return render(request, "ride_request_detail.html", context)


@login_required_role(allowed_roles=["driver"])
def accept_ride_request(request, pk):
    if request.method != "POST":
        messages.error(request, "Invalid method.")
        return redirect("driver_requests_list")

    uid = request.session.get("user_id")
    driver = get_object_or_404(DriverModel, user__pk=uid)

    ride_request = get_object_or_404(RideRequest.objects.select_related("ride"), pk=pk)
    if ride_request.driver_id != driver.id:
        return HttpResponseForbidden("You are not allowed to accept this request.")

    with transaction.atomic():
        # lock the ride row
        ride = Ride.objects.select_for_update().get(pk=ride_request.ride.pk)
        # re-lock the ride_request row
        ride_request = (
            RideRequest.objects.select_for_update()
            .select_related("driver", "ride")
            .get(pk=pk)
        )

        # ✅ extra check: prevent driver from having multiple active rides
        active_exists = RideRequest.objects.filter(
            driver=driver,
            status=RideRequest.Status.ACCEPTED,
            ride__status__in=[Ride.Status.REQUESTED, Ride.Status.ACCEPTED, Ride.Status.ONGOING],
        ).exclude(pk=ride_request.pk).exists()

        if active_exists:
            messages.error(request, "You already have an active ride. Complete it before accepting another.")
            return redirect("driver_requests_list")

        # check ride status
        if ride.status != Ride.Status.REQUESTED:
            messages.error(request, "This ride is no longer available (already accepted/cancelled).")
            return redirect("driver_requests_list")

        if ride_request.status != RideRequest.Status.PENDING:
            messages.error(request, "This request is no longer pending.")
            return redirect("driver_requests_list")

        # --- VEHICLE handling for CAR_WITH_DRIVER mode ---
        vehicle = None
        if ride.ride_mode == Ride.Mode.CAR_WITH_DRIVER:
            vehicle_id = request.POST.get("vehicle_id")
            if vehicle_id:
                # lock the vehicle row to avoid race conditions
                try:
                    vehicle = Vehicle.objects.select_for_update().get(pk=vehicle_id)
                except Vehicle.DoesNotExist:
                    messages.error(request, "Selected vehicle not found.")
                    return redirect("driver_requests_list")

                # ensure this vehicle is actually assigned to this driver
                if vehicle.current_driver_id != driver.id:
                    messages.error(request, "Selected vehicle is not assigned to you.")
                    return redirect("driver_requests_list")

                # optional checks: active/verified
                if not vehicle.active:
                    messages.error(request, "Selected vehicle is not active.")
                    return redirect("driver_requests_list")
                if not vehicle.verified:
                    messages.error(request, "Selected vehicle is not verified.")
                    return redirect("driver_requests_list")
            else:
                # try to auto-select the driver's currently assigned active & verified vehicle
                vehicle = (
                    Vehicle.objects.select_for_update()
                    .filter(current_driver=driver, active=True, verified=True)
                    .first()
                )
                if not vehicle:
                    messages.error(
                        request,
                        "No active/verified vehicle assigned to you. Please select a vehicle to accept this ride."
                    )
                    return redirect("driver_requests_list")

        ride_request.status = RideRequest.Status.ACCEPTED
        ride_request.responded_at = timezone.now()
        ride_request.save()

        # assign driver to ride (and vehicle if needed)
        ride.driver = driver
        if vehicle:
            ride.vehicle = vehicle
        else:
            # ensure we don't accidentally keep some old vehicle if this is DRIVER_ONLY
            if ride.ride_mode == Ride.Mode.DRIVER_ONLY:
                ride.vehicle = None
        ride.status = Ride.Status.ACCEPTED
        ride.save()

        # cancel other pending requests for same ride
        now = timezone.now()
        RideRequest.objects.filter(ride=ride).exclude(pk=ride_request.pk).filter(
            status=RideRequest.Status.PENDING
        ).update(status=RideRequest.Status.AUTO_CANCELLED, responded_at=now)

    messages.success(request, "Ride accepted. Other driver requests have been cancelled.")
    return redirect(reverse("driver_request_detail", args=[ride_request.pk]))


def calculate_distance_osrm(lat1: float, lon1: float, lat2: float, lon2: float, timeout=5):
    """
    Query the OSRM demo server for driving distance & duration.
    Returns (distance_km, duration_min, source) or (None, None, None) on failure.
    """
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
        params = {"overview": "false", "alternatives": "false", "steps": "false"}
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            distance_km = float(route["distance"]) / 1000.0
            duration_min = float(route["duration"]) / 60.0  # seconds → minutes
            return distance_km, duration_min, "osrm"
        return None, None, None
    except Exception:
        return None, None, None


@require_GET
@login_required_role(allowed_roles=["driver"])
def ride_request_distance(request, pk):
    """
    JSON endpoint returning distance & duration for a RideRequest.
    """
    ride_request = get_object_or_404(RideRequest.objects.select_related("ride"), pk=pk)
    ride = ride_request.ride

    lat1, lon1 = ride.start_latitude, ride.start_longitude
    lat2, lon2 = ride.end_latitude, ride.end_longitude

    if None in (lat1, lon1, lat2, lon2):
        return JsonResponse(
            {"status": "error", "message": "Missing coordinates"},
            status=400,
        )

    try:
        lat1f, lon1f, lat2f, lon2f = map(float, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return JsonResponse(
            {"status": "error", "message": "Invalid coordinate values"},
            status=400,
        )

    # Try OSRM
    dist_km, duration_min, source = calculate_distance_osrm(lat1f, lon1f, lat2f, lon2f)
    if dist_km is None:
        # fallback: haversine for distance, no realistic duration (you could estimate avg speed)
        dist_km = haversine_distance(lat1f, lon1f, lat2f, lon2f)
        duration_min = dist_km / 40 * 60  # assume 40 km/h avg if no OSRM (very rough)
        source = "haversine"

    return JsonResponse({
        "status": "ok",
        "distance_km": round(dist_km, 2),
        "duration_min": round(duration_min, 1),
        "source": source,
    })
    
    
    

from django.utils import timezone
from django.core.exceptions import ValidationError




@login_required_role(allowed_roles=["driver"])
def set_ride_request_ongoing(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        # Get driver from session
        uid = request.session.get("user_id")
        if not uid:
            return JsonResponse({'error': 'User not authenticated'}, status=401)

        driver = get_object_or_404(DriverModel, user__pk=uid)
        ride_request = get_object_or_404(RideRequest, pk=pk, driver=driver)

        # Check statuses
        if not hasattr(ride_request, 'status') or not hasattr(ride_request.ride, 'status'):
            return JsonResponse({'error': 'Invalid ride or request configuration'}, status=500)

        if ride_request.status != 'accepted':  # Adjust if RideRequest.Status.ACCEPTED differs
            raise ValidationError("Ride request must be in 'accepted' status to set to ongoing.")

        ride = ride_request.ride
        if not ride:
            raise ValidationError("No ride associated with this request.")

        if ride.status != Ride.Status.ACCEPTED:
            raise ValidationError("Associated ride must be in 'accepted' status.")

        # Update statuses
        ride_request.status = 'completed'
        ride.status = Ride.Status.ONGOING
        if not ride.start_time:
            ride.start_time = timezone.now()

        ride_request.save()
        ride.save()

        return JsonResponse({
            'success': True,
            'message': 'Done',
            'ride_status': ride.get_status_display(),
            'request_status Nana': ride_request.get_status_display()
        })
    except ValidationError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except RideRequest.DoesNotExist:
        return JsonResponse({'error': 'Ride request not found or not assigned to you.'}, status=404)
    except DriverModel.DoesNotExist:
        return JsonResponse({'error': 'Driver not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'error': 'An unexpected error occurred.'}, status=500)

@login_required_role(allowed_roles=["driver"])
def end_ride_request(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)

    try:
        uid = request.session.get("user_id")
        if not uid:
            return JsonResponse({'error': 'User not authenticated'}, status=401)

        driver = get_object_or_404(DriverModel, user__pk=uid)
        ride_request = get_object_or_404(RideRequest, pk=pk, driver=driver)

        ride = ride_request.ride
        if not ride:
            raise ValidationError("No ride associated with this request.")

        if ride_request.status != 'completed':  # Adjust if RideRequest.Status.ONGOING differs
            raise ValidationError("Ride request must be in 'completed' status to end.")
        
        if ride.end_time:
            raise ValidationError("Ride has already been ended.")

        # Parse form data
        additional_charges_str = request.POST.get('additional_charges', '0')
        return_trip = request.POST.get('return_trip', 'false').lower() == 'true'

        try:
            additional_charges = Decimal(additional_charges_str)
            if additional_charges < 0:
                raise ValueError("Additional charges cannot be negative.")
        except Exception as e:
            raise ValidationError("Invalid additional charges value.")

        if not (ride.start_latitude and ride.start_longitude and ride.end_latitude and ride.end_longitude):
            raise ValidationError("Start and end coordinates are required to calculate distance and duration.")

        distance_km, duration_min, _ = calculate_distance_osrm(
            float(ride.start_latitude), float(ride.start_longitude),
            float(ride.end_latitude), float(ride.end_longitude)
        )
        if distance_km is None or duration_min is None:
            raise ValidationError("Failed to calculate distance and duration using OSRM.")

        ride.actual_distance_km = Decimal(str(distance_km)).quantize(Decimal('0.01'))
        ride.actual_duration_min = int(duration_min)
        ride.end_time = timezone.now()

        # Calculate fare as per Ride model
        ride.calculate_fare()

        # Apply return trip doubling
        if return_trip:
            if ride.base_fare is None:
                raise ValidationError("Base fare must be calculated before doubling for return trip.")
            ride.base_fare *= Decimal('2')

        # Add additional charges
        ride.base_fare = (ride.base_fare or Decimal('0')) + additional_charges

        # Recalculate tax and total
        ride.tax_amount = ride.base_fare * Decimal('0.05')
        ride.total_amount = ride.base_fare + ride.tax_amount - ride.discount_amount

        ride.save()
        ride_request.save()

        return JsonResponse({
            'success': True,
            'message': 'Ride ended. Payment pending. Status remains ongoing until payment success.',
            'total_amount': str(ride.total_amount),
        })
    except ValidationError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except RideRequest.DoesNotExist:
        return JsonResponse({'error': 'Ride request not found or not assigned to you.'}, status=404)
    except DriverModel.DoesNotExist:
        return JsonResponse({'error': 'Driver not found.'}, status=404)
    except Exception as e:
        return JsonResponse({'error': 'An unexpected error occurred.'}, status=500)
    
@login_required_role(allowed_roles=["driver"])
@require_http_methods(["POST"])
def api_toggle_driver_availability(request):
    """
    POST JSON body (optional):
      { "is_available": true|false }   # if omitted, the view will toggle the current value

    Response:
      { "success": True, "is_available": true|false }
    """
    # fetch current user from session (matches your driver_dashboard pattern)
    user_id = request.session.get("user_id")
    if not user_id:
        return HttpResponseForbidden(json.dumps({"error": "Not authenticated"}), content_type="application/json")

    user = get_object_or_404(User, id=user_id)

    # ensure user has a driver profile
    try:
        driver = user.driver_profile
    except Driver.DoesNotExist:
        return JsonResponse({"error": "Driver profile not found"}, status=404)

    # parse JSON body (if any)
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return HttpResponseBadRequest(json.dumps({"error": "Invalid JSON"}), content_type="application/json")

    # allow explicit set or toggle if not provided
    if "is_available" in body:
        # coerce to bool safely
        new_state = bool(body["is_available"])
    else:
        new_state = not bool(driver.is_available)

    # update and save
    driver.is_available = new_state
    driver.save(update_fields=["is_available"])

    return JsonResponse({"success": True, "is_available": driver.is_available})
