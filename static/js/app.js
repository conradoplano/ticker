// Ticker's small scripts; everything else is server-rendered and HTMX.
(() => {
  // Forms that need a second thought: <form data-confirm="Question?">.
  document.addEventListener('submit', (event) => {
    const question = (event.submitter && event.submitter.dataset.confirm) || event.target.dataset.confirm;
    if (question && !window.confirm(question)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // A short message from the server (HX-Trigger: {"toast": "..."}).
  const toast = document.getElementById('toast');
  let toastTimer;
  document.body.addEventListener('toast', (event) => {
    if (!toast) return;
    toast.textContent = event.detail.value || event.detail;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
  });

  // The setup wizard takes the time zone from the browser.
  document.querySelectorAll('input[data-browser-tz]').forEach((input) => {
    try {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (zone && !input.dataset.keep) input.value = zone;
    } catch (e) { /* keep the server's default */ }
  });

  // Refresh times: the custom field only matters when "Custom" is picked.
  document.querySelectorAll('[data-frequency]').forEach((group) => {
    const custom = document.querySelector(group.dataset.frequency);
    const sync = () => {
      const picked = group.querySelector('input:checked');
      if (custom) custom.hidden = !(picked && picked.value === 'custom');
    };
    group.addEventListener('change', sync);
    sync();
  });

  // Opening an article (in a new tab) dims it here straight away; the server records it as read.
  const markRead = (event) => {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const link = event.target.closest('a[href*="/open/"]');
    if (!link) return;
    const item = link.closest('.item');
    if (!item || item.classList.contains('read')) return;
    item.classList.add('read');
    const meta = item.querySelector('.meta');
    if (meta && !meta.querySelector('.read-tag')) {
      const fresh = meta.querySelector('.tag.fresh');
      if (fresh) fresh.remove();
      meta.append(Object.assign(document.createElement('span'), { className: 'tag read-tag', textContent: 'Read' }));
    }
  };
  document.addEventListener('click', markRead);
  document.addEventListener('auxclick', markRead);

  // Small menus (<details class="menu">) close when tapping elsewhere or pressing Escape.
  const closeMenus = (except) => document.querySelectorAll('details.menu[open]').forEach((m) => { if (m !== except) m.open = false; });
  document.addEventListener('click', (event) => closeMenus(event.target.closest('details.menu')));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenus(); });

  // Filters on the feed: changing a select submits the form.
  document.querySelectorAll('form.filters select').forEach((select) => {
    select.addEventListener('change', () => select.form.requestSubmit());
  });

  // Installable on the home screen.
  if ('serviceWorker' in navigator && document.body.dataset.swUrl) {
    navigator.serviceWorker.register(document.body.dataset.swUrl).catch(() => {});
  }
})();
