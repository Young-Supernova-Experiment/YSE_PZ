/**
 * YSE-PZ chrome: night/light theme toggle, sidebar state + BS3 widget shims.
 * With no stored choice the theme follows prefers-color-scheme (#821); a click on
 * the toggle stores the choice in localStorage key "yse-theme-choice", which then
 * wins. The old key "yse-theme" was written on every page load, so it is only read
 * once for migration: a legacy "light" counts as a choice, a legacy "dark" does not.
 * The sidebar's collapse choice on wide screens is stored in "yse-sidebar" (#839);
 * only a click on the toggle is stored, not AdminLTE's own load/resize auto-collapse.
 * Head FOUC script in base.html must run first.
 */
(function () {
  var KEY = "yse-theme-choice";
  var LEGACY_KEY = "yse-theme";
  var SIDEBAR_KEY = "yse-sidebar";
  var WIDE = 992;

  // Same test as AdminLTE's autoCollapse ($(window).width() <= 992, scrollbar excluded).
  function isWide() {
    return document.documentElement.clientWidth > WIDE;
  }

  function storedTheme() {
    var t = null;
    try {
      t = localStorage.getItem(KEY);
      if (t !== "light" && t !== "dark") {
        t = null;
        if (localStorage.getItem(LEGACY_KEY) === "light") {
          t = "light";
          localStorage.setItem(KEY, "light");
        }
      }
    } catch (err) {
      /* storage blocked: t is whatever was read */
    }
    return t;
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-yse-theme") || "dark";
  }

  function apply(theme, remember) {
    var next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-yse-theme", next);
    document.documentElement.classList.toggle("dark-mode", next === "dark");
    if (document.body) {
      document.body.classList.toggle("dark-mode", next === "dark");
    }
    // AdminLTE colours the sidebar from sidebar-dark-* / sidebar-light-*, not from dark-mode.
    var sidebars = document.querySelectorAll(".main-sidebar");
    for (var i = 0; i < sidebars.length; i++) {
      sidebars[i].classList.toggle("sidebar-dark-primary", next === "dark");
      sidebars[i].classList.toggle("sidebar-light-primary", next !== "dark");
    }
    // navbar-dark is white text: swap it, and the outline buttons, with the theme (#823).
    var headers = document.querySelectorAll(".main-header");
    for (var h = 0; h < headers.length; h++) {
      headers[h].classList.toggle("navbar-dark", next === "dark");
      headers[h].classList.toggle("navbar-light", next !== "dark");
      var btns = headers[h].querySelectorAll(".btn-outline-light, .btn-outline-secondary");
      for (var b = 0; b < btns.length; b++) {
        btns[b].classList.toggle("btn-outline-light", next === "dark");
        btns[b].classList.toggle("btn-outline-secondary", next !== "dark");
      }
    }
    if (remember) {
      try {
        localStorage.setItem(KEY, next);
      } catch (err) {
        /* ignore quota / private mode */
      }
    }
    var btn = document.getElementById("yse-theme-toggle");
    if (btn) {
      var lightOn = next === "light";
      btn.setAttribute("aria-pressed", lightOn ? "true" : "false");
      btn.textContent = lightOn ? "Dark mode" : "Light mode";
    }
  }

  function syncSidebarToggle() {
    var body = document.body;
    if (!body) {
      return;
    }
    var expanded = isWide()
      ? !body.classList.contains("sidebar-collapse")
      : body.classList.contains("sidebar-open");
    var toggles = document.querySelectorAll("[data-widget='pushmenu']");
    for (var i = 0; i < toggles.length; i++) {
      toggles[i].setAttribute("aria-expanded", expanded ? "true" : "false");
    }
  }

  function rememberSidebar() {
    // Only wide-screen choices are remembered; on phones the sidebar is an overlay.
    if (typeof window.jQuery === "undefined") {
      return;
    }
    // AdminLTE fires the same events from autoCollapse() on load and resize, so only
    // store a state that follows a click on the toggle. Capture phase runs before
    // AdminLTE's own click handler, which triggers the events synchronously.
    var userToggle = false;
    document.addEventListener("click", function (event) {
      var t = event.target;
      if (t && t.closest && t.closest("[data-widget='pushmenu']")) {
        userToggle = true;
        window.setTimeout(function () { userToggle = false; }, 0);
      }
    }, true);
    window.jQuery(document).on("collapsed.lte.pushmenu shown.lte.pushmenu", function (event) {
      if (userToggle && isWide()) {
        try {
          localStorage.setItem(SIDEBAR_KEY, event.type === "collapsed" ? "collapsed" : "expanded");
        } catch (err) {
          /* ignore quota / private mode */
        }
      }
      userToggle = false;
      syncSidebarToggle();
    });
    window.addEventListener("resize", syncSidebarToggle);
    syncSidebarToggle();
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
    apply(currentTheme(), false);
    rememberSidebar();
    shimBs3Widgets();
    window.setTimeout(fitYseCalendar, 0);
    window.addEventListener("resize", fitYseCalendar);
    window.addEventListener("load", fitYseCalendar);
    // Until the user picks a theme, follow OS changes too.
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: light)");
      var onOsChange = function (e) {
        if (storedTheme() === null) {
          apply(e.matches ? "light" : "dark", false);
        }
      };
      if (mq.addEventListener) {
        mq.addEventListener("change", onOsChange);
      } else if (mq.addListener) {
        mq.addListener(onOsChange);
      }
    }
    var btn = document.getElementById("yse-theme-toggle");
    if (!btn) {
      return;
    }
    btn.addEventListener("click", function () {
      apply(currentTheme() === "dark" ? "light" : "dark", true);
    });
  });
})();
