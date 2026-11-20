'use strict';

const form = document.getElementById('loginForm');
const title = document.getElementById('loginTitle');
const submit = document.getElementById('submitLogin');
const toggle = document.getElementById('switchMode');
const errorText = document.getElementById('loginError');
let registering = false;

toggle.addEventListener('click', () => {
  registering = !registering;
  title.textContent = submit.textContent = registering ? 'Create account' : 'Sign in';
  document.title = title.textContent + ' — STEPS';
  document.getElementById('loginIntro').textContent = registering
    ? 'Use an email and password. You can start training immediately.'
    : 'View your training history and daily progress.';
  document.getElementById('switchLabel').textContent = registering ? 'Already registered?' : 'New to STEPS?';
  toggle.textContent = registering ? 'Sign in' : 'Create account';
  form.elements.password.autocomplete = registering ? 'new-password' : 'current-password';
  errorText.hidden = true;
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = toggle.disabled = true;
  errorText.hidden = true;
  try {
    const response = await fetch(registering ? '/api/register' : '/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: form.elements.email.value, password: form.elements.password.value})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message);
    form.reset();
    location.replace('/');
  } catch (error) {
    errorText.textContent = error.message || 'Unable to reach the local dashboard.';
    errorText.hidden = false;
  } finally {
    submit.disabled = toggle.disabled = false;
  }
});
