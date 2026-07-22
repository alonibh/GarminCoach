(() => {
  const sheet = document.getElementById('mobile-more');
  const toggle = document.querySelector('[data-more-toggle]');
  if (sheet && toggle) {
    const closeButtons = sheet.querySelectorAll('[data-more-close]');
    const panel = sheet.querySelector('.sheet-panel');
    let lastFocus = null;
    const setOpen = (open) => {
      sheet.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      document.body.classList.toggle('sheet-open', open);
      if (open) {
        lastFocus = document.activeElement;
        requestAnimationFrame(() => panel.querySelector('button, a')?.focus());
      } else if (lastFocus) lastFocus.focus();
    };
    toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
    closeButtons.forEach((button) => button.addEventListener('click', () => setOpen(false)));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !sheet.hidden) setOpen(false);
      if (event.key !== 'Tab' || sheet.hidden) return;
      const focusable = [...panel.querySelectorAll('button:not([disabled]), a[href]')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
  }
})();
