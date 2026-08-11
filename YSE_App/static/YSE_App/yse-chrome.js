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

  document.addEventListener("DOMContentLoaded", function () {
    apply(currentTheme());
    shimBs3Widgets();
    var btn = document.getElementById("yse-theme-toggle");
    if (!btn) {
      return;
    }
    btn.addEventListener("click", function () {
      apply(currentTheme() === "dark" ? "light" : "dark");
    });
  });
})();
