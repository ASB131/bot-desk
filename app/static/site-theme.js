(function () {
  var KEY = "botdesk-theme";

  function systemDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function readStored() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  function resolveTheme(stored) {
    if (stored === "dark" || stored === "light") return stored;
    return systemDark() ? "dark" : "light";
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    var color = theme === "dark" ? "#0f1218" : "#f0f2f5";
    if (meta) meta.setAttribute("content", color);
    var btn = document.getElementById("site-theme-toggle");
    if (btn) {
      var next = theme === "dark" ? "light" : "dark";
      btn.setAttribute("aria-label", "Switch to " + next + " theme");
      btn.setAttribute("title", "Switch to " + next + " theme");
      btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
  }

  apply(resolveTheme(readStored()));

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("site-theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme") || "light";
      var next = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch (e) {}
      apply(next);
    });
  });
})();
