/* rdv.js — Formulaire de demande de rendez-vous, servi en externe (CSP: script-src 'self').
   Le slug est lu depuis <body data-plan-slug="...">. Envoie POST /api/rdv/{slug}.
   Injecté sur le site À L'ACTIVATION quand l'option "rdv" a été achetée. */
(function () {
  var root = document.getElementById('va-rdv-form');
  if (!root) return;
  var SLUG = (document.body.getAttribute('data-plan-slug') || '').trim();

  function val(name) {
    var el = root.querySelector('[name=' + name + ']');
    return el ? String(el.value || '').trim() : '';
  }

  root.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = document.getElementById('va-rdv-submit');
    if (!val('name') || !val('phone')) {
      alert('Merci d’indiquer au moins votre nom et votre téléphone.');
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Envoi...';
    fetch('/api/rdv/' + encodeURIComponent(SLUG), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: val('name'), phone: val('phone'), slot: val('slot'), message: val('message') })
    })
      .then(function (res) { if (!res.ok) throw new Error('http'); return res.json(); })
      .then(function () {
        root.style.display = 'none';
        document.getElementById('va-rdv-success').style.display = 'block';
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = 'Demander un rendez-vous';
        alert('Un souci est survenu, réessayez dans un instant.');
      });
  });
})();
