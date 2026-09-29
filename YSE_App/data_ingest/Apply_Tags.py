import datetime
import logging

from django_cron import CronJobBase, Schedule

from YSE_App.common.alert import IsK2Pixel
from YSE_App.common.tess_obs import tess_obs
from YSE_App.common.thacher_transient_search import thacher_transient_search
from YSE_App.common.utilities import date_to_mjd
from YSE_App.models.tag_models import TransientTag
from YSE_App.models.transient_models import Transient

logger = logging.getLogger(__name__)


def _add_tag(transient, tag_name):
	"""Attach the named TransientTag; a missing tag row is logged, not fatal."""
	try:
		tag = TransientTag.objects.get(name=tag_name)
	except TransientTag.DoesNotExist:
		logger.warning("TransientTag %r does not exist; cannot tag %s", tag_name, transient)
		return False
	transient.tags.add(tag)
	return True


def apply_footprint_tags(transient, tag_K2=False, tag_TESS=True, tag_Thacher=True):
	"""
	Tag one transient by survey footprint: TESS (HEASARC lookup, cached 7 days
	in tess_obs), Thacher (local catalogue) and optionally K2.

	This is the only place these tags are applied. The Transient post_save
	handler used to do the same work synchronously in the request path (#239).
	"""
	logger.info("checking transient %s", transient)
	if tag_K2:
		is_k2_C16_validated, C16_msg = IsK2Pixel(transient.ra, transient.dec, "16")
		is_k2_C17_validated, C17_msg = IsK2Pixel(transient.ra, transient.dec, "17")
		is_k2_C19_validated, C19_msg = IsK2Pixel(transient.ra, transient.dec, "19")
		logger.debug("K2 C16 %s (%s); C17 %s (%s); C19 %s (%s)",
					 is_k2_C16_validated, C16_msg, is_k2_C17_validated, C17_msg,
					 is_k2_C19_validated, C19_msg)
		for validated, msg, tag_name in (
			(is_k2_C16_validated, C16_msg, 'K2 C16'),
			(is_k2_C17_validated, C17_msg, 'K2 C17'),
			(is_k2_C19_validated, C19_msg, 'K2 C19'),
		):
			if validated:
				transient.k2_validated = True
				transient.k2_msg = msg
				transient.tags.add(TransientTag.objects.get(name=tag_name))
				break

	if tag_TESS:
		ref_date = transient.disc_date or transient.modified_date
		if ref_date and tess_obs(transient.ra, transient.dec, date_to_mjd(ref_date) + 2400000.5):
			logger.info("tagging %s as TESS", transient)
			_add_tag(transient, 'TESS')

	if tag_Thacher and thacher_transient_search(transient.ra, transient.dec):
		logger.info("tagging %s as Thacher", transient)
		_add_tag(transient, 'Thacher')


class Tags(CronJobBase):

	RUN_EVERY_MINS = 480

	schedule = Schedule(run_every_mins=RUN_EVERY_MINS)
	code = 'YSE_App.data_ingest.Apply_Tags.Tags'

	def do(self, tag_K2=False):

		try:
			nowdate = datetime.datetime.utcnow() - datetime.timedelta(1)
			transients = Transient.objects.filter(created_date__gt=nowdate)
			for t in transients:
				apply_footprint_tags(t, tag_K2=tag_K2)
				t.save()

		except Exception:
			logger.exception("Apply_Tags cron failed")
