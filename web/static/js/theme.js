/* Theme controller — light, dark, and explicit system preference.
   The initial theme is resolved by the inline script in <head>, so this file
   only wires the control and reacts to OS-level changes. Preference is stored
   under "datashell-theme" (only when the visitor picks light or dark). */

(function () {
  'use strict';

  var STORAGE_KEY = 'datashell-theme';
  var root = document.documentElement;
  var meta = document.getElementById('themeColor');
  var media = window.matchMedia('(prefers-color-scheme: dark)');
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-theme-btn]'));

  var COLOR = { light: '#ffffff', dark: '#0b0c0e' };

  function storedPreference() {
    try {
      var value = localStorage.getItem(STORAGE_KEY);
      return value === 'light' || value === 'dark' ? value : null;
    } catch (err) {
      return null;
    }
  }

  function apply(theme) {
    root.setAttribute('data-theme', theme);
    if (meta) meta.setAttribute('content', COLOR[theme]);
  }

  function paintButtons(mode) {
    buttons.forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.themeBtn === mode));
    });
  }

  function select(mode) {
    var theme;
    if (mode === 'system') {
      try { localStorage.removeItem(STORAGE_KEY); } catch (err) { /* storage unavailable */ }
      theme = media.matches ? 'dark' : 'light';
    } else {
      try { localStorage.setItem(STORAGE_KEY, mode); } catch (err) { /* storage unavailable */ }
      theme = mode;
    }
    apply(theme);
    paintButtons(mode);
  }

  // A stored preference is an explicit choice; otherwise the system decides.
  var preference = storedPreference();
  apply(preference || (media.matches ? 'dark' : 'light'));
  paintButtons(preference || 'system');

  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      select(button.dataset.themeBtn);
    });
  });

  function onSystemChange(event) {
    if (storedPreference()) return;
    apply(event.matches ? 'dark' : 'light');
    paintButtons('system');
  }

  if (typeof media.addEventListener === 'function') {
    media.addEventListener('change', onSystemChange);
  } else if (typeof media.addListener === 'function') {
    media.addListener(onSystemChange);
  }
})();
