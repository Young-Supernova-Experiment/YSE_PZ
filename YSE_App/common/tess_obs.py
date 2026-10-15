import hashlib
import logging
import re

import requests
from django.core.cache import cache

_TESS_CACHE_TTL = 60 * 60 * 24 * 7  # 7 days
logger = logging.getLogger(__name__)


def _tess_cache_key(ra, dec, discovery_jd):
	raw = f"{round(float(ra), 5)}:{round(float(dec), 5)}:{round(float(discovery_jd), 2)}"
	return "tess_obs:" + hashlib.sha256(raw.encode()).hexdigest()


def tess_obs(ra, dec, discovery_jd):
	"""Return True if TESS likely observed this sky position near discovery_jd.

	Network / HEASARC failures must not raise: transient post_save (and CI)
	call this on every create. Soft-fail to False without caching so a later
	save can retry when the service recovers.
	"""
	key = _tess_cache_key(ra, dec, discovery_jd)
	cached = cache.get(key)
	if cached is not None:
		return cached

	before_leeway = 10	 # Days of leeway before date
	after_leeway = 10	 # Days of leeway after date
	tess_date = [
		2458324.5,2458352.5,2458381.5,2458409.5,2458437.5,2458463.5,
		2458490.5,2458516.5,2458542.5,2458568.5,2458595.5,2458624.5,
		2458653.5,2458682.5,2458710.5,2458737.5,2458763.5,2458789.5,
		2458814.5,2458841.5,2458869.5,2458897.5,2458926.5,2458955.5,
		2458982.5,2459008.5,2459034.5,2459060.5,2459087.5,2459114.5,
		2459143.5,2459172.5,2459200.5,2459227.5,2459254.5,2459280.5,
		2459306.5,2459332.5,2459360.5,2459389.5,2459418.5,2459446.5,
		2459473.5,2459499.5,2459524.5,2459550.5,2459578.5,2459607.5,
		2459636.5,2459664.5,2459691.5,2459717.5,
		2459743.5,2459769.5,2459796.5,2459823.5,2459852.5,2459881.5,
		2459909.5,2459936.5,2459962.5,2459987.5,2460013.5,2460040.5,
		2460068.5,2460097.5,2460126.5,2460154.5,2460181.5,2460207.5]
	url = 'https://heasarc.gsfc.nasa.gov/cgi-bin/tess/webtess/'
	url += 'wtv.py?Entry={ra}%2C{dec}'
	try:
		r = requests.get(url.format(ra=str(ra), dec=str(dec)), timeout=15)
	except requests.RequestException as exc:
		logger.warning("TESS footprint query failed (%s); skipping tag", exc)
		return False
	if r.status_code != 200:
		logger.warning(
			"TESS footprint query returned HTTP %s; skipping tag",
			r.status_code,
		)
		return False

	reg = r"observed in camera \w+.\nSector \w+"
	info = re.findall(reg, r.content.decode())
	sectors=[]
	for k in info:
	    sectors.append(int(re.split(r'\s', k)[5])-1)

	if len(sectors)>0:
		for sector in sectors:
			if int(sector)<len(tess_date):
				if (discovery_jd > tess_date[int(sector)-1]-before_leeway and
					discovery_jd < tess_date[int(sector)]+after_leeway):
					cache.set(key, True, _TESS_CACHE_TTL)
					return True
	cache.set(key, False, _TESS_CACHE_TTL)
	return False
