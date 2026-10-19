/* System customization page: preset picker and live preview.
   The preview CSS comes from the server (the same generator that styles the real interface), so what you see is what
   gets saved. Nothing is stored until the form is submitted. */
(function () {
  "use strict";
  var form = document.getElementById("appearance-form");
  var preview = document.getElementById("theme-preview");
  var config = document.getElementById("preview-config");
  var styleEl = document.getElementById("preview-style");
  if (!form || !preview || !config) return;

  var presets = JSON.parse(document.getElementById("preset-data").textContent);
  var colors = ["primary", "secondary", "accent", "background", "surface", "text", "border"];
  var presetSelect = form.elements["theme_preset"];
  var modeSelect = form.elements["mode"];
  var tracked = ["theme_preset", "mode", "sidebar_style", "sidebar_width", "density", "font_scale", "font_family", "stat_columns"]
    .concat(colors.map(function (c) { return c + "_color"; }));
  var timer = null, request = 0;

  function csrf() { return form.querySelector("[name=csrfmiddlewaretoken]").value; }
  function resolvedMode() {
    var m = modeSelect.value;
    if (m === "system") return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    return m;
  }
  function refresh() {
    var body = new URLSearchParams();
    tracked.forEach(function (n) { if (form.elements[n]) body.append(n, form.elements[n].value); });
    body.append("resolved_mode", resolvedMode());
    var mine = ++request;
    fetch(config.dataset.url, { method: "POST", headers: { "X-CSRFToken": csrf(), "Content-Type": "application/x-www-form-urlencoded" }, body: body, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (data) { if (mine === request) styleEl.textContent = data.css; })
      .catch(function () { /* keep the last preview; saving is unaffected */ });
    preview.setAttribute("data-sidebar", form.elements["sidebar_style"].value);
    preview.setAttribute("data-bs-theme", resolvedMode());
  }
  function schedule() { window.clearTimeout(timer); timer = window.setTimeout(refresh, 120); }

  presetSelect.addEventListener("change", function () {
    var p = presets[presetSelect.value];
    if (p) colors.forEach(function (c) { form.elements[c + "_color"].value = p[c]; });
    schedule();
  });
  colors.forEach(function (c) {          // editing a colour by hand makes the theme "Custom"
    form.elements[c + "_color"].addEventListener("input", function () { presetSelect.value = "custom"; });
  });
  tracked.forEach(function (n) {
    var el = form.elements[n];
    if (el) { el.addEventListener("input", schedule); el.addEventListener("change", schedule); }
  });
  refresh();
})();
