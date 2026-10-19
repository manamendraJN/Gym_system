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

  // Loading state: disable submit buttons (prevents double submits) and show "Please wait…".
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.dataset.noLoading !== undefined) return;
    var btn = form.querySelector('button[type="submit"]:not([data-no-loading])');
    if (btn) {
      setTimeout(function () {
        btn.disabled = true;
        btn.dataset.originalHtml = btn.innerHTML;
        btn.textContent = "Please wait…";
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

  // Confirmation dialogs: <form data-confirm="Are you sure?">. Uses a Bootstrap modal, or window.confirm if Bootstrap is unavailable.
  var modalEl = document.getElementById("confirmModal");
  var pending = null;
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var msg = form.dataset && form.dataset.confirm;
    if (!msg || form._confirmed) return;
    event.preventDefault();
    if (!(modalEl && window.bootstrap)) {
      if (window.confirm(msg)) { form._confirmed = true; form.requestSubmit(); }
      return;
    }
    pending = form;
    document.getElementById("confirmBody").textContent = msg;
    var ok = document.getElementById("confirmOk");
    var danger = !!form.querySelector(".btn-danger, .btn-outline-danger");
    ok.className = "btn " + (danger ? "btn-danger" : "btn-primary");
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }, true);
  if (modalEl) {
    document.getElementById("confirmOk").addEventListener("click", function () {
      var form = pending; pending = null;
      bootstrap.Modal.getInstance(modalEl).hide();
      if (form) { form._confirmed = true; form.requestSubmit(); }
    });
    modalEl.addEventListener("hidden.bs.modal", function () { pending = null; });
  }
})();
