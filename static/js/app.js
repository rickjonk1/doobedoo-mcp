// doobedoo MCP — kleine stukjes interactie. Geen inline scripts (CSP).
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  // Kopiëren
  document.querySelectorAll("[data-kopieer]").forEach((knop) => {
    knop.addEventListener("click", async () => {
      const doel = document.getElementById(knop.dataset.kopieer);
      const tekst = doel ? (doel.value || doel.textContent).trim() : "";
      try {
        await navigator.clipboard.writeText(tekst);
        const oud = knop.textContent;
        knop.textContent = "Gekopieerd";
        setTimeout(() => (knop.textContent = oud), 1600);
      } catch (_) {
        if (doel && doel.select) doel.select();
      }
    });
  });

  // Sheets openen/sluiten
  document.querySelectorAll("[data-open]").forEach((knop) => {
    knop.addEventListener("click", () => document.getElementById(knop.dataset.open)?.showModal());
  });
  document.querySelectorAll("[data-sluit]").forEach((knop) => {
    knop.addEventListener("click", () => knop.closest("dialog")?.close());
  });

  // Schakelaars versturen hun formulier direct
  document.querySelectorAll("form[data-schakel] .schakel").forEach((s) => {
    s.addEventListener("change", () => s.form.submit());
  });

  // Pull-down-menu's: sluiten bij klik ernaast of Escape
  const sluitMenus = (behalve) => document.querySelectorAll("details.menu[open]").forEach((d) => {
    if (d !== behalve) d.removeAttribute("open");
  });
  document.addEventListener("click", (e) => sluitMenus(e.target.closest("details.menu")));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") sluitMenus(null); });

  // Bevestigen bij onomkeerbare acties
  document.querySelectorAll("form[data-bevestig]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.bevestig)) e.preventDefault();
    });
  });

  // Database: verbinding testen en bedrijven tonen
  const dbForm = document.getElementById("db-formulier");
  if (dbForm) {
    const testKnop = document.getElementById("db-testen");
    const opslaan = document.getElementById("db-opslaan");
    const melding = document.getElementById("db-melding");
    const lijst = document.getElementById("db-bedrijven");
    const blok = document.getElementById("db-bedrijven-blok");

    const reset = () => {
      opslaan.disabled = true;
      blok.classList.add("verborgen");
      melding.classList.add("verborgen");
    };
    dbForm.querySelectorAll("input").forEach((i) => i.addEventListener("input", reset));

    testKnop.addEventListener("click", async () => {
      testKnop.disabled = true;
      testKnop.textContent = "Bezig…";
      melding.className = "melding verborgen";
      try {
        const data = Object.fromEntries(new FormData(dbForm));
        const r = await fetch(dbForm.dataset.test, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(data),
          credentials: "same-origin",
        });
        const uit = await r.json();
        if (!uit.ok) throw new Error(uit.fout || "Verbinden mislukt.");
        lijst.replaceChildren(
          ...uit.bedrijven.map((b) => {
            const label = document.createElement("label");
            label.className = "vinkje";
            const input = document.createElement("input");
            input.type = "checkbox";
            input.name = "bedrijf";
            input.value = b.id;
            input.checked = true;
            const naam = document.createElement("span");
            naam.textContent = b.naam;
            label.append(input, naam);
            return label;
          })
        );
        blok.classList.remove("verborgen");
        melding.textContent = `Verbonden. ${uit.bedrijven.length} bedrijf${uit.bedrijven.length === 1 ? "" : "en"} gevonden.`;
        melding.className = "melding melding--ok";
        opslaan.disabled = false;
      } catch (e) {
        melding.textContent = e.message;
        melding.className = "melding melding--fout";
      } finally {
        testKnop.disabled = false;
        testKnop.textContent = "Test verbinding";
      }
    });
  }

  // Codevelden: alleen cijfers
  document.querySelectorAll("input[data-code]").forEach((i) => {
    i.addEventListener("input", () => (i.value = i.value.replace(/\D/g, "").slice(0, 6)));
  });
})();
