(async () => {
  const status = document.getElementById('status');
  const token = window.location.hash.slice(1);
  history.replaceState(null, '', '/invite');
  if (!token) {
    status.textContent = 'This invitation link is incomplete.';
    return;
  }
  const body = new URLSearchParams({ invitation_token: token });
  try {
    const response = await fetch('/auth/enrollment', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    });
    if (!response.ok) {
      status.textContent = 'This invitation is invalid or expired.';
      return;
    }
    window.location.replace('/auth/google/start');
  } catch (_error) {
    status.textContent = 'Could not start sign-in. Please try again.';
  }
})();
