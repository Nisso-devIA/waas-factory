(() => {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("slug");
  const sessionId = params.get("session_id") || "";
  const siteLink = document.getElementById("site-link");
  if (!siteLink) return;
  if (slug && /^[a-z0-9][a-z0-9-]{0,80}$/.test(slug)) {
    siteLink.href = "/" + slug + "/";
    if (sessionId) {
      fetch(
        "/api/upload-info?slug=" +
          encodeURIComponent(slug) +
          "&session_id=" +
          encodeURIComponent(sessionId)
      )
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data || !data.upload_url) return;
          const box = document.getElementById("upload-box");
          const a = document.getElementById("upload-link");
          const t = document.getElementById("upload-url-text");
          if (!box || !a || !t) return;
          a.href = data.upload_url;
          t.textContent = data.upload_url;
          box.classList.remove("hidden");
        })
        .catch(() => {});
    }
  }
})();
