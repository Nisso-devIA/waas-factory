/* contact.js — Formulaire de demande / devis avancé, servi en externe (CSP: script-src 'self').
   Le slug est lu depuis <body data-plan-slug="...">. Envoie POST /api/contact/{slug}.
   Injecté sur le site À L'ACTIVATION quand l'option "form" a été achetée. */
(function () {
  var root = document.getElementById('va-form2-form');
  if (!root) return;
  var SLUG = (document.body.getAttribute('data-plan-slug') || '').trim();

  function val(name) {
    var el = root.querySelector('[name=' + name + ']');
    return el ? String(el.value || '').trim() : '';
  }

  root.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = document.getElementById('va-form2-submit');
    if (!val('name') || !val('phone') || !val('email') || !val('message')) {
      alert('Merci de remplir votre nom, téléphone, email et votre demande.');
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Envoi...';
    fetch('/api/contact/' + encodeURIComponent(SLUG), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: val('name'),
        phone: val('phone'),
        email: val('email'),
        subject: val('subject'),
        message: val('message')
      })
    })
      .then(function (res) { if (!res.ok) throw new Error('http'); return res.json(); })
      .then(function () {
        root.style.display = 'none';
        document.getElementById('va-form2-success').style.display = 'block';
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = 'Envoyer ma demande';
        alert('Un souci est survenu, réessayez dans un instant.');
      });
  });
})();
