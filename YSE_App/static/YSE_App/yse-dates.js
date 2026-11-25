/**
 * The one date format for people (#807), for rows built in the browser.
 * Same output as the Django `utcdate` filter (YSE_App/common/utc_dates.py):
 *   yseUtcDateHtml("2026-10-24T08:41:17Z")
 *     -> '<span class="yse-utcdate" title="MJD 61337.362">20261024.362</span>'
 * Values without a time zone are taken as UTC. A date-only "YYYY-MM-DD" value
 * has no time of day, so it prints as plain "YYYYMMDD" (no fraction, no MJD
 * tooltip). Unparseable values come back escaped and unchanged;
 * null/undefined/"" give "".
 */
(function () {
  var MJD_UNIX_EPOCH = 40587; // MJD of 1970-01-01
  var UNIT_MS = 86400; // 1/1000 of a day in milliseconds
  var DATE_ONLY_RE = /^\s*(\d{4})-(\d{2})-(\d{2})\s*$/;
  var RE = /^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$/i;

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function pad(n, width) {
    var s = String(n);
    while (s.length < width) s = "0" + s;
    return s;
  }

  function parseUtcMs(value) {
    var m = RE.exec(String(value));
    if (!m) return null;
    var ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0),
                      m[7] ? Math.round(+("0." + m[7]) * 1000) : 0);
    if (m[8] && m[8].toUpperCase() !== "Z") {
      var sign = m[8][0] === "-" ? -1 : 1;
      var digits = m[8].replace(/[^0-9]/g, "");
      ms -= sign * (+digits.slice(0, 2) * 60 + +digits.slice(2, 4)) * 60000;
    }
    return isNaN(ms) ? null : ms;
  }

  function parts(value) {
    if (value === null || value === undefined || value === "") return null;
    var dateOnly = DATE_ONLY_RE.exec(String(value));
    if (dateOnly) {
      var check = new Date(Date.UTC(+dateOnly[1], +dateOnly[2] - 1, +dateOnly[3]));
      if (isNaN(check.getTime()) || check.getUTCDate() !== +dateOnly[3]) return null;
      return {utc: dateOnly[1] + dateOnly[2] + dateOnly[3], mjd: null};
    }
    var ms = parseUtcMs(value);
    if (ms === null) return null;
    var units = Math.floor(ms / UNIT_MS + 0.5) + MJD_UNIX_EPOCH * 1000; // thousandths of a day, half up
    var day = Math.floor(units / 1000);
    var frac = units - day * 1000;
    var d = new Date((day - MJD_UNIX_EPOCH) * 86400000);
    return {
      utc: pad(d.getUTCFullYear(), 4) + pad(d.getUTCMonth() + 1, 2) + pad(d.getUTCDate(), 2) + "." + pad(frac, 3),
      mjd: day + "." + pad(frac, 3)
    };
  }

  window.yseUtcDateText = function (value) {
    var p = parts(value);
    if (p) return p.utc;
    return value === null || value === undefined ? "" : String(value);
  };

  window.yseUtcDateHtml = function (value) {
    var p = parts(value);
    if (!p) return value === null || value === undefined ? "" : escapeHtml(value);
    if (p.mjd === null) return p.utc;
    return '<span class="yse-utcdate" title="MJD ' + p.mjd + '">' + p.utc + "</span>";
  };
})();
