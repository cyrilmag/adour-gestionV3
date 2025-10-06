// ------------------------------
// Adour Gestion - UI helpers
// ------------------------------
(function () {
  // Util: query one
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ------------------------------
  // Search modal (si présent dans le DOM)
  // ------------------------------
  const searchModal = $("#searchModal");
  const openSearchBtn = $("#openSearch");
  const clearSearchBtn = $("#clearSearch");

  if (openSearchBtn && searchModal) {
    openSearchBtn.addEventListener("click", () => {
      try { searchModal.showModal(); } catch { /* fallback */ }
      const input = $("#searchInput", searchModal) || $("#searchInput");
      if (input) input.focus();
    });
  }

  if (clearSearchBtn) {
    clearSearchBtn.addEventListener("click", () => {
      const url = new URL(location.href);
      url.searchParams.delete("q");
      url.searchParams.set("page", "1");
      location.href = url.toString();
    });
  }

  // ------------------------------
  // Effacer filtres par colonnes (si bouton présent)
  // ------------------------------
  $$(".clear-filters").forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = new URL(location.href);
      // supprime tous les paramètres c_<colonne>=...
      [...url.searchParams.keys()]
        .filter((k) => k.startsWith("c_"))
        .forEach((k) => url.searchParams.delete(k));
      url.searchParams.set("page", "1");
      location.href = url.toString();
    });
  });

  // ------------------------------
  // Barre de scroll horizontale en haut du tableau
  // (synchronisée avec le wrapper principal)
  // ------------------------------
  (function syncTopScrollbar() {
    const wrapper = document.querySelector("[data-table-wrapper]");
    const topbar  = document.querySelector("[data-hscroll-top]");
    if (!wrapper || !topbar) return;

    // élément interne servant à donner la même largeur scrollable
    const inner = document.createElement("div");
    inner.style.height = "1px";
    topbar.appendChild(inner);

    function syncWidths() {
      // largeur scrollable du wrapper = offset scrollWidth
      inner.style.width = wrapper.scrollWidth + "px";
    }

    function syncFromWrapper() {
      if (Math.abs(topbar.scrollLeft - wrapper.scrollLeft) > 1) {
        topbar.scrollLeft = wrapper.scrollLeft;
      }
    }

    function syncFromTop() {
      if (Math.abs(wrapper.scrollLeft - topbar.scrollLeft) > 1) {
        wrapper.scrollLeft = topbar.scrollLeft;
      }
    }

    wrapper.addEventListener("scroll", syncFromWrapper);
    topbar.addEventListener("scroll", syncFromTop);

    // Ajuste la largeur du topbar si la table change
    if (window.ResizeObserver) {
      new ResizeObserver(syncWidths).observe(wrapper);
    }
    // premier calcul
    syncWidths();

    // ------------------------------
    // Adapter la hauteur visible à la fenêtre
    // pour que la barre du haut soit utile sans scroller
    // ------------------------------
    function fitHeight() {
      const rect = wrapper.getBoundingClientRect();
      const bottomPadding = 80; // réserve pour pagination/boutons sous la table
      const available = window.innerHeight - rect.top - bottomPadding;
      if (available > 200) {
        wrapper.style.maxHeight = available + "px";
      }
    }

    window.addEventListener("resize", fitHeight);
    window.addEventListener("orientationchange", fitHeight);
    // laisser le layout s'installer avant de mesurer
    setTimeout(fitHeight, 0);
  })();

  // ------------------------------
  // Optionnel: calculer automatiquement page_size
  // Ajoute ?auto_pagesize=1 dans l'URL pour activer.
  // ------------------------------
  (function autoPageSize() {
    const params = new URLSearchParams(location.search);
    if (params.get("auto_pagesize") !== "1") return;

    const wrapper = document.querySelector("[data-table-wrapper]");
    const table = wrapper ? wrapper.querySelector("table") : null;
    if (!wrapper || !table) return;

    function computeAndNavigate() {
      const body = table.tBodies && table.tBodies[0] ? table.tBodies[0] : null;
      const rows = body ? Array.from(body.rows) : [];
      const firstRow = rows.find(r => r.offsetHeight > 0);
      const rowH = firstRow ? firstRow.getBoundingClientRect().height : 28;

      const headerH = table.tHead ? table.tHead.getBoundingClientRect().height : 40;
      const available = wrapper.clientHeight - headerH - 8; // petite marge
      const perPage = Math.max(10, Math.floor(available / Math.max(22, rowH)));

      const cur = params.get("page_size");
      if (perPage && String(perPage) !== cur) {
        params.set("page_size", String(perPage));
        params.set("page", "1");
        location.search = params.toString();
      }
    }

    setTimeout(computeAndNavigate, 60);
  })();
})();
