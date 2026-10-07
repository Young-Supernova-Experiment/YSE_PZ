/* Weather + SkyCam widget (#311): reloads the widget body from its fragment URL and
 * re-reads the SkyCam image on a timer. Works for widgets present at page load and
 * for those inserted later (detail-page fragments): call window.yseInitWeatherWidgets(). */
(function () {
  "use strict";
  var TIMERS = {};

  function bust(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + "t=" + Date.now();
  }

  function reloadSkycam(widget) {
    var imgs = widget.querySelectorAll("img.yse-skycam-img");
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      var src = img.getAttribute("data-src") || img.getAttribute("src");
      if (!src) { continue; }
      img.setAttribute("src", bust(src));
      var stamp = widget.querySelector(".yse-skycam-stamp");
      if (stamp) { stamp.textContent = "reloaded " + new Date().toISOString().slice(11, 19) + " UT"; }
    }
  }

  function reloadBody(widget, force) {
    var url = widget.getAttribute("data-fragment-url");
    var body = widget.querySelector(".yse-weather-body");
    if (!url || !body || !window.fetch) { return; }
    var full = url + (url.indexOf("?") === -1 ? "?" : "&") + (force ? "refresh=1&" : "") + "_=" + Date.now();
    widget.classList.add("yse-weather-loading");
    fetch(full, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html) {
          body.innerHTML = html;
          var readings = body.querySelector("[data-status]");
          widget.setAttribute("data-status", readings ? readings.getAttribute("data-status") : widget.getAttribute("data-status"));
          reloadSkycam(widget);
        }
      })
      .catch(function () {})
      .then(function () { widget.classList.remove("yse-weather-loading"); });
  }

  function init(widget) {
    var id = widget.getAttribute("data-telescope-id");
    if (!id || widget.getAttribute("data-yse-weather-init") === "1") { return; }
    widget.setAttribute("data-yse-weather-init", "1");
    var btn = widget.querySelector(".yse-weather-refresh");
    if (btn) {
      btn.addEventListener("click", function () { reloadBody(widget, true); });
    }
    var status = widget.getAttribute("data-status");
    if (status === "pending" || status === "stale") {
      reloadBody(widget, false);
    } else {
      reloadSkycam(widget);
    }
    var every = parseInt(widget.getAttribute("data-weather-refresh") || "0", 10);
    var skycamEvery = parseInt(widget.getAttribute("data-skycam-refresh") || "0", 10);
    if (TIMERS[id]) { TIMERS[id].forEach(clearInterval); }
    TIMERS[id] = [];
    if (every > 0) {
      TIMERS[id].push(setInterval(function () {
        if (document.body.contains(widget)) { reloadBody(widget, false); }
      }, every * 1000));
    }
    if (skycamEvery > 0) {
      TIMERS[id].push(setInterval(function () {
        if (document.body.contains(widget)) { reloadSkycam(widget); }
      }, skycamEvery * 1000));
    }
  }

  function initAll(root) {
    var nodes = (root || document).querySelectorAll(".yse-weather-widget");
    for (var i = 0; i < nodes.length; i++) { init(nodes[i]); }
  }

  window.yseInitWeatherWidgets = initAll;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  } else {
    initAll(document);
  }
  // Fragments inserted by jQuery.html() on the detail page: re-scan after ajax completes.
  if (window.jQuery) {
    window.jQuery(document).on("ajaxComplete", function () { initAll(document); });
  }
})();
