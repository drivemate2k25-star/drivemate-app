from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.http import HttpResponseForbidden

from rides.models import Ride, RideRequest
from accounts.models import Driver as DriverModel  
from accounts.views import login_required_role  

