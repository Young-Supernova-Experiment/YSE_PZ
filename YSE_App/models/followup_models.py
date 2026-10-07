from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.dispatch import receiver
from django.contrib.auth.models import Group
from django.contrib.auth.models import User
from YSE_App.models.base import *
from YSE_App.models.enum_models import *
from YSE_App.models.telescope_resource_models import *
from YSE_App.models.transient_models import *
from YSE_App.models.host_models import *
from YSE_App.models.profile_models import *

#class SimpleTransientSpecRequest(BaseModel):
#	status = models.ForeignKey(FollowupStatus, on_delete=models.SET(get_sentinel_followupstatus))
#	transient = models.ForeignKey(Transient, on_delete=models.CASCADE)

class Followup(BaseModel):

	class Meta:
		abstract = True
		ordering = ['-id']

	### Entity relationships ###
	# Required
	status = models.ForeignKey(FollowupStatus, on_delete=models.SET(get_sentinel_followupstatus))

	# Optional
	too_resource = models.ForeignKey(ToOResource, null=True, blank=True, on_delete=models.SET_NULL)
	classical_resource = models.ForeignKey(ClassicalResource, null=True, blank=True, on_delete=models.SET_NULL)
	queued_resource = models.ForeignKey(QueuedResource, null=True, blank=True, on_delete=models.SET_NULL)

	### Properties ###
	# Required
	valid_start = models.DateTimeField()
	valid_stop = models.DateTimeField()

	# Optional
	priority = models.FloatField(null=True, blank=True)
	offset_star_ra = models.FloatField(null=True, blank=True)
	offset_star_dec = models.FloatField(null=True, blank=True)
	offset_north = models.FloatField(null=True, blank=True)
	offset_east = models.FloatField(null=True, blank=True)

	# Collaboration visibility (default public; see YSE_App.services.visibility)
	is_public = models.BooleanField(default=True)
	requested_by = models.ForeignKey(
		User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
	)
	groups = models.ManyToManyField(Group, blank=True)

class TransientFollowup(Followup):
	### Entity relationships ###
	# Required
	transient = models.ForeignKey(Transient, on_delete=models.CASCADE)

	# Usage accounting on the attached ToO / queued resource (#304): stamped once
	# when the status reaches Successful, cleared (and refunded) when it leaves it.
	usage_hours = models.FloatField(default=0.0, help_text="Hours charged to the resource when this follow-up succeeds.")
	usage_charged_at = models.DateTimeField(null=True, blank=True, editable=False)

	def __str__(self):
		return "Transient Followup: [%s]; Valid: %s to %s" % (self.transient.name, self.valid_start.strftime('%m/%d/%Y'), self.valid_stop.strftime('%m/%d/%Y'))

	def observation_window(self):
		return "%s - %s" % (self.valid_start.strftime('%m/%d/%Y'), self.valid_stop.strftime('%m/%d/%Y'))		

class TransientFollowupRequest(BaseModel):
	"""One observing-request submit attached to a parent TransientFollowup.

	Same user may have many rows. Display priority is min of each
	requestor's most recent ``priority`` (1.0 highest, 5.0 lowest).
	"""

	class Meta:
		ordering = ['requested_at', 'id']
		indexes = [
			# Name pinned to the index created by migration 0008 so the
			# autodetector does not try to rename it.
			models.Index(
				fields=['followup', 'requestor', '-requested_at'],
				name='YSE_App_tra_followu_7c9a1e_idx',
			),
		]

	followup = models.ForeignKey(
		TransientFollowup,
		on_delete=models.CASCADE,
		related_name='requests',
	)
	requestor = models.ForeignKey(
		User,
		on_delete=models.PROTECT,
		related_name='transient_followup_requests',
	)
	requested_at = models.DateTimeField(auto_now_add=True)
	priority = models.FloatField(
		default=4.0,
		validators=[MinValueValidator(1.0), MaxValueValidator(5.0)],
	)
	comment = models.TextField(blank=True)

	def __str__(self):
		return "Followup request: [%s] by %s at %s" % (
			self.followup_id,
			self.requestor,
			self.requested_at,
		)

@receiver(models.signals.post_save, sender=TransientFollowup)
def execute_after_save(sender, instance, created, *args, **kwargs):
	"""New follow-up: notify the users following its telescope (issue #69, via notify())."""

	if created:
		from YSE_App.services.followup_notices import notify_followup_created_safely

		notify_followup_created_safely(instance)
	
class HostFollowup(Followup):
	### Entity relationships ###
	host = models.ForeignKey(Host, on_delete=models.CASCADE)

	def __str__(self):
		return "Host Followup: [%s]; Valid: %s to %s" % (self.host.HostString(), self.valid_start.strftime('%m/%d/%Y'), self.valid_stop.strftime('%m/%d/%Y'))
