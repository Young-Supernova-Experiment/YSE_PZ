from django.db import models
from YSE_App.models.base import *
from YSE_App.models.fields import JSONTextField
from YSE_App.models.observatory_models import *

class Telescope(BaseModel):
	### Entity relationships ###
	# Required
	observatory = models.ForeignKey(Observatory, on_delete=models.CASCADE)

	### Properties ###
	# Required
	name = models.CharField(max_length=64)
	latitude = models.FloatField()
	longitude = models.FloatField()
	elevation = models.FloatField()

	# Optional: weather widget and SkyCam (#309: #311). All blank = widget omitted.
	weather_url = models.URLField(
		max_length=500, blank=True, default="",
		help_text="JSON endpoint for current conditions (Open-Meteo or OpenWeatherMap shape); "
		          "{lat}, {lon} and {elevation} placeholders are filled from this telescope.",
	)
	weather_link = models.URLField(
		max_length=500, blank=True, default="",
		help_text="The site's own weather page, linked from the widget.",
	)
	skycam_url = models.URLField(
		max_length=500, blank=True, default="",
		help_text="All-sky camera image URL; the widget reloads it periodically.",
	)
	weather = JSONTextField(default=dict, help_text="Cached last weather snapshot (written by the fetcher).")
	weather_fetched_at = models.DateTimeField(null=True, blank=True, editable=False)

	@property
	def has_weather_widget(self):
		return bool(self.weather_url or self.skycam_url or self.weather_link)

	def __str__(self):
		return self.name

	def __unicode__(self):
		return self.name

	def tostring(self):
		return self.name

	def natural_key(self):
		return self.name
