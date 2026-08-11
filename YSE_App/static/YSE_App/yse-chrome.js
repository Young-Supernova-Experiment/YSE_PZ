/**
 * YSE-PZ chrome: night/light theme toggle.
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

  document.addEventListener("DOMContentLoaded", function () {
    apply(currentTheme());
    var btn = document.getElementById("yse-theme-toggle");
    if (!btn) {
      return;
    }
    btn.addEventListener("click", function () {
      apply(currentTheme() === "dark" ? "light" : "dark");
    });
  });
})();
