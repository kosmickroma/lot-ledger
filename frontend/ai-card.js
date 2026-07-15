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

    // §2.1 (docs/AI/CODER_SPEC_BRIEF_SCORECARD_2026-07-15.md) -- the ISD key
    // carries its own county prefix ("dcad:DALLAS ISD" / "tad:905"). Dallas +
    // Denton carry a real district NAME after the colon; Tarrant + Collin
    // carry only a numeric CODE, which is never a district name -- print
    // nothing for those and rely on the gold match indicator alone. Keyed
    // off the KEY's own prefix (never a county field), which is right for
    // both a comp row and the subject in a cross-county drawn area. Mirrors
    // map.js's _ISD_NAME_FORM_COUNTIES -- keep both in sync when a county
    // is added (this file has no import from map.js, so it's a hand copy).
    const ISD_NAME_FORM_PREFIXES = new Set(["dcad", "denton"]);

    let state = "idle";        // idle | loading | error | done
    let lastResponse = null;   // full API response from the last successful run
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
        // §2.0 (Fable) -- the facts-only scorecard needs no AI read, so a
        // failed read must not be a dead end. Retry stays; "see brief" is
        // added so the facts + gold matches are still reachable.
        mount.innerHTML =
          '<div class="ai-card-row ai-card-row-error">' +
          '<span class="ai-card-error-text">Couldn’t read the comps right now.</span>' +
          '<span class="ai-card-actions">' +
          '<button type="button" class="ai-card-btn ai-card-retry-btn">Retry</button>' +
          '<button type="button" class="ai-card-btn ai-card-brief-btn">see brief</button>' +
          "</span></div>";
        const retryBtn = mount.querySelector(".ai-card-retry-btn");
        if (retryBtn) retryBtn.addEventListener("click", () => { void runRead(); });
        const briefBtn = mount.querySelector(".ai-card-brief-btn");
        if (briefBtn) briefBtn.addEventListener("click", () => openModal());
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

      // idle -- no run yet (or nothing to read: e.g. every visible comp is
      // outside the drawn area, so runRead() left the row idle on purpose --
      // see the early return in runRead()). We still do NOT show a "Read
      // comps" button or auto-read here (KK: entering AI mode is the trigger,
      // and an ambient Vertex call on every admin area-load is forbidden by
      // design) -- but §2.0 (Fable) requires the facts-only scorecard to stay
      // reachable even when no read has ever run, so "see brief" is shown.
      mount.innerHTML =
        '<div class="ai-card-row">' +
        '<span class="ai-card-hint">not read yet</span>' +
        '<span class="ai-card-actions">' +
        '<button type="button" class="ai-card-btn ai-card-brief-btn">see brief</button>' +
        "</span></div>";
      const idleBriefBtn = mount.querySelector(".ai-card-brief-btn");
      if (idleBriefBtn) idleBriefBtn.addEventListener("click", () => openModal());
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
          mount.innerHTML = "";
          return;
        }
        if (!res.ok) throw new Error(`AI read failed: ${res.status}`);

        const data = await res.json();
        if (!stillCurrent()) return;
        lastResponse = data;
        state = "done";
        renderSidebar();
      } catch (err) {
        if ((ctx() || {}).areaId !== requestAreaId) return;
        state = "error";
        renderSidebar();
      }
    }

    // Task E (docs/AI/CODER_SPEC_FACTS_2026-07-14.md) — pressing AI mode is
    // the explicit human act that fires the read; map.js's _enableAiMode
    // calls this instead of requiring a second "Read comps" click. ONE SHOT:
    // runRead() has no retry loop of its own (a single fetch, no internal
    // re-attempt), so calling it once here is already the correct shape —
    // failure just lands in the existing error state with its manual Retry
    // button, same as today. The manual Read/Re-read buttons are untouched.
    window.__aiCardTriggerRead = () => { void runRead(); };

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

    // --- Facts (§2 of the scorecard spec) — deterministic, no model -------

    // §2.1 -- reimplements map.js's _isdDisplayName exactly (this file has no
    // imports from map.js, so it cannot call that function). Splits the ISD
    // key on ":" and returns the tail ONLY when the key's own county prefix
    // is a name-form county (dcad/denton) -- Tarrant ("tad:905") and Collin
    // ("collin:...") carry a numeric code, never a name, so those return
    // null and the caller falls back to the quiet "—" (gold indicator only).
    function isdDisplayName(isdKey) {
      const s = String(isdKey || "");
      const i = s.indexOf(":");
      if (i === -1) return null;
      const prefix = s.slice(0, i).toLowerCase();
      return ISD_NAME_FORM_PREFIXES.has(prefix) ? s.slice(i + 1) : null;
    }

    // §1.2 / §2.2 -- plain-equality fact comparator, mirroring map.js's
    // _compFacts() (same file cannot call it directly). same_subdivision
    // additionally requires the SAME county on both sides (a drawn area can
    // straddle a county line, and generic subdivision names like "OAK GROVE
    // PH 1" recur across counties) -- unknown county on either side means we
    // can't confirm the match, so it resolves to null (never asserted true).
    // Unknown (null on either side) is always null, never false: a false
    // "different" would be as much a lie as a false "same."
    function compFacts(subject, comp) {
      const subSub = subject ? (subject.cad_subdivision || null) : null;
      const compSub = comp ? (comp.cad_subdivision || null) : null;
      const subCounty = subject ? String(subject.county || "").trim().toLowerCase() : "";
      const compCounty = comp ? String(comp.parcel_county || "").trim().toLowerCase() : "";
      let same_subdivision;
      if (!subSub || !compSub) {
        same_subdivision = null;
      } else if (subSub !== compSub) {
        same_subdivision = false;
      } else if (!subCounty || !compCounty) {
        same_subdivision = null;
      } else {
        same_subdivision = subCounty === compCounty;
      }

      const subIsd = subject ? (subject.isd || null) : null;
      const compIsd = comp ? (comp.isd || null) : null;
      const same_isd = (subIsd && compIsd) ? (subIsd === compIsd) : null;

      return { same_subdivision, same_isd };
    }

    // §2.3 -- the AI read is a bonus layer, keyed by comp_address_key onto
    // the full visible set. Three outcomes: read (condition + quote),
    // excluded (gated before the model ever saw it, still a comp), or
    // neither (visible but never sent -- e.g. outside the drawn area). Only
    // "neither" renders no condition line at all; the facts render in all
    // three cases regardless.
    function aiLayerFor(comp, readByKey, excludedByKey) {
      const key = comp && comp.comp_address_key;
      if (key && readByKey.has(key)) return { kind: "read", data: readByKey.get(key) };
      if (key && excludedByKey.has(key)) return { kind: "excluded", data: excludedByKey.get(key) };
      return { kind: "none", data: null };
    }

    // Builds one scorecard row descriptor per visible comp. Pure data --
    // rendering and ordering are separate steps so the sort comparator can
    // read `same_subdivision`/`same_isd` without re-deriving them.
    function buildScoreRow(comp, subject, readByKey, excludedByKey) {
      const facts = compFacts(subject, comp);
      return {
        comp,
        same_subdivision: facts.same_subdivision,
        same_isd: facts.same_isd,
        schoolName: comp && comp.isd ? isdDisplayName(comp.isd) : null,
        ai: aiLayerFor(comp, readByKey, excludedByKey),
        // Explicit, non-price sort key (§2.4): the seam intentionally does
        // not carry the subject's lat/lng (kept minimal per the spec), so a
        // true distance_asc tie-break isn't derivable in this decoupled
        // file -- address is deterministic, stable, and never price.
        sortKey: String((comp && (comp.address || comp.comp_address_key)) || "").toLowerCase(),
      };
    }

    // §2.4 -- gold matches first (neighborhood match, then school match),
    // then everything else, in a fixed non-price order within each tier.
    // ⛔ Never price: the app's default sort is price_desc, and inheriting
    // ctx().comps' own order here would silently re-introduce it.
    function compareScoreRows(a, b) {
      const rank = (v) => (v === true ? 0 : 1);
      const subDiff = rank(a.same_subdivision) - rank(b.same_subdivision);
      if (subDiff !== 0) return subDiff;
      const isdDiff = rank(a.same_isd) - rank(b.same_isd);
      if (isdDiff !== 0) return isdDiff;
      if (a.sortKey < b.sortKey) return -1;
      if (a.sortKey > b.sortKey) return 1;
      return 0;
    }

    // One unified row renderer -- facts always render; the AI layer (§2.3)
    // is layered on only where present. Every CAD-derived string is esc()'d
    // (cad_subdivision and the ISD display text are free text, not trusted).
    function renderScoreRow(row) {
      const comp = row.comp;
      const rating = comp && comp.user_rating ? comp.user_rating : null;   // §2.1 -- LIVE rating, not a read-time snapshot
      const badge = rating === "good"
        ? '<span class="ai-modal-badge ai-modal-badge-good">GOOD</span>'
        : rating === "bad"
          ? '<span class="ai-modal-badge ai-modal-badge-bad">BAD</span>'
          : "";

      const neighGold = row.same_subdivision === true ? " ai-modal-fact-gold" : "";
      const schoolGold = row.same_isd === true ? " ai-modal-fact-gold" : "";
      const neighText = comp && comp.cad_subdivision ? esc(comp.cad_subdivision) : "—";
      // Collin + Tarrant carry a school CODE, not a name, so schoolName is null
      // there. A naked gold box around "—" reads as "it matched [nothing]" (KK,
      // preview 2026-07-15). When it matches but we have no name to show, say
      // "same district" so the gold means something; otherwise the quiet "—".
      // (The real district NAME for code-only counties is a separate task.)
      const schoolText = row.schoolName
        ? esc(row.schoolName)
        : (row.same_isd === true ? "same district" : "—");

      let condHtml = "";
      let bodyHtml = "";
      let rowClass = "ai-modal-comp";
      if (row.ai.kind === "read") {
        const rc = row.ai.data;
        condHtml = `<span class="ai-modal-comp-cond">${esc(rc.condition)}</span>`;
        const quoteHtml = rc.condition_quote
          ? `<div class="ai-modal-quote" title="${esc(rc.condition_quote)}">“${esc(truncate(rc.condition_quote, 90))}”</div>`
          : "";
        const flagsHtml = (rc.flags || [])
          .map((f) => `<div class="ai-modal-flag" title="${esc(f.quote)}">⚑ ${esc(f.tag)}: “${esc(truncate(f.quote, 80))}”</div>`)
          .join("");
        bodyHtml = quoteHtml + flagsHtml;
      } else if (row.ai.kind === "excluded") {
        rowClass += " ai-modal-comp-unread";
        condHtml = '<span class="ai-modal-comp-cond ai-modal-comp-cond-unread">not read</span>';
        const why = esc(EXCLUDED_REASON_LABEL[row.ai.data.reason] || row.ai.data.reason || "not read");
        bodyHtml = `<div class="ai-modal-comp-unread-why">${why}</div>`;
      }
      // kind === "none" -- the comp is visible but was never sent to the read
      // (e.g. outside the drawn area). No condition line at all (§2.3): the
      // row is still here, just with no AI layer to show.

      return (
        `<div class="${rowClass}">` +
        '<div class="ai-modal-comp-head">' +
        condHtml +
        `<span class="ai-modal-comp-price">${esc(fmtPrice(comp && comp.price))}</span>` +
        `<span class="ai-modal-comp-addr">${esc((comp && (comp.address || comp.comp_address_key)) || "?")}</span>` +
        badge +
        "</div>" +
        '<div class="ai-modal-comp-facts">' +
        `<span class="ai-modal-fact${neighGold}"><span class="ai-modal-fact-label">Neighborhood</span><span class="ai-modal-fact-value">${neighText}</span></span>` +
        `<span class="ai-modal-fact${schoolGold}"><span class="ai-modal-fact-label">School</span><span class="ai-modal-fact-value">${schoolText}</span></span>` +
        "</div>" +
        bodyHtml +
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

    // §2.0 (Fable) -- lastResponse is now OPTIONAL. The scorecard is driven
    // by ctx().comps (the full visible set), never by data.comps -- a comp
    // read but no longer visible does not appear; a comp visible but never
    // read still appears, facts-only (§2.5). The AI response, when present,
    // is layered on top by comp_address_key and only adds the condition
    // read/exclusion-reason/dropped-tags/raw-JSON blocks that depend on it.
    function openModal() {
      const c = ctx();
      if (!c || !c.isAdmin) return;   // sidebar already gates this; guard anyway since idle/error now expose the button directly
      closeModal();

      const subject = c.subject || null;
      const visibleComps = Array.isArray(c.comps) ? c.comps : [];
      const data = lastResponse;   // may be null (idle) or from a prior run (error / done)

      const readByKey = new Map();
      const excludedByKey = new Map();
      if (data) {
        for (const rc of (Array.isArray(data.comps) ? data.comps : [])) {
          if (rc && rc.comp_address_key) readByKey.set(rc.comp_address_key, rc);
        }
        for (const e of (Array.isArray(data.excluded) ? data.excluded : [])) {
          if (e && e.comp_address_key) excludedByKey.set(e.comp_address_key, e);
        }
      }

      const rows = visibleComps
        .map((comp) => buildScoreRow(comp, subject, readByKey, excludedByKey))
        .sort(compareScoreRows);
      const compsHtml = rows.map(renderScoreRow).join("");

      const excluded = data && Array.isArray(data.excluded) ? data.excluded : [];
      const rejected = data && Array.isArray(data.rejected) ? data.rejected : [];
      const meta = (data && data.meta) || {};
      const mix = (data && data.mix) || {};

      const headerText = data
        ? buildHeaderLine(meta, excluded)
        : (state === "error" ? "Read failed — facts only." : "Not read yet — facts only.");

      const mixHtml = CONDITION_ORDER
        .filter((k) => mix[k] > 0)
        .map((k) => `<span class="ai-modal-mix-item">${mix[k]} ${esc(k)}</span>`)
        .join(" · ");
      // The mix line, dropped-tags block, and raw-JSON copy button all
      // describe a specific AI run -- nothing to show without one, and
      // showing them empty would read as broken rather than "not read yet."
      const mixLineHtml = data
        ? `<div class="ai-modal-mix-line">${mixHtml || "(no conditions read)"}</div>`
        : "";

      let droppedBlockHtml = "";
      let copyBtnHtml = "";
      let footerHtml = "";
      if (data) {
        const cachedNote = meta.cached > 0
          ? '<div class="ai-modal-cached-note">(cached — re-read to see dropped tags)</div>'
          : "";
        const droppedHtml = rejected.length
          ? rejected.map(renderDroppedRow).join("")
          : '<div class="ai-modal-dropped-empty">Nothing dropped this run.</div>';
        droppedBlockHtml =
          '<details class="ai-modal-dropped-block">' +
          `<summary>Dropped tags (${rejected.length})</summary>` +
          cachedNote +
          `<div class="ai-modal-dropped-list">${droppedHtml}</div>` +
          "</details>";
        copyBtnHtml = '<button type="button" class="ai-modal-copy-btn">Copy raw JSON</button>';
        const excludedFooter = excluded.length
          ? excluded
              .map((e) => `${esc(e.address || e.comp_address_key || "?")} (${esc(EXCLUDED_REASON_LABEL[e.reason] || e.reason)})`)
              .join(", ")
          : "none";
        footerHtml =
          '<div class="ai-modal-footer">' +
          `Excluded: ${excludedFooter} · prompt ${esc(meta.prompt_version)} · $${Number(meta.est_cost || 0).toFixed(4)} · ${meta.cached || 0} served from cache` +
          "</div>";
      }

      const overlay = document.createElement("div");
      overlay.className = "ai-modal-overlay";
      overlay.innerHTML =
        '<div class="ai-modal-box" role="dialog" aria-modal="true" aria-label="AI read brief">' +
        '<button type="button" class="ai-modal-close" aria-label="Close">×</button>' +
        `<div class="ai-modal-header">${esc(headerText)}</div>` +
        mixLineHtml +
        `<div class="ai-modal-comps">${compsHtml || '<div class="ai-modal-empty">No comps currently visible.</div>'}</div>` +
        droppedBlockHtml +
        copyBtnHtml +
        footerHtml +
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
