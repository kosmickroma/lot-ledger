// Value Drafts — ghost ARV/NBV drafts beside the stored-value boxes, computed
// entirely client-side from comps the user is already rating (spec
// docs/AI/VALUE_DRAFTS_SPEC_2026-07-13.md). THERE IS NO AI IN THIS FEATURE.
//
// Self-contained IIFE. No imports from map.js — reads exactly the seam
// window.__valueDraftsGetContext() / window.__valueDraftsAcceptDraft() (map.js
// seam, next to the AI card's). Wrapped in try/catch end to end: this file
// must be incapable of breaking comp loading, filtering, rating, or the
// stored-value save path.
//
// ⛔ NEVER writes input.value on the real stored-value inputs except via
// window.__valueDraftsAcceptDraft (the one accept path). The ghost draft is
// its own DOM element, injected beside the box, never inside it.
//
// Deleting this file + value-drafts.css + the 3 index.html lines (script,
// stylesheet, LL_CONFIG key) + the 2 map.js seam functions must leave the app
// byte-identical.
(function () {
  "use strict";

  try {
    if (!(window.LL_CONFIG && window.LL_CONFIG.valueDraftsEnabled)) return;

    const FIELDS = ["arv", "nbv"];
    const MEDIAN_MIN_KEPT = 2;
    // "New build" = built 2008 or later. This is NOT a guess and NOT the scraper's
    // NEW_BUILDS_MIN_YEAR (2015, api/propelio/config.py) — that constant is for an
    // offline crawl and has nothing to do with this procedure.
    //
    // 2008 is the CLIENT'S OWN LINE, read from his team's saved filters: they set
    // the NBV view's yearMin to 2008 on 28 of the 29 areas where they configured it
    // (the 29th is 2020). At 2015 we would silently drop every 2008-2014 build out
    // of the ceiling pool -- which can drop the very comp that IS the NBV.
    //
    // ⚠️ Confirm with him on the call ("is 2008 your line, or does it move by
    // neighbourhood?"). If it moves, this becomes a knob, not a constant.
    const NEW_BUILD_MIN_YEAR = 2008;
    const POLL_MS = 700;
    const DEFAULT_ARV_MULTIPLIER = 0.5;

    let modalEl = null;
    let modalKeydownHandler = null;
    let arvMultiplier = DEFAULT_ARV_MULTIPLIER;

    // Per-field bookkeeping for telemetry + reactive re-render.
    const fieldState = {
      arv: { draft: null, ratings: new Map(), keys: null, savedSnapshot: undefined, hasEverAccepted: false },
      nbv: { draft: null, ratings: new Map(), keys: null, savedSnapshot: undefined, hasEverAccepted: false },
    };
    let seenAreaId = undefined;

    function ctx() {
      try {
        return typeof window.__valueDraftsGetContext === "function"
          ? window.__valueDraftsGetContext()
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

    function fmt(n) {
      if (n === null || n === undefined || !Number.isFinite(Number(n))) return "?";
      const v = Math.round(Number(n));
      if (Math.abs(v) >= 1000) return "$" + Math.round(v / 1000) + "K";
      return "$" + v.toLocaleString("en-US");
    }

    function fmtFull(n) {
      if (n === null || n === undefined || !Number.isFinite(Number(n))) return "?";
      return "$" + Math.round(Number(n)).toLocaleString("en-US");
    }

    function median(nums) {
      const sorted = nums.slice().sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      if (sorted.length % 2 === 1) return { value: sorted[mid], midIdx: [mid] };
      return { value: (sorted[mid - 1] + sorted[mid]) / 2, midIdx: [mid - 1, mid] };
    }

    function isMathEligible(c) {
      return c.price !== null && c.permit_avm !== false;
    }

    function exclusionReason(c) {
      if (c.price === null) return "no sale price";
      if (c.permit_avm === false) return "no AVM permission";
      return null;
    }

    function summarizeExclusions(excluded) {
      if (!excluded.length) return "";
      const counts = { "no sale price": 0, "no AVM permission": 0 };
      excluded.forEach((c) => {
        const r = exclusionReason(c);
        if (r) counts[r] = (counts[r] || 0) + 1;
      });
      const parts = Object.keys(counts).filter((k) => counts[k] > 0).map((k) => `${counts[k]} ${k}`);
      return `${excluded.length} kept comp${excluded.length === 1 ? "" : "s"} not in the math: ${parts.join(", ")}`;
    }

    // --- Draft math (spec §1, §3) ------------------------------------------

    function computeArvDraft(allComps) {
      if (allComps.length === 0) {
        return { value: null, label: "Keep comps to draft a value", basis: "none", mathComps: [], excludedComps: [], keptCount: 0 };
      }
      const kept = allComps.filter((c) => c.user_rating === "good");
      if (kept.length === 1) {
        return { value: null, label: "1 kept — keep another to draft ARV", basis: "kept", mathComps: [], excludedComps: [], keptCount: 1 };
      }
      const basisComps = kept.length >= MEDIAN_MIN_KEPT ? kept : allComps;
      const basis = kept.length >= MEDIAN_MIN_KEPT ? "kept" : "pool";
      const mathComps = basisComps.filter(isMathEligible);
      const excludedComps = basisComps.filter((c) => !isMathEligible(c));
      if (mathComps.length === 0) {
        return {
          value: null,
          label: `${basisComps.length} ${basis === "kept" ? "kept" : "visible"} comp${basisComps.length === 1 ? "" : "s"}, all excluded from the math`,
          basis, mathComps: [], excludedComps, keptCount: kept.length,
        };
      }
      const { value, midIdx } = median(mathComps.map((c) => c.price));
      const label = basis === "kept"
        ? `median of your ${basisComps.length} kept comp${basisComps.length === 1 ? "" : "s"}: ${fmt(value)}`
        : `median of ${basisComps.length} visible comps: ${fmt(value)}`;
      const sorted = mathComps.slice().sort((a, b) => a.price - b.price);
      const highlightKeys = midIdx.map((i) => sorted[i].comp_address_key);
      return { value, label, basis, mathComps: sorted, excludedComps, keptCount: kept.length, highlightKeys };
    }

    function computeNbvDraft(allComps) {
      if (allComps.length === 0) {
        return { value: null, label: "Keep comps to draft a value", basis: "none", mathComps: [], excludedComps: [], keptCount: 0 };
      }
      const kept = allComps.filter((c) => c.user_rating === "good");
      if (kept.length >= 1) {
        const mathComps = kept.filter(isMathEligible);
        const excludedComps = kept.filter((c) => !isMathEligible(c));
        if (mathComps.length === 0) {
          return {
            value: null,
            label: `${kept.length} kept comp${kept.length === 1 ? "" : "s"}, all excluded from the math`,
            basis: "kept", mathComps: [], excludedComps, keptCount: kept.length,
          };
        }
        const ceiling = mathComps.slice().sort((a, b) => b.price - a.price)[0];
        return {
          value: ceiling.price,
          label: "your kept ceiling comp: " + fmt(ceiling.price),
          basis: "kept", mathComps: [ceiling], excludedComps, keptCount: kept.length,
          highlightKeys: [ceiling.comp_address_key],
        };
      }
      const pool = allComps.filter((c) => Number.isFinite(c.year_built) && c.year_built >= NEW_BUILD_MIN_YEAR);
      if (pool.length === 0) {
        return { value: null, label: "no new-build sale visible — keep one to draft NBV", basis: "pool", mathComps: [], excludedComps: [], keptCount: 0 };
      }
      const mathComps = pool.filter(isMathEligible);
      const excludedComps = pool.filter((c) => !isMathEligible(c));
      if (mathComps.length === 0) {
        return {
          value: null,
          label: `${pool.length} new-build sale${pool.length === 1 ? "" : "s"} visible, all excluded from the math`,
          basis: "pool", mathComps: [], excludedComps, keptCount: 0,
        };
      }
      const ceiling = mathComps.slice().sort((a, b) => b.price - a.price)[0];
      return {
        value: ceiling.price,
        label: "top new-build sale visible: " + fmt(ceiling.price),
        basis: "pool", mathComps: [ceiling], excludedComps, keptCount: 0,
        highlightKeys: [ceiling.comp_address_key],
      };
    }

    // --- Telemetry (spec §6) -----------------------------------------------

    function sendTelemetry(payload) {
      try {
        const c = ctx();
        if (!c || !c.isAdmin || !c.areaId) return;
        void fetch("/api/value-drafts/telemetry", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(c.headers || {}) },
          credentials: "same-origin",
          body: JSON.stringify({ area_id: c.areaId, ...payload }),
        }).catch(() => { /* telemetry is best-effort; never surfaces to the user */ });
      } catch { /* never let telemetry break anything */ }
    }

    // --- Ghost draft chip DOM ----------------------------------------------

    function ensureChip(fieldKey) {
      const row = document.querySelector(`.sv-row[data-field-key="${fieldKey}"]`);
      if (!row) return null;
      let chip = row.querySelector(".vd-draft-chip");
      if (!chip) {
        chip = document.createElement("div");
        chip.className = "vd-draft-chip";
        const wrap = row.querySelector(".sv-input-wrap");
        if (wrap && wrap.parentElement) {
          wrap.insertAdjacentElement("afterend", chip);
        } else {
          row.appendChild(chip);
        }
      }
      return chip;
    }

    function renderChip(fieldKey, draft, savedValue) {
      const chip = ensureChip(fieldKey);
      if (!chip) return;

      if (draft.value === null) {
        chip.innerHTML = `<span class="vd-draft-label vd-draft-empty">${esc(draft.label)}</span>`;
        return;
      }

      const hasSaved = savedValue !== null && savedValue !== undefined;
      let valueHtml;
      if (hasSaved && savedValue !== draft.value) {
        const delta = savedValue - draft.value;
        const deltaText = delta === 0 ? "(saved = draft)" : `(${delta > 0 ? "+" : "−"}${fmt(Math.abs(delta))})`;
        valueHtml = `draft ${fmtFull(draft.value)} · saved ${fmtFull(savedValue)} ${deltaText}`;
      } else if (hasSaved) {
        valueHtml = `draft ${fmtFull(draft.value)} · saved ${fmtFull(savedValue)}`;
      } else {
        valueHtml = esc(draft.label);
      }

      chip.innerHTML =
        `<span class="vd-draft-label">${hasSaved ? esc(valueHtml) : esc(draft.label)}</span>` +
        '<span class="vd-draft-actions">' +
        '<button type="button" class="vd-draft-explain" title="How did it get this?" aria-label="How did it get this?">?</button>' +
        '<button type="button" class="vd-draft-accept">Accept</button>' +
        "</span>";

      const explainBtn = chip.querySelector(".vd-draft-explain");
      if (explainBtn) explainBtn.addEventListener("click", () => openDerivationModal(fieldKey));
      const acceptBtn = chip.querySelector(".vd-draft-accept");
      if (acceptBtn) acceptBtn.addEventListener("click", () => acceptDraft(fieldKey));
    }

    function acceptDraft(fieldKey) {
      const draft = fieldState[fieldKey].draft;
      if (!draft || draft.value === null) return;
      const ok = typeof window.__valueDraftsAcceptDraft === "function"
        && window.__valueDraftsAcceptDraft(fieldKey, draft.value);
      if (!ok) return;

      fieldState[fieldKey].hasEverAccepted = true;
      fieldState[fieldKey].savedSnapshot = draft.value;

      const c = ctx();
      const view = c ? c[fieldKey] : null;
      const keptKeys = view ? view.comps.filter((cp) => cp.user_rating === "good").map((cp) => cp.comp_address_key) : [];
      sendTelemetry({
        event_type: "commit",
        field: fieldKey,
        draft_value: draft.value,
        final_value: draft.value,
        outcome: "accepted_verbatim",
        kept_comp_keys: keptKeys,
      });

      closeModal();
      tick(true);
    }

    // --- Direct-typing commit detection (observation only — never writes) --

    function wireInputObservers() {
      FIELDS.forEach((fieldKey) => {
        const input = document.getElementById(`sv-input-${fieldKey}`);
        if (!input) return;
        input.addEventListener("blur", () => {
          // Runs AFTER map.js's own blur handler (attached earlier, at page
          // load) since listeners fire in attachment order — so ctx() already
          // reflects the post-edit saved value here.
          const c = ctx();
          if (!c) return;
          const newVal = c.storedValues ? c.storedValues[fieldKey] : null;
          const fs = fieldState[fieldKey];
          if (fs.savedSnapshot === undefined) { fs.savedSnapshot = newVal; return; }
          if (newVal === fs.savedSnapshot) return;   // blur without an edit

          const draft = fs.draft;
          let outcome;
          if (draft && draft.value !== null && newVal === draft.value) {
            outcome = "accepted_verbatim";
          } else if (fs.hasEverAccepted) {
            outcome = "edited";
          } else if (draft && draft.value !== null) {
            outcome = "typed_while_draft_visible";
          } else {
            fs.savedSnapshot = newVal;
            return;   // no draft was ever in play -- not an anchoring-relevant event
          }
          const view = c[fieldKey];
          const keptKeys = view ? view.comps.filter((cp) => cp.user_rating === "good").map((cp) => cp.comp_address_key) : [];
          sendTelemetry({
            event_type: "commit",
            field: fieldKey,
            draft_value: draft ? draft.value : null,
            final_value: newVal,
            outcome,
            kept_comp_keys: keptKeys,
          });
          fs.savedSnapshot = newVal;
        });
      });
    }

    // --- Derivation panel (spec §4) ----------------------------------------

    function closeModal() {
      if (modalEl) { modalEl.remove(); modalEl = null; }
      if (modalKeydownHandler) { document.removeEventListener("keydown", modalKeydownHandler); modalKeydownHandler = null; }
    }

    function compRow(c, highlightKeys) {
      const hl = (highlightKeys || []).includes(c.comp_address_key) ? " vd-modal-comp-row-hl" : "";
      return `<div class="vd-modal-comp-row${hl}"><span>${esc(fmtFull(c.price))}</span><span>${esc(c.address || c.comp_address_key)}</span></div>`;
    }

    function openDerivationModal(fieldKey) {
      const draft = fieldState[fieldKey].draft;
      if (!draft) return;
      closeModal();

      const c = ctx();
      const savedValue = c && c.storedValues ? c.storedValues[fieldKey] : null;

      const compsHtml = draft.mathComps.length
        ? draft.mathComps.map((cm) => compRow(cm, draft.highlightKeys)).join("")
        : '<div class="vd-modal-empty">No comps in the math.</div>';

      const exclusionsHtml = draft.excludedComps.length
        ? draft.excludedComps.map((c2) => `<div class="vd-modal-excl-row">${esc(c2.address || c2.comp_address_key)} — ${esc(exclusionReason(c2) || "excluded")}</div>`).join("")
        : '<div class="vd-modal-empty">None excluded.</div>';

      let arithmetic;
      if (draft.value === null) {
        arithmetic = esc(draft.label);
      } else if (fieldKey === "arv") {
        const prices = draft.mathComps.map((cm) => fmtFull(cm.price)).join(", ");
        arithmetic = `median(${prices}) = ${fmtFull(draft.value)}`;
      } else {
        arithmetic = `highest kept/visible new-build price = ${fmtFull(draft.value)}`;
      }

      // §3.4 — ARV ≈ NBV × multiplier, framed as a question, never as truth.
      // There is no 0.5 anywhere else in the codebase; this is shown ONLY on
      // the ARV panel, only when an NBV value (draft or saved) exists to
      // multiply against.
      let questionHtml = "";
      if (fieldKey === "arv") {
        const nbvValue = (c && c.storedValues && c.storedValues.nbv !== null && c.storedValues.nbv !== undefined)
          ? c.storedValues.nbv
          : (fieldState.nbv.draft && fieldState.nbv.draft.value !== null ? fieldState.nbv.draft.value : null);
        if (nbvValue !== null) {
          const suggested = Math.round(nbvValue * arvMultiplier);
          questionHtml = `
            <div class="vd-modal-section vd-modal-question">
              <div class="vd-modal-section-title">Half of your new-build value — is that your rule?</div>
              <div class="vd-modal-question-row">
                <span>ARV ≈ ${esc(fmtFull(suggested))}</span>
                <span class="vd-modal-question-mult">× <input type="number" step="0.01" min="0" max="2" class="vd-modal-mult-input" value="${esc(arvMultiplier)}"></span>
                <button type="button" class="vd-modal-question-accept">Use this instead</button>
              </div>
              <div class="vd-modal-question-note">There is no ${esc(arvMultiplier)} rule anywhere in the app — this is a pattern in your team's own data. Your answer is the point.</div>
            </div>`;
        }
      }

      const overlay = document.createElement("div");
      overlay.className = "vd-modal-overlay";
      overlay.setAttribute("data-field", fieldKey);
      overlay.innerHTML =
        '<div class="vd-modal-box" role="dialog" aria-modal="true" aria-label="How did it get this?">' +
        '<button type="button" class="vd-modal-close" aria-label="Close">×</button>' +
        `<div class="vd-modal-header">${esc((fieldKey === "arv" ? "ARV" : "NBV") + " draft — " + draft.label)}</div>` +
        (savedValue !== null && savedValue !== undefined
          ? `<div class="vd-modal-saved-note">Saved value: ${esc(fmtFull(savedValue))}</div>` : "") +
        '<div class="vd-modal-section"><div class="vd-modal-section-title">Comps in the math</div>' +
        `<div class="vd-modal-comps">${compsHtml}</div></div>` +
        '<div class="vd-modal-section"><div class="vd-modal-section-title">Arithmetic</div>' +
        `<div class="vd-modal-arithmetic">${esc(arithmetic)}</div></div>` +
        '<div class="vd-modal-section"><div class="vd-modal-section-title">Excluded from the math</div>' +
        `<div class="vd-modal-exclusions">${exclusionsHtml}</div></div>` +
        questionHtml +
        "</div>";

      document.body.appendChild(overlay);
      modalEl = overlay;

      overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
      const closeBtn = overlay.querySelector(".vd-modal-close");
      if (closeBtn) closeBtn.addEventListener("click", () => closeModal());

      const multInput = overlay.querySelector(".vd-modal-mult-input");
      const qAcceptBtn = overlay.querySelector(".vd-modal-question-accept");
      if (multInput) {
        multInput.addEventListener("input", () => {
          const v = Number(multInput.value);
          if (Number.isFinite(v) && v >= 0) {
            arvMultiplier = v;
            openDerivationModal(fieldKey);   // re-render with the new multiplier
          }
        });
      }
      if (qAcceptBtn) {
        qAcceptBtn.addEventListener("click", () => {
          const nbvValue = (c && c.storedValues && c.storedValues.nbv !== null && c.storedValues.nbv !== undefined)
            ? c.storedValues.nbv
            : (fieldState.nbv.draft && fieldState.nbv.draft.value !== null ? fieldState.nbv.draft.value : null);
          if (nbvValue === null) return;
          const suggested = Math.round(nbvValue * arvMultiplier);
          const ok = typeof window.__valueDraftsAcceptDraft === "function"
            && window.__valueDraftsAcceptDraft("arv", suggested);
          if (ok) {
            fieldState.arv.hasEverAccepted = true;
            fieldState.arv.savedSnapshot = suggested;
            sendTelemetry({
              event_type: "commit", field: "arv", draft_value: suggested, final_value: suggested,
              outcome: "accepted_verbatim", kept_comp_keys: [],
            });
            closeModal();
            tick(true);
          }
        });
      }

      modalKeydownHandler = (e) => { if (e.key === "Escape") closeModal(); };
      document.addEventListener("keydown", modalKeydownHandler);
    }

    // --- Reactive loop ------------------------------------------------------
    // map.js has no event hook for "rating changed" / "filters changed" in
    // this build — the seam is deliberately just the two functions above —
    // so a light poll is how this fully decoupled file notices those
    // transitions and recomputes (spec: "recomputes on every filter change
    // and every rating").

    function ratingsMap(comps) {
      const m = new Map();
      comps.forEach((c) => { if (c.comp_address_key) m.set(c.comp_address_key, c.user_rating || null); });
      return m;
    }

    function keysOf(comps) {
      return comps.map((c) => c.comp_address_key).filter(Boolean).sort().join(",");
    }

    function diffRatings(fieldKey, view, oldMap) {
      const newMap = ratingsMap(view.comps);
      let changed = false;
      newMap.forEach((rating, key) => {
        const prev = oldMap.has(key) ? oldMap.get(key) : null;
        if (prev !== rating) {
          changed = true;
          const draft = fieldState[fieldKey].draft;
          sendTelemetry({
            event_type: "rating",
            view: fieldKey,
            comp_key: key,
            verdict: rating || "unrated",
            draft_state: draft ? draft.basis : "none",
            draft_value: draft ? draft.value : null,
          });
        }
      });
      return { changed, newMap };
    }

    function tick(force) {
      const c = ctx();
      if (!c || !c.isAdmin || !c.areaId) {
        FIELDS.forEach((f) => { const chip = document.querySelector(`.sv-row[data-field-key="${f}"] .vd-draft-chip`); if (chip) chip.remove(); });
        return;
      }

      if (c.areaId !== seenAreaId) {
        seenAreaId = c.areaId;
        FIELDS.forEach((f) => {
          fieldState[f].ratings = new Map();
          fieldState[f].keys = null;
          fieldState[f].savedSnapshot = undefined;
          fieldState[f].hasEverAccepted = false;
        });
        closeModal();
        force = true;
      }

      FIELDS.forEach((fieldKey) => {
        const view = c[fieldKey];
        const fs = fieldState[fieldKey];
        const newKeys = keysOf(view.comps);
        const { changed: ratingChanged, newMap } = diffRatings(fieldKey, view, fs.ratings);
        const setChanged = fs.keys !== null && fs.keys !== newKeys;
        const prevDraft = fs.draft;

        const draft = fieldKey === "arv" ? computeArvDraft(view.comps) : computeNbvDraft(view.comps);
        fs.draft = draft;
        fs.ratings = newMap;
        fs.keys = newKeys;

        const savedValue = c.storedValues ? c.storedValues[fieldKey] : null;
        if (fs.savedSnapshot === undefined) fs.savedSnapshot = savedValue;

        if (!prevDraft || prevDraft.value !== draft.value || force) {
          const cause = ratingChanged ? "rating" : (setChanged ? "filter" : "other");
          if (prevDraft && prevDraft.value !== draft.value) {
            sendTelemetry({
              event_type: "draft_change", field: fieldKey,
              from_value: prevDraft.value, to_value: draft.value, cause,
            });
          }
          renderChip(fieldKey, draft, savedValue);
          if (modalEl && modalEl.getAttribute("data-field") === fieldKey) {
            openDerivationModal(fieldKey);
          }
        }
      });
    }

    wireInputObservers();
    tick(true);
    setInterval(() => tick(false), POLL_MS);
  } catch (err) {
    try { console.error("[value-drafts] init failed (non-fatal):", err); } catch { /* ignore */ }
  }
})();
