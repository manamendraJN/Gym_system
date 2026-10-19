(function () {
  "use strict";

  // Responsive sidebar
  var sidebar = document.getElementById("sidebar");
  var backdrop = document.getElementById("sidebarBackdrop");
  var toggle = document.getElementById("sidebarToggle");
  function setSidebar(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    backdrop.classList.toggle("show", open);
  }
  if (toggle) toggle.addEventListener("click", function () { setSidebar(!sidebar.classList.contains("open")); });
  if (backdrop) backdrop.addEventListener("click", function () { setSidebar(false); });

  // Loading state: disable submit buttons (prevents double submits) and show a spinner.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.dataset.noLoading !== undefined) return;
    var btn = form.querySelector('button[type="submit"]:not([data-no-loading])');
    if (btn) {
      setTimeout(function () {
        btn.disabled = true;
        btn.dataset.originalHtml = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Please wait…';
      }, 0);
    }
  });
  // Restore buttons when the page is shown from the back/forward cache.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("button[data-original-html]").forEach(function (b) {
      b.disabled = false; b.innerHTML = b.dataset.originalHtml;
    });
  });

  // Auto-dismiss success messages
  document.querySelectorAll(".alert-success").forEach(function (el) {
    setTimeout(function () {
      if (window.bootstrap) bootstrap.Alert.getOrCreateInstance(el).close();
    }, 6000);
  });

  // Confirm dialogs: <form data-confirm="Are you sure?">
  document.addEventListener("submit", function (event) {
    var msg = event.target.dataset && event.target.dataset.confirm;
    if (msg && !window.confirm(msg)) event.preventDefault();
  }, true);
})();
