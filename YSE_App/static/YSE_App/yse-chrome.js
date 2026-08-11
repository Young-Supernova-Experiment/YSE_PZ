/**
 * YSE-PZ chrome: night/light theme toggle + BS3 widget shims.
 * Default is dark. Persists in localStorage key "yse-theme".
 * Head FOUC script in base.html must run first.
 */
(function () {
  var KEY = "yse-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-yse-theme") || "dark";
  }

  function apply(theme) {
    var next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-yse-theme", next);
    document.documentElement.classList.toggle("dark-mode", next === "dark");
    if (document.body) {
      document.body.classList.toggle("dark-mode", next === "dark");
    }
    try {
      localStorage.setItem(KEY, next);
    } catch (err) {
      /* ignore quota / private mode */
    }
    var btn = document.getElementById("yse-theme-toggle");
    if (btn) {
      var lightOn = next === "light";
      btn.setAttribute("aria-pressed", lightOn ? "true" : "false");
      btn.textContent = lightOn ? "Dark mode" : "Light mode";
    }
  }

  function shimBs3Widgets() {
    if (typeof window.jQuery === "undefined") {
      return;
    }
    var $ = window.jQuery;
    $(".carousel-inner > .item").addClass("carousel-item");
    $(".carousel-control.left").addClass("carousel-control-prev");
    $(".carousel-control.right").addClass("carousel-control-next");
    $(document).on("click", "[data-widget='collapse']", function (event) {
      event.preventDefault();
      event.stopPropagation();
      var $box = $(this).closest(".box");
      if (!$box.length) {
        return;
      }
      $box.toggleClass("collapsed-box");
      if (!$box.hasClass("collapsed-box")) {
        $box.children(".box-body, .box-footer").removeClass("collapsed-box");
      }
    });
  }

  function yseCalendarHeight() {
    var h = window.innerHeight;
    var nodes = document.querySelectorAll(".main-header, .main-footer, .content-header");
    for (var i = 0; i < nodes.length; i += 1) {
      h -= nodes[i].offsetHeight || 0;
    }
    return Math.max(360, h - 72);
  }

  function fitYseCalendar() {
    if (typeof window.jQuery === "undefined" || typeof window.jQuery.fn.fullCalendar !== "function") {
      return;
    }
    var $cal = window.jQuery("#calendar");
    if (!$cal.length) {
      return;
    }
    $cal.fullCalendar("option", "height", yseCalendarHeight());
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply(currentTheme());
    shimBs3Widgets();
    window.setTimeout(fitYseCalendar, 0);
    window.addEventListener("resize", fitYseCalendar);
    window.addEventListener("load", fitYseCalendar);
    var btn = document.getElementById("yse-theme-toggle");
    if (!btn) {
      return;
    }
    btn.addEventListener("click", function () {
      apply(currentTheme() === "dark" ? "light" : "dark");
    });
  });
})();
