// AI module — sidebar summary line + modal "brief" (§B.1.3, which SUPERSEDES
// the roadmap's "collapsible sidebar card": 22 comps with quotes in a ~300px
// column is unreadable, and the primary user of this surface for the next
// few weeks is the person tuning the rubric, not the client).
//
// Self-contained IIFE. No imports from map.js — reads exactly one accessor,
// window.__aiGetVisibleCompContext (the map.js seam, §B.3). Wrapped in
// try/catch end to end: this file must be incapable of breaking comp
// loading, filtering, rating, or any other existing path.
//
// Deleting this file + ai-card.css + the 2 index.html lines + the 2 map.js
// lines (the stash + the accessor) must leave the app byte-identical.
(function () {
  "use strict";

  try {
    const mount = document.getElementById("ai-card-mount");
    if (!mount) return;   // seam absent -> feature simply does not exist

    const CONDITION_ORDER = ["new-build", "remodeled", "updated", "original-dated", "gut-job", "unknown"];
    // Compact labels for the one-line sidebar summary only (§B.1.3(a)'s mockup
    // abbreviates "original-dated" to "dated" to keep the line to one row).
    const CONDITION_LABEL_SHORT = {
      "new-build": "new-build",
      "remodeled": "remodeled",
      "updated": "updated",
      "original-dated": "dated",
      "gut-job": "gut-job",
      "unknown": "unknown",
    };
    const EXCLUDED_REASON_LABEL = {
      no_avm_permission: "no AVM permission",
      outside_drawn_area: "outside the drawn area",
      no_geometry: "no geometry",
      no_remarks: "no remarks",
      not_found: "not found",
    };

    let state = "idle";        // idle | loading | error | done
    let lastResponse = null;   // full API response from the last successful run
    let ratingByKey = null;    // Map(comp_address_key -> user_rating), snapshotted at request time
    let seenIsAdmin = null;
    let seenAreaId = undefined;
    let modalEl = null;
    let modalKeydownHandler = null;

    function ctx() {
      try {
        return typeof window.__aiGetVisibleCompContext === "function"
          ? window.__aiGetVisibleCompContext()
          : null;
      } catch {
        return null;
      }
    }

    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
      ));
    }

    function fmtPrice(price) {
      const n = Number(price);
      return price === null || price === undefined || Number.isNaN(n) ? "?" : "$" + Math.round(n).toLocaleString("en-US");
    }

    function truncate(s, n) {
      const str = String(s || "");
      return str.length > n ? str.slice(0, n) + "…" : str;
    }

    // --- Sidebar row (§B.1.3(a)) -----------------------------------------

    function renderSidebar() {
      const c = ctx();
      if (!c || !c.isAdmin) {
        mount.innerHTML = "";
        return;
      }

      if (state === "loading") {
        mount.innerHTML =
          '<div class="ai-card-row">' +
          '<span class="ai-card-label">AI read</span>' +
          '<button type="button" class="ai-card-btn" disabled>' +
          '<span class="ai-card-spinner" aria-hidden="true"></span>Reading…</button>' +
          "</div>";
        return;
      }

      if (state === "error") {
        mount.innerHTML =
          '<div class="ai-card-row ai-card-row-error">' +
          '<span class="ai-card-error-text">Couldn’t read the comps right now.</span>' +
          '<button type="button" class="ai-card-btn ai-card-retry-btn">Retry</button>' +
          "</div>";
        const retryBtn = mount.querySelector(".ai-card-retry-btn");
        if (retryBtn) retryBtn.addEventListener("click", () => { void runRead(); });
        return;
      }

      if (state === "done" && lastResponse) {
        const mix = lastResponse.mix || {};
        const parts = CONDITION_ORDER
          .filter((k) => mix[k] > 0)
          .map((k) => `${mix[k]} ${esc(CONDITION_LABEL_SHORT[k] || k)}`);
        const mixText = parts.length ? parts.join(" · ") : "no conditions read";
        mount.innerHTML =
          '<div class="ai-card-row">' +
          `<span class="ai-card-mix">${mixText}</span>` +
          '<span class="ai-card-actions">' +
          '<button type="button" class="ai-card-reread-btn" title="Re-read comps" aria-label="Re-read comps">↻</button>' +
          '<button type="button" class="ai-card-btn ai-card-brief-btn">see brief</button>' +
          "</span></div>";
        const rereadBtn = mount.querySelector(".ai-card-reread-btn");
        if (rereadBtn) rereadBtn.addEventListener("click", () => { void runRead(); });
        const briefBtn = mount.querySelector(".ai-card-brief-btn");
        if (briefBtn) briefBtn.addEventListener("click", () => openModal());
        return;
      }

      // idle -- no run yet
      mount.innerHTML =
        '<div class="ai-card-row">' +
        '<span class="ai-card-label">AI read</span>' +
        '<button type="button" class="ai-card-btn ai-card-read-btn">Read comps</button>' +
        "</div>";
      const readBtn = mount.querySelector(".ai-card-read-btn");
      if (readBtn) readBtn.addEventListener("click", () => { void runRead(); });
    }

    // --- Fetch (§B.1.5) ---------------------------------------------------

    async function runRead() {
      if (state === "loading") return;   // button is disabled while in flight, but guard anyway
      const c = ctx();
      if (!c || !c.isAdmin) {
        state = "idle";
        renderSidebar();
        return;
      }
      const comps = Array.isArray(c.comps) ? c.comps : [];
      // Scope rule (spec §A.3.5): never ASK for comps outside the drawn area, even
      // when the OAC toggle is on. The server re-derives this and excludes them
      // regardless -- this is the client half, so we don't ship 200 keys just to
      // have the server throw them away (and risk the 500-key cap on a big area).
      const inArea = comps.filter((cp) => cp && !cp.extra?.is_outside_polygon);
      const compKeys = inArea.map((cp) => cp.comp_address_key).filter(Boolean);
      if (!c.areaId || compKeys.length === 0) {
        return;   // nothing to read -- leave the row as-is
      }

      const requestAreaId = c.areaId;
      closeModal();
      state = "loading";
      renderSidebar();

      const ratingSnapshot = new Map(
        comps.filter((cp) => cp && cp.comp_address_key).map((cp) => [cp.comp_address_key, cp.user_rating || null])
      );

      try {
        const res = await fetch("/api/ai/read-comps", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(c.headers || {}) },   // FROM THE SEAM
          credentials: "same-origin",
          body: JSON.stringify({ area_id: requestAreaId, comp_keys: compKeys }),
        });

        // The loaded area may have changed while this request was in flight --
        // discard a response that no longer matches the currently-loaded area.
        const stillCurrent = () => (ctx() || {}).areaId === requestAreaId;

        if (res.status === 403) {
          // Role changed under us -- hide the card entirely, not just an error row.
          if (!stillCurrent()) return;
          state = "idle";
          lastResponse = null;
          ratingByKey = null;
          mount.innerHTML = "";
          return;
        }
        if (!res.ok) throw new Error(`AI read failed: ${res.status}`);

        const data = await res.json();
        if (!stillCurrent()) return;
        lastResponse = data;
        ratingByKey = ratingSnapshot;
        state = "done";
        renderSidebar();
      } catch (err) {
        if ((ctx() || {}).areaId !== requestAreaId) return;
        state = "error";
        renderSidebar();
      }
    }

    // --- Modal (§B.1.3(b)) — own DOM node, own classes, never shares #auth-modal ---

    function closeModal() {
      if (modalEl) {
        modalEl.remove();
        modalEl = null;
      }
      if (modalKeydownHandler) {
        document.removeEventListener("keydown", modalKeydownHandler);
        modalKeydownHandler = null;
      }
    }

    function excludedGroupCounts(excluded) {
      const counts = {};
      for (const e of excluded || []) {
        const r = e && e.reason;
        if (!r) continue;
        counts[r] = (counts[r] || 0) + 1;
      }
      return counts;
    }

    function buildHeaderLine(meta, excluded) {
      const quals = [];
      if (meta.truncated) quals.push(`showing first ${meta.read} of ${meta.permitted}`);
      if (meta.partial) quals.push("timed out");
      const base = `Read ${meta.read} of ${meta.permitted} comps`;
      const counts = excludedGroupCounts(excluded);
      const reasonParts = Object.keys(counts).map((r) => `${counts[r]} ${EXCLUDED_REASON_LABEL[r] || r}`);
      return [base, ...quals, ...reasonParts].join(" · ");
    }

    function renderCompRow(comp) {
      const rating = ratingByKey ? ratingByKey.get(comp.comp_address_key) : null;
      const badge = rating === "good"
        ? '<span class="ai-modal-badge ai-modal-badge-good">GOOD</span>'
        : rating === "bad"
          ? '<span class="ai-modal-badge ai-modal-badge-bad">BAD</span>'
          : "";
      const quoteHtml = comp.condition_quote
        ? `<div class="ai-modal-quote" title="${esc(comp.condition_quote)}">“${esc(truncate(comp.condition_quote, 90))}”</div>`
        : "";
      const flagsHtml = (comp.flags || [])
        .map((f) => `<div class="ai-modal-flag" title="${esc(f.quote)}">⚑ ${esc(f.tag)}: “${esc(truncate(f.quote, 80))}”</div>`)
        .join("");
      return (
        '<div class="ai-modal-comp">' +
        '<div class="ai-modal-comp-head">' +
        `<span class="ai-modal-comp-cond">${esc(comp.condition)}</span>` +
        `<span class="ai-modal-comp-price">${esc(fmtPrice(comp.price))}</span>` +
        `<span class="ai-modal-comp-addr">${esc(comp.address)}</span>` +
        badge +
        "</div>" +
        quoteHtml +
        flagsHtml +
        "</div>"
      );
    }

    // ⛔ An excluded comp is still a COMP. It belongs in the brief.
    // The permit_avm gate stops us sending a flagged listing's TEXT to the model
    // -- it is not a reason to make the comp disappear from the user's own brief.
    // Before this, `excluded` comps were demoted to a text footer, so an area
    // whose comps were ALL flagged rendered an EMPTY list and read as broken
    // (KK, preview, 2026-07-14: "Read 0 of 0 comps · 2 no AVM permission").
    // Same shape as a read comp -- price, address, rating -- minus the one thing
    // we genuinely do not have: a condition tag. No quote, because we never read
    // it. Nothing hidden, nothing invented.
    function renderExcludedCompRow(e) {
      const rating = ratingByKey ? ratingByKey.get(e.comp_address_key) : null;
      const badge = rating === "good"
        ? '<span class="ai-modal-badge ai-modal-badge-good">GOOD</span>'
        : rating === "bad"
          ? '<span class="ai-modal-badge ai-modal-badge-bad">BAD</span>'
          : "";
      const why = esc(EXCLUDED_REASON_LABEL[e.reason] || e.reason || "not read");
      return (
        '<div class="ai-modal-comp ai-modal-comp-unread">' +
        '<div class="ai-modal-comp-head">' +
        '<span class="ai-modal-comp-cond ai-modal-comp-cond-unread">not read</span>' +
        `<span class="ai-modal-comp-price">${esc(fmtPrice(e.price))}</span>` +
        `<span class="ai-modal-comp-addr">${esc(e.address || e.comp_address_key || "?")}</span>` +
        badge +
        "</div>" +
        `<div class="ai-modal-comp-unread-why">${why}</div>` +
        "</div>"
      );
    }

    function renderDroppedRow(d) {
      const addr = d.address ? esc(d.address) : "(unattributed)";
      const tag = d.tag ? esc(d.tag) : "(none)";
      const quote = d.quote ? `“${esc(truncate(d.quote, 90))}”` : "(no quote)";
      return (
        '<div class="ai-modal-dropped-row">' +
        `<span class="ai-modal-dropped-reason">${esc(d.reason)}</span>` +
        `<span class="ai-modal-dropped-tag">${tag}</span>` +
        `<span class="ai-modal-dropped-addr">${addr}</span>` +
        `<span class="ai-modal-dropped-quote">${quote}</span>` +
        "</div>"
      );
    }

    function openModal() {
      if (!lastResponse) return;
      closeModal();

      const data = lastResponse;
      const meta = data.meta || {};
      const mix = data.mix || {};
      const comps = Array.isArray(data.comps) ? data.comps : [];
      const excluded = Array.isArray(data.excluded) ? data.excluded : [];
      const rejected = Array.isArray(data.rejected) ? data.rejected : [];

      const mixHtml = CONDITION_ORDER
        .filter((k) => mix[k] > 0)
        .map((k) => `<span class="ai-modal-mix-item">${mix[k]} ${esc(k)}</span>`)
        .join(" · ");

      const byCondition = {};
      for (const comp of comps) {
        const k = CONDITION_ORDER.includes(comp.condition) ? comp.condition : "unknown";
        (byCondition[k] = byCondition[k] || []).push(comp);
      }
      // Read comps first, grouped by condition -- then the ones we couldn't read,
      // still in the list, still yours, just honestly labelled. An empty comp list
      // when every comp was gated is worse than useless: it reads as broken.
      const compsHtml =
        CONDITION_ORDER.flatMap((k) => (byCondition[k] || []).map(renderCompRow)).join("")
        + excluded.map(renderExcludedCompRow).join("");

      // §A.6.1 — rejections only happen at extraction time, never on a cache
      // hit. Any cached portion of this run has dropped tags we can't see.
      const cachedNote = meta.cached > 0
        ? '<div class="ai-modal-cached-note">(cached — re-read to see dropped tags)</div>'
        : "";
      const droppedHtml = rejected.length
        ? rejected.map(renderDroppedRow).join("")
        : '<div class="ai-modal-dropped-empty">Nothing dropped this run.</div>';

      const excludedFooter = excluded.length
        ? excluded
            .map((e) => `${esc(e.address || e.comp_address_key || "?")} (${esc(EXCLUDED_REASON_LABEL[e.reason] || e.reason)})`)
            .join(", ")
        : "none";

      const overlay = document.createElement("div");
      overlay.className = "ai-modal-overlay";
      overlay.innerHTML =
        '<div class="ai-modal-box" role="dialog" aria-modal="true" aria-label="AI read brief">' +
        '<button type="button" class="ai-modal-close" aria-label="Close">×</button>' +
        `<div class="ai-modal-header">${esc(buildHeaderLine(meta, excluded))}</div>` +
        `<div class="ai-modal-mix-line">${mixHtml || "(no conditions read)"}</div>` +
        `<div class="ai-modal-comps">${compsHtml || '<div class="ai-modal-empty">No comps read.</div>'}</div>` +
        '<details class="ai-modal-dropped-block">' +
        `<summary>Dropped tags (${rejected.length})</summary>` +
        cachedNote +
        `<div class="ai-modal-dropped-list">${droppedHtml}</div>` +
        "</details>" +
        '<button type="button" class="ai-modal-copy-btn">Copy raw JSON</button>' +
        '<div class="ai-modal-footer">' +
        `Excluded: ${excludedFooter} · prompt ${esc(meta.prompt_version)} · $${Number(meta.est_cost || 0).toFixed(4)} · ${meta.cached || 0} served from cache` +
        "</div>" +
        "</div>";

      document.body.appendChild(overlay);
      modalEl = overlay;

      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeModal();
      });
      const closeBtn = overlay.querySelector(".ai-modal-close");
      if (closeBtn) closeBtn.addEventListener("click", () => closeModal());

      const copyBtn = overlay.querySelector(".ai-modal-copy-btn");
      if (copyBtn) {
        copyBtn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
            const original = copyBtn.textContent;
            copyBtn.textContent = "Copied!";
            setTimeout(() => { copyBtn.textContent = original; }, 1500);
          } catch {
            copyBtn.textContent = "Copy failed";
          }
        });
      }

      modalKeydownHandler = (e) => {
        if (e.key === "Escape") closeModal();
      };
      document.addEventListener("keydown", modalKeydownHandler);
    }

    // --- Reactivity without touching map.js beyond the one accessor ------
    // map.js has no event hook for "auth resolved" / "area (un)loaded" in
    // this build (the seam is ONE accessor, nothing else), so a light poll
    // is how this fully decoupled file notices those transitions.
    function tick() {
      const c = ctx();
      const isAdmin = !!(c && c.isAdmin);
      const areaId = c ? c.areaId : null;
      if (isAdmin === seenIsAdmin && areaId === seenAreaId) return;

      const areaChanged = areaId !== seenAreaId;
      seenIsAdmin = isAdmin;
      seenAreaId = areaId;

      if (areaChanged) {
        // A different area is loaded (or cleared) -- any previous read is stale.
        state = "idle";
        lastResponse = null;
        ratingByKey = null;
        closeModal();
      }
      renderSidebar();
    }

    // Stop polling for users who will never see this card. The <script> tag is
    // unconditional in index.html, so without this a 1Hz timer would run forever
    // in every VA's and the client's browser -- on prod, where AI_ENABLED is off
    // and the feature does not exist. "Flag off => byte-identical" is a hard
    // guarantee, and this client never clears his cache.
    // Admins keep the poll (they need it to notice area switches). Everyone else
    // gets ~30s of grace for auth to resolve, then the timer is torn down.
    let ticks = 0;
    tick();
    const pollId = setInterval(() => {
      tick();
      if (!seenIsAdmin && ++ticks >= 30) clearInterval(pollId);   // auth resolved, not an admin -> done
    }, 1000);
  } catch (err) {
    try { console.error("[ai-card] init failed (non-fatal):", err); } catch { /* ignore */ }
  }
})();
