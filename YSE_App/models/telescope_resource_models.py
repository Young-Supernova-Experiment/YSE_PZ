from django.db import models
from YSE_App.models.base import *
from YSE_App.models.enum_models import *
from YSE_App.models.telescope_models import *
from YSE_App.models.principal_investigator_models import *
import datetime
from django.utils import timezone
from astropy.coordinates import EarthLocation
from astropy.coordinates import get_moon, SkyCoord
from astropy.time import Time
import astropy.units as u
from django.contrib.auth.models import Group

class TelescopeResource(BaseModel):
	class Meta:
		abstract = True

	### Entity relationships ###
	# Required
	telescope = models.ForeignKey(Telescope, on_delete=models.CASCADE)
	# Optional
	principal_investigator = models.ForeignKey(PrincipalInvestigator, null=True, blank=True, on_delete=models.SET_NULL)
	groups = models.ManyToManyField(Group, blank=True)

	### Properties ###
	# Required
	begin_date_valid = models.DateTimeField() # i.e. the "semester" start
	end_date_valid = models.DateTimeField() # i.e. the "semester" start

	# Optional
	description = models.TextField(null=True, blank=True)
	# When True, follow-ups may use empty audience (requester-only); see audience.resource_is_creator_only
	creator_only = models.BooleanField(default=False)
	# Facility binding (#304): the Allocation that holds the facility API slug, the encrypted
	# credential and the default request parameters for this resource.
	allocation = models.ForeignKey(
		'YSE_App.Allocation', null=True, blank=True, on_delete=models.SET_NULL, related_name='%(class)s_resources',
		help_text='Allocation with the facility API, credentials and default request parameters (#304).',
	)

	@property
	def facility_api(self):
		return self.allocation.facility if self.allocation_id else ''

	@property
	def has_credential(self):
		return bool(self.allocation_id and self.allocation.has_credential)

	@property
	def default_request_params(self):
		return dict(self.allocation.default_request_params or {}) if self.allocation_id else {}


class ToOResource(TelescopeResource):
	### Properites ###
	# Required
	awarded_too_hours = models.FloatField(null=True, blank=True)
	used_too_hours = models.FloatField(null=True, blank=True)

	awarded_too_triggers = models.FloatField(null=True, blank=True)
	used_too_triggers = models.FloatField(null=True, blank=True)

	@property
	def remaining_hours(self):
		return None if self.awarded_too_hours is None else round(self.awarded_too_hours - (self.used_too_hours or 0.0), 3)

	@property
	def remaining_triggers(self):
		return None if self.awarded_too_triggers is None else round(self.awarded_too_triggers - (self.used_too_triggers or 0.0), 3)

	def __str__(self):
		return "ToO Resource: %s; Valid: %s to %s" % (self.telescope.name, self.begin_date_valid.strftime('%m/%d/%Y'), self.end_date_valid.strftime('%m/%d/%Y'))

class QueuedResource(TelescopeResource):
	### Properites ###
	# Optional
	awarded_hours = models.FloatField(null=True, blank=True)
	used_hours = models.FloatField(null=True, blank=True)

	@property
	def remaining_hours(self):
		return None if self.awarded_hours is None else round(self.awarded_hours - (self.used_hours or 0.0), 3)

	def __str__(self):
		return "Queued Resource: %s; Valid: %s to %s" % (self.telescope.name, self.begin_date_valid.strftime('%m/%d/%Y'), self.end_date_valid.strftime('%m/%d/%Y'))

class ClassicalResource(TelescopeResource):
	
	def __str__(self):
		if self.principal_investigator:
			return "Classical Resource: %s; PI: %s, Valid: %s to %s" % (self.telescope.name, self.principal_investigator.name, self.begin_date_valid.strftime('%m/%d/%Y'), self.end_date_valid.strftime('%m/%d/%Y'))
		else:
			return "Classical Resource: %s; Valid: %s to %s" % (self.telescope.name, self.begin_date_valid.strftime('%m/%d/%Y'), self.end_date_valid.strftime('%m/%d/%Y'))




class ClassicalObservingDate(BaseModel):
	### Entity relationships ###
	# Required
	resource = models.ForeignKey(ClassicalResource, on_delete=models.CASCADE)
	night_type = models.ForeignKey(ClassicalNightType, on_delete=models.CASCADE)

	### Properties ###
	# Required
	obs_date = models.DateTimeField()

	def happening_soon(self):
		now = timezone.now()
		return (now + datetime.timedelta(days=4) >= self.obs_date >= now-datetime.timedelta(days=2))

	def __str__(self):
		return "%s - %s" % (self.resource.telescope.name, self.obs_date.strftime('%m/%d/%Y'))
