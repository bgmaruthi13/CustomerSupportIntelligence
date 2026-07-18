document.addEventListener('DOMContentLoaded', function () {
  const sidebar = document.getElementById('sidebar');
  const toggle = document.getElementById('sidebarToggle');
  // Below tablet width the sidebar auto-collapses via CSS alone (see app.css);
  // toggling .force-expanded overrides that default instead of .collapsed,
  // which is reserved for the desktop manual-collapse behavior below that
  // width. Checked at click time so resizing the window doesn't require a
  // page reload for the toggle to pick the right behavior.
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 1024px)').matches) {
        sidebar.classList.toggle('force-expanded');
      } else {
        sidebar.classList.toggle('collapsed');
      }
    });
  }

  const pswBtn = document.getElementById('pswBtn');
  const pswDropdown = document.getElementById('pswDropdown');
  if (pswBtn && pswDropdown) {
    pswBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      pswDropdown.classList.toggle('open');
    });
    document.addEventListener('click', () => pswDropdown.classList.remove('open'));
  }

  // Page subtitles are single-line with ellipsis truncation (no wrapping) — if the
  // full text got cut off, expose it via native title so it's still one hover away.
  document.querySelectorAll('.page-subtitle').forEach((el) => {
    if (el.scrollWidth > el.clientWidth) el.title = el.textContent.trim();
  });

  // Every .info-dot already carries its tooltip text in data-tip for the
  // hover/focus tooltip below — without this, a screen-reader user tabbing to
  // one gets an unlabeled, purposeless-sounding stop, since data-tip alone
  // has no accessible-name semantics. Setting aria-label here once covers
  // every current and future .info-dot across the app from one place, rather
  // than hand-adding the attribute to each of the many templates that use it.
  document.querySelectorAll('.info-dot[data-tip]').forEach((el) => {
    if (!el.hasAttribute('aria-label')) el.setAttribute('aria-label', el.getAttribute('data-tip'));
  });
});

// "What this does" tooltips (.info-dot[data-tip]). Rendered as a single element
// appended to <body> with position:fixed and positioned via getBoundingClientRect,
// so it always paints above every container and is never clipped by a scrollable
// card, the kpi cards, or the sidebar's own overflow — unlike a CSS ::after bubble
// nested inside those elements, which those containers cut off / turned into stray
// scrollbars.
(function () {
  let tipEl = null;

  function ensureTip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'global-tooltip';
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function showTip(target) {
    const text = target.getAttribute('data-tip');
    if (!text) return;
    const tip = ensureTip();
    tip.textContent = text;
    tip.classList.add('show');

    const rect = target.getBoundingClientRect();
    const margin = 8;
    const tipRect = tip.getBoundingClientRect();

    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));

    let top = rect.top - tipRect.height - margin;
    if (top < margin) top = rect.bottom + margin;

    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove('show');
  }

  document.addEventListener('mouseover', (e) => {
    const dot = e.target.closest('.info-dot');
    if (dot) showTip(dot);
  });
  document.addEventListener('mouseout', (e) => {
    const dot = e.target.closest('.info-dot');
    if (dot) hideTip();
  });
  document.addEventListener('focusin', (e) => {
    const dot = e.target.closest('.info-dot');
    if (dot) showTip(dot);
  });
  document.addEventListener('focusout', (e) => {
    const dot = e.target.closest('.info-dot');
    if (dot) hideTip();
  });
  window.addEventListener('scroll', hideTip, true);
  window.addEventListener('resize', hideTip);
})();

function showToast(message, type) {
  type = type || 'success';
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.innerHTML = `<span>${message}</span><span class="toast-close" onclick="this.parentElement.remove()">✕</span>`;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 4500);
}

function getCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(';').shift();
  return null;
}
