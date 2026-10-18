(() => {
  const form = document.getElementById("form");
  const filesInput = document.getElementById("files");
  const captions = document.getElementById("captions");
  const status = document.getElementById("status");
  const submitBtn = document.getElementById("submit");
  if (!form || !filesInput || !captions || !status || !submitBtn) return;

  const maxBatch = Number(form.dataset.maxBatch || "8");

  filesInput.addEventListener("change", () => {
    captions.innerHTML = "";
    const list = Array.from(filesInput.files || []).slice(0, maxBatch);
    list.forEach((f, i) => {
      const wrap = document.createElement("label");
      wrap.className = "block text-sm";
      const span = document.createElement("span");
      span.className = "font-medium text-slate-700";
      span.textContent = f.name || "Photo " + (i + 1);
      const input = document.createElement("input");
      input.type = "text";
      input.name = "caption";
      input.maxLength = 200;
      input.placeholder = "Légende (optionnel)";
      input.className =
        "mt-1 w-full border border-slate-200 rounded-xl px-3 py-2 text-sm";
      wrap.appendChild(span);
      wrap.appendChild(input);
      captions.appendChild(wrap);
    });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const list = Array.from(filesInput.files || []);
    if (!list.length) {
      status.textContent = "Choisissez au moins une photo.";
      return;
    }
    if (list.length > maxBatch) {
      status.textContent = "Maximum " + maxBatch + " photos par envoi.";
      return;
    }
    const fd = new FormData();
    const caps = captions.querySelectorAll('input[name="caption"]');
    list.slice(0, maxBatch).forEach((f, i) => {
      fd.append("photos", f, f.name);
      fd.append("captions", (caps[i] && caps[i].value) || "");
    });
    submitBtn.disabled = true;
    status.textContent = "Envoi en cours…";
    try {
      const res = await fetch(window.location.pathname, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        status.textContent = data.detail || "Erreur " + res.status;
      } else {
        status.textContent =
          (data.count || 0) + " photo(s) publiée(s). Merci !";
        filesInput.value = "";
        captions.innerHTML = "";
      }
    } catch (err) {
      status.textContent = "Réseau indisponible. Réessayez.";
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
