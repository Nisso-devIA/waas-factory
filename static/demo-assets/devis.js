/* devis.js — Configurateur de devis servi en externe (CSP nginx : script-src 'self').
   Le slug est lu depuis <body data-plan-slug="...">. Envoie POST /api/quote/{slug}.
   L'estimation affichée est cosmétique ; le serveur (pricing.py) recalcule et fait foi. */
(function () {
  var root = document.getElementById('va-form');
  if (!root) return;
  var SLUG = (document.body.getAttribute('data-plan-slug') || '').trim();
  var fmt = new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 });
  var T = { vitrine: { price: 890, pagesIncl: 5, score: 2, minTier: 2 }, ecom: { price: 2490, pagesIncl: 8, score: 5, minTier: 2 } };
  var OPTS = [
    { id: 'rdv', name: 'Prise de rendez-vous', price: 250, score: 2, minTier: 2 },
    { id: 'blog', name: 'Blog / actualités', price: 200, score: 2, minTier: 2 },
    { id: 'galerie', name: 'Galerie / réalisations', price: 150, score: 1, minTier: 1 },
    { id: 'form', name: 'Formulaire avancé', price: 120, score: 1, minTier: 1 },
    { id: 'avis', name: 'Avis Google', price: 90, score: 1, minTier: 2 },
    { id: 'multi', name: 'Multilingue', price: 300, score: 2, minTier: 2, qty: 'lang' },
    { id: 'espace', name: 'Espace client', price: 400, score: 3, minTier: 3 },
    { id: 'redac', name: 'Rédaction des contenus', price: 60, score: 1, minTier: 1, qty: 'pages' },
    { id: 'logo', name: 'Logo & identité', price: 350, score: 1, minTier: 1 },
    { id: 'photos', name: 'Visuels premium', price: 120, score: 1, minTier: 1 }
  ];
  var TIERS = [{ id: 'essentiel', name: 'Essentiel', price: 29 }, { id: 'visibilite', name: 'Visibilité', price: 49 }, { id: 'croissance', name: 'Croissance', price: 99 }];
  var LEVELS = [[3, 'Simple'], [7, 'Standard'], [12, 'Avancé'], [1e9, 'Premium']];
  var langs = 2;

  document.getElementById('va-opts').innerHTML = OPTS.map(function (o) {
    var suf = o.qty === 'lang' ? '/langue' : (o.qty === 'pages' ? '/page' : '');
    return '<label class="va-opt"><input type="checkbox" value="' + o.id + '"><span class="va-ck"></span><span class="va-opt-n">' + o.name + '</span><span class="va-opt-p">+' + fmt.format(o.price) + suf + '</span></label>';
  }).join('');
  document.getElementById('va-tiers').innerHTML = TIERS.map(function (t) {
    var tag = t.id === 'visibilite' ? ' <i>conseillé</i>' : '';
    return '<label class="va-tier"><input type="radio" name="tier" value="' + t.id + '"' + (t.id === 'visibilite' ? ' checked' : '') + '><span class="va-tier-n">' + t.name + tag + '</span><span class="va-tier-p">' + fmt.format(t.price) + '<small>/mois</small></span></label>';
  }).join('');

  function sel() {
    var type = (root.querySelector('input[name=type]:checked') || {}).value || 'vitrine';
    var opts = {};
    root.querySelectorAll('#va-opts input:checked').forEach(function (i) { opts[i.value] = true; });
    var tier = (root.querySelector('input[name=tier]:checked') || {}).value || 'visibilite';
    return { type: type, options: opts, langs: langs, tier: tier, billing: 'mensuel' };
  }
  function compute() {
    var s = sel(), ty = T[s.type], creation = ty.price, score = ty.score, minTier = ty.minTier, pages = ty.pagesIncl;
    OPTS.forEach(function (o) {
      if (!s.options[o.id]) return;
      var c = o.price;
      if (o.qty === 'pages') c = o.price * pages;
      if (o.qty === 'lang') c = o.price * langs;
      creation += c; score += o.score;
      if (o.minTier > minTier) minTier = o.minTier;
    });
    var level = 'Premium';
    for (var i = 0; i < LEVELS.length; i++) { if (score <= LEVELS[i][0]) { level = LEVELS[i][1]; break; } }
    var chosen = TIERS.filter(function (t) { return t.id === s.tier; })[0] || TIERS[1];
    return { creation: creation, monthly: chosen.price, level: level };
  }
  function refresh() {
    var r = compute();
    document.getElementById('va-creation').textContent = fmt.format(r.creation);
    document.getElementById('va-monthly').textContent = fmt.format(r.monthly);
    document.getElementById('va-level').textContent = 'Niveau ' + r.level;
    document.getElementById('va-lang').hidden = !root.querySelector('#va-opts input[value=multi]:checked');
  }
  root.addEventListener('change', refresh);
  document.getElementById('va-lang').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-d]');
    if (!b) return;
    langs = Math.max(2, Math.min(5, langs + parseInt(b.getAttribute('data-d'), 10)));
    document.getElementById('va-langv').textContent = langs;
    refresh();
  });
  refresh();

  root.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = document.getElementById('va-submit');
    btn.disabled = true; btn.textContent = 'Envoi...';
    fetch('/api/quote/' + encodeURIComponent(SLUG), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sel())
    })
      .then(function (res) { if (!res.ok) throw new Error('http'); return res.json(); })
      .then(function () { root.style.display = 'none'; document.getElementById('va-success').style.display = 'block'; })
      .catch(function () { btn.disabled = false; btn.textContent = 'Demander mon devis'; alert('Un souci est survenu, réessayez dans un instant.'); });
  });
})();
