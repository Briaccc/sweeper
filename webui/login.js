'use strict';
translatePage();
const form = document.getElementById('f');
const field = document.getElementById('t');
const error = document.getElementById('err');
const button = document.getElementById('go');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.textContent = '';
  button.disabled = true;
  button.textContent = t('Vérification…');
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: field.value.trim() }),
    });
    if (response.ok) {
      location.replace('/');          // the cookie is set: reload the app
      return;
    }
    const data = await response.json().catch(() => ({}));
    error.textContent = data.erreur || t('Jeton refusé.');
    field.select();
  } catch (e) {
    error.textContent = t('Serveur injoignable : ') + e.message;
  } finally {
    button.disabled = false;
    button.textContent = t('Entrer');
  }
});
