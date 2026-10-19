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

  // Backdrop parallax: nudge each equipment layer a few pixels with scroll (app) or pointer (login). Skipped for reduced motion.
  var bg = document.querySelector(".gym-bg");
  if (bg && !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) {
    var layers = bg.querySelectorAll(".layer"), ticking = false, px = 0, py = 0;
    var apply = function () {
      ticking = false;
      var y = window.scrollY;
      layers.forEach(function (l) {
        var d = parseFloat(l.dataset.depth) || 0.04;
        l.style.transform = "translate3d(" + (px * d * 40).toFixed(1) + "px," + (-(y * d) + py * d * 40).toFixed(1) + "px,0)";
      });
    };
    var request = function () { if (!ticking) { ticking = true; window.requestAnimationFrame(apply); } };
    // Only the login page uses pointer parallax; the app skips scroll-linked work so scrolling stays smooth.
    if (document.body.classList.contains("auth-body")) {
      window.addEventListener("pointermove", function (e) { px = e.clientX / window.innerWidth - 0.5; py = e.clientY / window.innerHeight - 0.5; request(); }, { passive: true });
    }
  }
})();
