import datetime
import json
 
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, HttpResponseForbidden
from django.contrib import auth
from django.contrib.auth import authenticate, login
  
# The basic auth decorator
import base64
import functools
def login_or_basic_auth_required(view):
	# Views wrapped here are called by ingest scripts and crons; the onboarding
	# gate (YSE_App/onboarding.py) never redirects them (#630).
	@functools.wraps(view)
	def _decorator(request, *args, **kwargs):
		if 'META' in request.__dict__.keys() and 'HTTP_AUTHORIZATION' in request.META.keys():
			auth_method, credentials = request.META['HTTP_AUTHORIZATION'].split(' ', 1)
			if auth_method.lower() == 'basic':
				credentials = base64.b64decode(credentials.strip()).decode('utf-8')
				username, password = credentials.split(':', 1)
				user = auth.authenticate(username=username, password=password)
				if user is not None and user.is_active:
					# Correct password, and the user is marked "active"
					request.user = user
					return view(request, *args, **kwargs)
				else:
					return HttpResponseForbidden('Incorrect user credentials.')
			response = HttpResponse()
			response.status_code = 401
			response['WWW-Authenticate'] = 'Basic'
			return response
		elif 'request' in request.__dict__.keys() and 'HTTP_AUTHORIZATION' in request.request.META.keys():
			auth_method, credentials = request.request.META['HTTP_AUTHORIZATION'].split(' ', 1)
			if auth_method.lower() == 'basic':
				credentials = base64.b64decode(credentials.strip()).decode('utf-8')
				username, password = credentials.split(':', 1)
				user = auth.authenticate(username=username, password=password)
				if user is not None and user.is_active:
					# Correct password, and the user is marked "active"
					request.request.user = user
					return view(request, *args, **kwargs)
				else:
					return HttpResponseForbidden('Incorrect user credentials.')
			response = HttpResponse()
			response.status_code = 401
			response['WWW-Authenticate'] = 'Basic'
			return response			
		else:
			if 'user' in request.__dict__.keys() and request.user.is_authenticated:
				return view(request, *args, **kwargs)
			elif 'request' in request.__dict__.keys() and request.request.user.is_authenticated:
				return view(request, *args, **kwargs)
			else:
				return HttpResponseForbidden('Incorrect user credentials')
	_decorator.onboarding_exempt = True
	# functools.wraps replaces __qualname__, so LoginRequiredMiddleware (#496)
	# recognises these views by this marker (YSE_App/login_required.py).
	_decorator.login_or_basic_auth = True
	return _decorator


