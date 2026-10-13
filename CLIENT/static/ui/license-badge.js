/**
 * license-badge.js - Persistent READ-ONLY license badge.
 * LICENSING PLAN v16 (2026-10-15; badge states refined 2026-10-12).
 *
 * The UI NEVER asks, collects or saves anything about licensing — that happens in
 * ZZZ_initial_init.bat at setup time (utils/init_setup/licensing.py). This file only
 * DISPLAYS the license information:
  *       private (unconfirmed / no e-mail)   -> "PRIVATE USE · license not acquired"
 *       private (e-mail confirmed on web side, free commercial use as individual)
 *                                           -> "PRIVATE LICENSE · licensed <e-mail> for commercial use"
 *       pending  -> "COMMERCIAL USE · pending license confirmation"
 *       licensed -> "LICENSED — <Company> [· valid to YYYY-MM-DD]"
 * All data comes from GET /api/license/status ONLY (the server reads/matches the
 * available license on coolems.com and passes it down). This file never talks to
 * coolems.com directly, never sees licenses.json, and has no modal, form or button.
 */

(function () {
    'use strict';

    const CONTACT_EMAIL = 'coolems.kg@gmail.com';
    let _badgeEl = null;

    // ===== helpers =============================================================

    async function licenseApi(path) {
        const res = await fetch(path);
        try { return await res.json(); } catch (e) { return {}; }
    }

    // ===== BADGE (display only — no interaction, no gate) ======================

    function buildBadge() {
        if (_badgeEl || document.getElementById('coolemsLicenseBadge')) return;
        const el = document.createElement('div');
        el.id = 'coolemsLicenseBadge';
        el.className = 'license-badge license-badge--loading';
        el.title = 'Loading license status…';
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
            a.className = 'license-badge__line';
            a.textContent = l1 || '';
            const b = document.createElement('div');
            b.className = 'license-badge__line license-badge__line--sub';
            b.textContent = l2 || '';
            el.appendChild(a);
            el.appendChild(b);
        }

        function renderBadge(st) {
            buildBadge();
            const el = _badgeEl;
            el.classList.remove('license-badge--loading');
            if (st && st.badge === 'licensed') {
                el.className = 'license-badge license-badge--licensed';
                setLines(el, st.line1 || 'COMMERCIAL USE — LICENSED',
                         st.line2 || (st.company ? ('assigned to ' + st.company) : 'LICENSED'));
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private-licensed') {
                // Free private license CONFIRMED on the web side — good form.
                el.className = 'license-badge license-badge--licensed';
                setLines(el, st.line1 || 'PRIVATE LICENSE',
                         st.line2 || 'licensed for commercial use');
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private') {
                // Legacy SERVER payload: private without confirmation state — unconfirmed.
                el.className = 'license-badge license-badge--pending';
                setLines(el, st.line1 || 'PRIVATE USE',
                         st.line2 || 'license not acquired');
                el.title = st.tooltip || '';
            } else if (st && st.badge === 'private-pending') {
                // Private install, e-mail missing or confirmation still pending.
                el.className = 'license-badge license-badge--pending';
                setLines(el, st.line1 || 'PRIVATE USE',
                         st.line2 || 'license not acquired');
                el.title = st.tooltip || '';
            } else if (st && st.choice === null) {
                // Never registered — the question belongs to ZZZ_initial_init.bat, not here.
                el.className = 'license-badge license-badge--pending';
                setLines(el, st.line1 || 'LICENSE NOT SET',
                         st.line2 || 'run ZZZ_initial_init.bat');
                el.title = ('Licensing is configured during setup (ZZZ_initial_init.bat). ' +
                            'Company use requires a license from Coolems KG: ' + CONTACT_EMAIL);
            } else {
                // pending — registered as company, server confirmation not in yet.
                const offlineNote = (st && st.offline) ? '\nLicense check unavailable right now — will retry.' : '';
                el.className = 'license-badge license-badge--pending';
                setLines(el, st.line1 || 'COMMERCIAL USE',
                         st.line2 || ((st.company ? (st.company + ' · ') : '') + 'pending license confirmation'));
                el.title = ((st && st.tooltip) || ('Commercial use requires a license from Coolems KG. Contact: ' + CONTACT_EMAIL)) + offlineNote;
            }
        }

    async function refreshBadge() {
        try {
            const st = await licenseApi('/api/license/status');
            renderBadge(st);
            return st;
        } catch (e) {
            console.warn('[License] status fetch failed:', e);
            return null;
        }
    }

    // ===== BOOT =================================================================

    async function initLicenseBadge() {
        buildBadge();
        const st = await refreshBadge();
        if (!st || !st.badge) {
            // Backend not reachable yet (booting) — retry a few times, then stay quiet.
            for (let i = 0; i < 5 && !(await refreshBadge()); i++) await new Promise(r => setTimeout(r, 2000));
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLicenseBadge);
    } else {
        initLicenseBadge();
    }

    // Expose for other UI modules / debugging. Display only — no gate, no openGate.
    window.LicenseBadge = { refresh: refreshBadge };
})();
