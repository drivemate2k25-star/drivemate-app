from django.shortcuts import get_object_or_404, render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q

from payments.models import Payment
from .models import Ride, RideRequest, RidePurpose, Rating
from accounts.models import Driver
from vehicles.models import Vehicle
from accounts.views import login_required_role
from django.utils import timezone
from decimal import Decimal
import math
import json
from .utils import haversine_distance
from django.db import transaction


from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django import forms
from django.db.models import Avg

from django.utils import timezone
import datetime as _time

@login_required_role(['customer'])
def create_ride(request):
    if request.method == 'POST':
        try:
            # Extract form data
            ride_mode = request.POST.get('ride_mode', Ride.Mode.CAR_WITH_DRIVER)
            start_location = request.POST.get('start_location')
            start_lat = Decimal(request.POST.get('start_latitude'))
            start_lon = Decimal(request.POST.get('start_longitude'))
            end_location = request.POST.get('end_location')
            end_lat = Decimal(request.POST.get('end_latitude'))
            end_lon = Decimal(request.POST.get('end_longitude'))
            female_driver = request.POST.get('female_driver', 'false') == 'true'
            purpose_id = request.POST.get('purpose')
            notes= request.POST.get('notes')
            
            # Validate required fields
            if not all([start_location, start_lat, start_lon, end_location, end_lat, end_lon]):
                messages.error(request, "Please select both start and end locations.")
                return redirect('create_ride')

            # Create ride
            ride = Ride.objects.create(
                customer_id=request.session.get('user_id'),
                ride_mode=ride_mode,
                start_location=start_location,
                start_latitude=start_lat,
                start_longitude=start_lon,
                end_location=end_location,
                end_latitude=end_lat,
                end_longitude=end_lon,
                female_driver_preference=female_driver,
                purpose_id=purpose_id if purpose_id else None,
                notes = notes,
                status=Ride.Status.REQUESTED
            )
            
            # Redirect to driver selection
            return redirect('select_driver', ride_id=ride.id)
            
        except ValueError as e:
            messages.error(request, f"Invalid input: {str(e)}")
            return redirect('create_ride')
        except Exception as e:
            messages.error(request, f"Error creating ride: {str(e)}")
            return redirect('create_ride')

    # GET request - render form
    default_mode = request.GET.get('mode', Ride.Mode.CAR_WITH_DRIVER)
    if default_mode not in dict(Ride.Mode.choices):
        default_mode = Ride.Mode.CAR_WITH_DRIVER
    purposes = RidePurpose.objects.all()
    return render(request, 'create_ride.html', {
        'purposes': purposes,
        'ride_modes': Ride.Mode.choices,
        'default_mode': default_mode
    })


@login_required_role(['customer'])
def select_driver(request, ride_id):
    DESIRED_RESULTS = 20  # change this if you want more/less
    try:
        ride = Ride.objects.get(id=ride_id, customer_id=request.session.get('user_id'))
    except Ride.DoesNotExist:
        messages.error(request, "Ride not found or you don't have permission.")
        return redirect('home')

    if request.method == 'POST':
        driver_id = request.POST.get('driver_id')
        if driver_id:
            try:
                driver = Driver.objects.get(id=driver_id)

                # ✅ Check if request already exists (exclude rejected)
                existing_request = RideRequest.objects.filter(
                    ride=ride, driver=driver
                ).exclude(status=RideRequest.Status.REJECTED).first()

                if existing_request:
                    messages.warning(request, f"You already requested driver {driver.user.name}.")
                else:
                    RideRequest.objects.create(
                        ride=ride,
                        driver=driver,
                        status=RideRequest.Status.PENDING
                    )
                    messages.success(request, f"Request sent to driver {driver.user.name}")

            except Driver.DoesNotExist:
                messages.error(request, "Invalid driver selected.")
            except Exception as e:
                messages.error(request, f"Error sending request: {str(e)}")
            return redirect('select_driver', ride_id=ride.id)

    # GET request - show available drivers (with fallbacks)
    vehicle_type = request.GET.get('vehicle_type')
    transmission = request.GET.get('transmission')
    fuel_type = request.GET.get('fuel_type')
    min_rating = request.GET.get('min_rating', '0')
    if min_rating == '':  # Error-proof: treat empty as 0
        min_rating = '0'

    requested_driver_ids = set(
        RideRequest.objects.filter(ride=ride).values_list("driver_id", flat=True)
    )

    def compute_distance_or_inf(src_lat, src_lon, dst_lat, dst_lon):
        try:
            if dst_lat is None or dst_lon is None:
                return float('inf')
            return haversine_distance(src_lat, src_lon, dst_lat, dst_lon)
        except Exception:
            return float('inf')

    if ride.ride_mode == Ride.Mode.DRIVER_ONLY:
        # Build base_qs with user filters
        base_qs = Driver.objects.select_related('user')
        if ride.female_driver_preference:
            base_qs = base_qs.filter(user__gender='female')
        if min_rating:
            try:
                base_qs = base_qs.filter(rating__gte=float(min_rating))
            except ValueError:
                messages.error(request, "Invalid minimum rating value.")
                return redirect('select_driver', ride_id=ride.id)

        # Strict: add hard constraints
        strict_qs = base_qs.filter(
            is_available=True,
            verified=True,
            background_check_passed=True
        )

        driver_list = []
        for driver in strict_qs:
            distance = compute_distance_or_inf(
                ride.start_latitude, ride.start_longitude,
                driver.latitude, driver.longitude
            )
            driver.distance = distance
            driver.already_requested = driver.id in requested_driver_ids
            driver_list.append(driver)

        # If we have fewer than desired, add extra drivers relaxing hard filters (but keeping user filters)
        if len(driver_list) < DESIRED_RESULTS:
            existing_ids = [d.id for d in driver_list]
            needed = DESIRED_RESULTS - len(driver_list)

            # Fetch extras from base_qs (user filters applied), exclude existing, order by rating
            extra_qs = base_qs.exclude(id__in=existing_ids).order_by('-rating')[:needed]
            for driver in extra_qs:
                distance = compute_distance_or_inf(
                    ride.start_latitude, ride.start_longitude,
                    driver.latitude, driver.longitude
                )
                driver.distance = distance
                driver.already_requested = driver.id in requested_driver_ids
                driver_list.append(driver)

        # Final sort by distance (closest first)
        sorted_drivers = sorted(driver_list, key=lambda x: x.distance)

        # Normalize ride_mode to a lowercase string so template checks work
        try:
            ride_mode_value = ride.ride_mode.name.lower()
        except Exception:
            ride_mode_value = str(ride.ride_mode).lower()

        context = {
            'ride': ride,
            'drivers': sorted_drivers[:DESIRED_RESULTS],
            'ride_mode': ride_mode_value,
            'vehicle_types': Vehicle.VehicleType.choices,
            'transmissions': Vehicle.Transmission.choices,
            'fuel_types': Vehicle.Fuel.choices,
            'filters': request.GET,  # expose GET params to the template
        }

    else:  # CAR_WITH_DRIVER
        # Build base_qs with user filters
        base_qs = Vehicle.objects.select_related('current_driver__user')
        if ride.female_driver_preference:
            base_qs = base_qs.filter(current_driver__user__gender='female')
        if vehicle_type:
            base_qs = base_qs.filter(vehicle_type=vehicle_type)
        if transmission:
            base_qs = base_qs.filter(transmission=transmission)
        if fuel_type:
            base_qs = base_qs.filter(fuel_type=fuel_type)
        if min_rating:
            try:
                base_qs = base_qs.filter(current_driver__rating__gte=float(min_rating))
            except ValueError:
                messages.error(request, "Invalid minimum rating value.")
                return redirect('select_driver', ride_id=ride.id)

        # Strict: add hard constraints
        strict_qs = base_qs.filter(
            current_driver__is_available=True,
            current_driver__verified=True,
            current_driver__background_check_passed=True,
            active=True,
            verified=True
        )

        vehicle_list = []
        for vehicle in strict_qs:
            driver = vehicle.current_driver
            distance = compute_distance_or_inf(
                ride.start_latitude, ride.start_longitude,
                getattr(driver, 'latitude', None), getattr(driver, 'longitude', None)
            )
            vehicle.distance = distance
            vehicle.already_requested = driver.id in requested_driver_ids
            vehicle.driver = driver
            vehicle_list.append(vehicle)

        # If fewer than desired, fetch additional vehicles relaxing hard constraints (but keeping user filters)
        if len(vehicle_list) < DESIRED_RESULTS:
            existing_vehicle_ids = [v.id for v in vehicle_list]
            needed = DESIRED_RESULTS - len(vehicle_list)

            # Fetch extras from base_qs (user filters applied), relax hard, exclude existing, order by driver rating
            extra_qs = base_qs.filter(active=True).exclude(id__in=existing_vehicle_ids).order_by('-current_driver__rating')[:needed]
            for vehicle in extra_qs:
                driver = vehicle.current_driver
                distance = compute_distance_or_inf(
                    ride.start_latitude, ride.start_longitude,
                    getattr(driver, 'latitude', None), getattr(driver, 'longitude', None)
                )
                vehicle.distance = distance
                vehicle.already_requested = (driver.id in requested_driver_ids) if driver else False
                vehicle.driver = driver
                vehicle_list.append(vehicle)

        sorted_vehicles = sorted(vehicle_list, key=lambda x: x.distance)

        # Normalize ride_mode to a lowercase string so template checks work
        try:
            ride_mode_value = ride.ride_mode.name.lower()
        except Exception:
            ride_mode_value = str(ride.ride_mode).lower()

        context = {
            'ride': ride,
            'vehicles': sorted_vehicles[:DESIRED_RESULTS],
            'ride_mode': ride_mode_value,
            'vehicle_types': Vehicle.VehicleType.choices,
            'transmissions': Vehicle.Transmission.choices,
            'fuel_types': Vehicle.Fuel.choices,
            'filters': request.GET,  # expose GET params to the template
        }

    return render(request, 'select_driver.html', context)


@login_required_role(['customer'])
def get_driver_details(request, driver_id):
    try:
        driver = Driver.objects.get(id=driver_id, is_available=True, verified=True)
        
        data = {
            'name': driver.user.name,
            'rating': driver.rating,
            'experience_years': driver.experience_years,
            'license_number': driver.license_number,
            'profile_pic': driver.profile_pic.url if driver.profile_pic else None,
        }
        return JsonResponse(data)
    except Driver.DoesNotExist:
        return JsonResponse({'error': 'Driver not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': f'Error fetching driver details: {str(e)}'}, status=500)
    



@login_required_role(['customer'])
def my_trips(request):
    """
    Show a paginated list (simple) of the customer's rides with quick actions.
    """
    customer_id = request.session.get('user_id')
    rides = Ride.objects.filter(customer_id=customer_id).order_by('-created_at')

    # optional: simple status filter from querystring
    status = request.GET.get('status')
    if status:
        rides = rides.filter(status=status)

    context = {
        'rides': rides,
        'status_choices': Ride.Status.choices,
    }
    return render(request, 'my_trips.html', context)

from vehicles.models import VehicleImage

@login_required_role(['customer'])
def trip_detail(request, ride_id):
    """
    Show ride details and driver requests. Allow cancelling or reopening (try another driver).
    """
    customer_id = request.session.get('user_id')
    ride = get_object_or_404(Ride, id=ride_id, customer_id=customer_id)
    payment = Payment.objects.filter(
        Q(ride=ride) & Q(status__in=[Payment.Status.SUCCESS, Payment.Status.PENDING, Payment.Status.FAILED, Payment.Status.REFUNDED])
    ).select_related("customer").first()

    # POST actions
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            with transaction.atomic():
                if action == 'cancel_ride':
                    # Customer cancels the ride
                    ride.status = Ride.Status.CANCELLED
                    ride.driver = None
                    ride.vehicle = None
                    ride.updated_at = timezone.now()
                    ride.save()
                    # mark pending requests as auto-cancelled
                    RideRequest.objects.filter(ride=ride, status=RideRequest.Status.PENDING).update(
                        status=RideRequest.Status.AUTO_CANCELLED, responded_at=timezone.now()
                    )
                    messages.success(request, "Ride cancelled.")
                    return redirect('my_trips')

                elif action == 'reopen_ride':
                    # Reopen the ride so customer can pick another driver
                    ride.status = Ride.Status.REQUESTED
                    ride.driver = None
                    ride.vehicle = None
                    ride.updated_at = timezone.now()
                    ride.save()
                    # mark previously accepted request (if any) as auto_cancelled
                    RideRequest.objects.filter(ride=ride, status=RideRequest.Status.ACCEPTED).update(
                        status=RideRequest.Status.AUTO_CANCELLED, responded_at=timezone.now()
                    )
                    messages.success(request, "Ride reopened. Please choose another driver.")
                    return redirect('select_driver', ride_id=ride.id)

                # optionally support deleting an individual driver request from customer's side
                elif action == 'close_request':
                    rr_id = request.POST.get('request_id')
                    rr = RideRequest.objects.filter(id=rr_id, ride=ride).first()
                    if rr and rr.status == RideRequest.Status.PENDING:
                        rr.status = RideRequest.Status.AUTO_CANCELLED
                        rr.responded_at = timezone.now()
                        rr.save()
                        messages.success(request, "Request closed.")
                    else:
                        messages.error(request, "Cannot close that request.")
                    return redirect('trip_detail', ride_id=ride.id)

        except Exception as e:
            messages.error(request, f"Error performing action: {str(e)}")
            return redirect('trip_detail', ride_id=ride.id)

    # GET -> show ride, requests
    requests_qs = ride.driver_requests.select_related('driver__user').order_by('-requested_at')

    # ---- New: gather vehicle images (primary + gallery) without changing other data passing ----
    vehicle_primary_image = None
    vehicle_images = []

    if ride.vehicle:
        # order so primary images come first
        imgs_qs = ride.vehicle.images.order_by('-is_primary', 'uploaded_at')
        # collect only valid image URLs
        for img in imgs_qs:
            try:
                if img.image and hasattr(img.image, 'url'):
                    vehicle_images.append(img.image.url)
            except Exception:
                # ignore images that fail to provide a URL for any reason
                continue

        if vehicle_images:
            # determine primary: prefer an image marked is_primary, otherwise first in list
            primary = imgs_qs.filter(is_primary=True).first() if hasattr(imgs_qs, 'filter') else None
            if primary and getattr(primary.image, 'url', None):
                vehicle_primary_image = primary.image.url
            else:
                vehicle_primary_image = vehicle_images[0]

    context = {
        'ride': ride,
        'driver_requests': requests_qs,
        'ride_modes': Ride.Mode.choices,
        'payment': payment,
        # newly added (safe, optional)
        'vehicle_primary_image': vehicle_primary_image,
        'vehicle_images': vehicle_images,
    }
    return render(request, 'trip_detail.html', context)


class RatingForm(forms.ModelForm):
    score = forms.IntegerField(min_value=1, max_value=5, initial=5)

    class Meta:
        model = Rating
        fields = ['score', 'feedback']

# View for Customer to Rate a Ride (Driver and Vehicle)
@login_required_role(allowed_roles=['customer'])
def rate_ride(request, ride_id):
    ride = get_object_or_404(Ride, pk=ride_id, customer__id=request.session.get('user_id'), status=Ride.Status.COMPLETED)
    
    # Check if rating already exists
    if Rating.objects.filter(ride=ride).exists():
        messages.error(request, "You have already rated this ride.")
        return redirect('trip_detail', ride_id=ride_id)
    
    if request.method == 'POST':
        form = RatingForm(request.POST)
        if form.is_valid():
            rating = form.save(commit=False)
            rating.ride = ride
            rating.customer_id = request.session.get('user_id')
            rating.driver = ride.driver
            rating.vehicle = ride.vehicle
            rating.save()
            
            # Update driver's average rating
            if ride.driver:
                driver_ratings = Rating.objects.filter(driver=ride.driver)
                if driver_ratings.exists():
                    avg_driver_rating = driver_ratings.aggregate(Avg('score'))['score__avg']
                    ride.driver.rating = avg_driver_rating
                    ride.driver.save()
            
            # Update vehicle's average rating (assuming Vehicle has a 'rating' field)
            if ride.vehicle:
                vehicle_ratings = Rating.objects.filter(vehicle=ride.vehicle)
                if vehicle_ratings.exists():
                    avg_vehicle_rating = vehicle_ratings.aggregate(Avg('score'))['score__avg']
                    ride.vehicle.rating = avg_vehicle_rating
                    ride.vehicle.save()
            
            messages.success(request, "Rating submitted successfully.")
            return redirect('my_trips')  # Redirect after success
    else:
        form = RatingForm()
    
    context = {
        'form': form,
        'ride': ride,
    }
    return render(request, 'rate_ride.html', context)

# View for Customer to View Driver Rating
@login_required_role(allowed_roles=['customer'])
def view_driver_rating(request, driver_id):
    driver = get_object_or_404(Driver, pk=driver_id)
    ratings = Rating.objects.filter(driver=driver)
    avg_rating = ratings.aggregate(Avg('score'))['score__avg'] if ratings.exists() else 0.0
    context = {
        'driver': driver,
        'avg_rating': avg_rating,
        'ratings': ratings.order_by('-created_at'),  # List individual ratings if needed
    }
    return render(request, 'view_driver_rating.html', context)


