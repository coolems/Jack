/**
 * license-badge.js - Persistent READ-ONLY licence badge.
 * LICENSING PLAN v16 (2026-10-15; badge states refined 2026-10-12).
 *
 * The UI NEVER asks, collects or saves anything about licensing — that happens in
 * ZZZ_initial_init.bat at setup time (utils/init_setup/licensing.py). This file only
 * DISPLAYS the licence information:
  *       private (unconfirmed / no e-mail)   -> "PRIVATE USE · licence not acquired"
 *       private (e-mail confirmed on web side, free commercial use as individual)
 *                                           -> "PRIVATE LICENCE · licensed <e-mail> for commercial use"
 *       pending  -> "COMMERCIAL USE · pending licence confirmation"
 *       licensed -> "LICENSED — <Company> [· valid to YYYY-MM-DD]"
 * All data comes from GET /api/licence/status ONLY (the server reads/matches the
 * available licence on coolems.com and passes it down). This file never talks to
 * coolems.com directly, never sees licences.json, and has no modal, form or button.
 */

(function () {
    'use strict';

    const CONTACT_EMAIL = 'coolems.kg@gmail.com';
    let _badgeEl = null;

    // ===== helpers =============================================================

    async function licenceApi(path) {
        const res = await fetch(path);
        try { return await res.json(); } catch (e) { return {}; }
    }

    // ===== BADGE (display only — no interaction, no gate) ======================

    function buildBadge() {
        if (_badgeEl || document.getElementById('coolemsLicenceBadge')) return;
        const el = document.createElement('div');
        el.id = 'coolemsLicenceBadge';
        el.className = 'licence-badge licence-badge--loading';
        el.title = 'Loading licence status…';
        // Read-only: no click handler, not a button.
                        // Bottom-left corner of the big input frame (.input-container),
                // which holds the centered prompt frame (.input-wrapper) untouched.
                const host = document.querySelector('.input-container') || document.body;
                host.appendChild(el);
        _badgeEl = el;
    }

    // Two-line badge: line 1 = status (PRIVATE USE / COMMERCIAL USE ...),
        // line 2 = who it is assigned to (company) or the pending/not-set note.
        function setLines(el, l1, l2) {
            el.textContent = '';
            const a = document.createElement('div');
            a.className = 'licence-badge__line';
            a.textContent = l1 || '';
            const b = document.createElement('div');
            b.className = 'licence-badge__line licence-badge__line--sub';
            b.textContent = l2 || '';
            el.appendChild(a);
            el.appendChild(b);
        }

        function renderBadge(st) {
            buildBadge();
            const el = _badgeEl;
            el.classList.remove('licence-badge--loading');
            if (st && st.badge === 'licensed') {
                el.className = 'licence-badge licence-badge--licensed';
                setLines(el, st.line1 || 'COMMERCIAL USE — LICENSED',
                         st.line2 || (st.company ? ('assigned to ' + st.company) : 'LICENSED'));
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private-licensed') {
                // Free private licence CONFIRMED on the web side — good form.
                el.className = 'licence-badge licence-badge--licensed';
                setLines(el, st.line1 || 'PRIVATE LICENCE',
                         st.line2 || 'licensed for commercial use');
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private') {
                // Legacy SERVER payload: private without confirmation state — unconfirmed.
                el.className = 'licence-badge licence-badge--pending';
                setLines(el, st.line1 || 'PRIVATE USE',
                         st.line2 || 'licence not acquired');
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private-pending') {
                // Private install, e-mail missing or confirmation still pending.
                el.className = 'licence-badge licence-badge--pending';
                setLines(el, st.line1 || 'PRIVATE USE',
                         st.line2 || 'licence not acquired');
                el.title = st.tooltip || '';
            } else if (st && st.choice === null) {
                // Never registered — the question belongs to ZZZ_initial_init.bat, not here.
                el.className = 'licence-badge licence-badge--pending';
                setLines(el, st.line1 || 'LICENCE NOT SET',
                         st.line2 || 'run ZZZ_initial_init.bat');
                el.title = ('Licensing is configured during setup (ZZZ_initial_init.bat). ' +
                            'Company use requires a licence from Coolems KG: ' + CONTACT_EMAIL);
            } else {
                // pending — registered as company, server confirmation not in yet.
                const offlineNote = (st && st.offline) ? '\nLicence check unavailable right now — will retry.' : '';
                el.className = 'licence-badge licence-badge--pending';
                setLines(el, st.line1 || 'COMMERCIAL USE',
                         st.line2 || ((st.company ? (st.company + ' · ') : '') + 'pending licence confirmation'));
                el.title = ((st && st.tooltip) || ('Commercial use requires a licence from Coolems KG. Contact: ' + CONTACT_EMAIL)) + offlineNote;
            }
        }

    async function refreshBadge() {
        try {
            const st = await licenceApi('/api/licence/status');
            renderBadge(st);
            return st;
        } catch (e) {
            console.warn('[Licence] status fetch failed:', e);
            return null;
        }
    }

    // ===== BOOT =================================================================

    async function initLicenceBadge() {
        buildBadge();
        const st = await refreshBadge();
        if (!st || !st.badge) {
            // Backend not reachable yet (booting) — retry a few times, then stay quiet.
            for (let i = 0; i < 5 && !(await refreshBadge()); i++) await new Promise(r => setTimeout(r, 2000));
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLicenceBadge);
    } else {
        initLicenceBadge();
    }

    // Expose for other UI modules / debugging. Display only — no gate, no openGate.
    window.LicenceBadge = { refresh: refreshBadge };
})();
