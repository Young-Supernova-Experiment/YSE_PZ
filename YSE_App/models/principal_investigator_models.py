from django.db import models
from YSE_App.models.base import *
from YSE_App.models.enum_models import *

class PrincipalInvestigator(BaseModel):
	### Entity relationships ###
	# Optional
	obs_group = models.ManyToManyField(ObservationGroup, blank=True)
	# The YSE-PZ account of this PI (#446), when they have one: PIs of private
	# photometry and spectra are notified about interests and data-access
	# requests through it, and may decide those requests. Set by staff (admin).
	user = models.ForeignKey(
		User, null=True, blank=True, on_delete=models.SET_NULL, related_name='pi_records',
		help_text='YSE-PZ account of this PI (optional). Linked PIs are notified about interest in, and requests for, '
		          'the data they lead, and may share it.')

	### Properties ###
	# Required
	name = models.CharField(max_length=64)

	# Optional
	phone = models.CharField(max_length=64)
	email = models.EmailField()
	institution = models.CharField(max_length=64)
	description = models.TextField(null=True, blank=True)

	def __str__(self):
		return self.name