"""K2 pixel check and the legacy alert senders.

Every email path here goes through the notification queue
(``YSE_App.services.notify``): ``SendTransientAlert`` and
``SendFollowingNotice`` create ``alert`` / ``followup_request`` notifications
(in-app row, email per user preference, same HTML bodies as before) and
``sendemail`` / ``sendsms`` use Django's mail backend, which settings.py fills
from the ``[SMTP_provider]`` block. No ``smtplib`` here any more (#320/#69).
"""

import logging
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives, send_mail

from YSE_App.models.on_call_date_models import OnCallDate
from YSE_App.models.profile_models import Profile

logger = logging.getLogger(__name__)


def _notify_service():
	from YSE_App.services import notify as notify_service

	return notify_service


def _base_url():
	return _notify_service().base_url()


def IsK2Pixel(ra, dec, campaign_num):

	ra = float(ra); dec = float(dec)
	
	print("Checking K2 Campaign %s API" % campaign_num)
	print("Input: (%0.5f, %0.5f)" % (ra, dec))

	YES = 'yes'
	HTTP_SUCCESS = 200

	url_formatter = "%s?ra=%0.5f&dec=%0.5f&campaign=%s"
	formatted_url = url_formatter % (settings.KEPLER_API_ENDPOINT, ra, dec, campaign_num)

	try:
		r = requests.get(formatted_url)

		if r.status_code == HTTP_SUCCESS:
			print("K2 API call success")

		is_K2 = (r.text == YES)
		if is_K2:
			print("Is K2")
		else:
			print("Not K2")

		return (is_K2, "200 Success")

	except requests.exceptions.RequestException as e:  # This is the correct syntax
		# By default, return True... so we don't miss any
		
		print("K2 API call failed")
		return (True, ("K2 API error: %s" % e))

def send_email_simple(to_addr, subject, message):
	"""Legacy helper: one HTML email to ``to_addr`` through Django's mail backend."""
	return sendemail(None, to_addr, subject, message)


def SendTransientAlert(transient_id, transient_name, ra, dec):
	"""K2 transient alert: ``alert`` notification (email per preference) to every user, SMS to on-call."""

	print("Sending Alert")

	subject = "TNS K2 Transient - Action Required"
	if settings.DEBUG:
		subject = "[TESTING] TNS K2 Transient - Action Required"

	base_url = _base_url()

	html_msg = """\
		<html>
			<head></head>
			<body>
				<h1>New K2 Transient!</h1>
				<p>
					<a href='%stransient_detail/%s/'>%s</a> (%s, %s)
				</p>
				<br />
				<p>Go to <a href='%sdashboard/'>YSE Dashboard</a></p>
			</body>
		</html>
	""" % (base_url, transient_name, transient_name, ra, dec, base_url)

	txt_msg = "New K2 Transient: %s (%s, %s)\n\nDetail: %stransient_detail/%s/\n\nDashboard: %sdashboard/" % \
			(transient_name, ra, dec, base_url, transient_name, base_url)

	# today() is UTC, so convert it to Pacific Standard Time
	print("UTC Now: %s" % datetime.today())

	PST_date = datetime.today() + timedelta(hours=settings.LOCAL_UTC_OFFSET)
	TargetOnCallDate = PST_date

	# Once we've correct for local time, we need to know which OnCall date to
	# actually access. If it is before 9:00 AM, it's yesterday's OnCallDate. If
	# it is >= 9:00 AM, it is today's OnCallDate
	begin_business_hours = 9 # PST
	end_business_hours = 17 # PST
	current_hour = PST_date.time().hour

	if current_hour < begin_business_hours:
		TargetOnCallDate = TargetOnCallDate - timedelta(days=1)

	print("PST Now: %s" % PST_date)
	print("Target On Call Date: %s" % TargetOnCallDate)

	business_hours = True
	if (current_hour < begin_business_hours) or (current_hour >= end_business_hours):
		business_hours = False

	print("Business Hours? %s" % business_hours)

	# Notify everyone regardless of business hours (email follows each user's preference)
	all_users = User.objects.filter(is_active=True).exclude(username='admin')
	notifications = _notify_service().notify(
		all_users, txt_msg, "/transient_detail/%s/" % transient_name, "alert",
		subject=subject, html=html_msg, payload={"transient_id": transient_id, "ra": ra, "dec": dec},
	)

	if business_hours:
		print("Sending SMS to everyone")
		sms_users = all_users
	else:
		print("Non-business hours... only send SMS to On Call list")
		sms_users = User.objects.none()
		ocd = OnCallDate.objects.filter(on_call_date=TargetOnCallDate.date()).first()
		if ocd is not None:
			print("On Call Date: %s" % ocd.on_call_date.strftime('%m/%d/%Y'))
			sms_users = ocd.user.all()

	for user in sms_users:
		print("Sending text to: %s" % user.username)
		for p in Profile.objects.filter(user__id=user.id):
			phone_email = "%s%s%s@%s" % (p.phone_area, p.phone_first_three, p.phone_last_four, p.phone_provider_str)
			sendsms(None, phone_email, subject, txt_msg)

	return notifications


def SendFollowingNotice(transient_id, transient_name, telescope, profile):
	"""Legacy entry point: ``followup_request`` notification to one telescope follower."""
	from YSE_App.services.followup_notices import followup_notice_html

	print("Sending Following Notice to %s" % profile.user.first_name)
	if not profile.user.email:
		raise RuntimeError('email doesn\'t exist')
	telescope_name = getattr(telescope, "name", telescope)
	subject = "New %s Request in YSE_PZ for %s" % (telescope_name, transient_name)
	text = "%s follow-up requested for %s." % (telescope_name, transient_name)
	return _notify_service().notify(
		[profile.user], text, "/transient_detail/%s/" % transient_name, "followup_request",
		subject=subject, html=followup_notice_html(transient_name, transient_name, telescope_name, _base_url()),
		payload={"followup_id": transient_id, "telescope": telescope_name},
	)


def sendemail(from_addr, to_addr,
			subject, message,
			login=None, password=None, smtpserver=None, cc_addr=None):
	"""Canonical HTML email helper for YSE_PZ (ingest, cron, and web).

	``message`` is HTML; a plain-text part is derived from it. ``login``,
	``password`` and ``smtpserver`` are accepted for the old call sites but the
	connection comes from Django's ``EMAIL_*`` settings (the same
	``[SMTP_provider]`` block). Returns True when the backend accepted the
	message; failures are logged, not raised, as before.
	"""

	print("Preparing email")
	text = _notify_service().html_to_text(message) or message
	msg = EmailMultiAlternatives(subject, text, from_addr or None, [to_addr], cc=[cc_addr] if cc_addr else None)
	msg.attach_alternative(message, 'text/html')
	try:
		sent = msg.send(fail_silently=False)
	except Exception as exc:  # noqa: BLE001 - legacy callers expect no exception
		logger.warning("email to %s failed: %s", to_addr, exc)
		print("Send fail")
		return False
	print("Send success")
	return bool(sent)


def sendsms(from_addr, to_addr,
			subject, message,
			login=None, password=None, smtpserver=None, cc_addr=None):
	"""Plain-text email to a carrier SMS gateway address through Django's mail backend."""

	print("Preparing SMS")
	try:
		sent = send_mail(subject, message, from_addr or None, [to_addr], fail_silently=False)
	except Exception as exc:  # noqa: BLE001 - legacy callers expect no exception
		logger.warning("sms to %s failed: %s", to_addr, exc)
		print("Send fail")
		return False
	print("Send success")
	return bool(sent)
