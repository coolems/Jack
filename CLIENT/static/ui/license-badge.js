/**
 * license-badge.js - Persistent READ-ONLY license badge.
 *
 * Display only: no ask, no form, no gate, and no direct call to coolems.com.
 * Licensing is collected at setup time (utils/init_setup/licensing.py); this badge
 * renders whatever GET /api/license/status returns:
 *       private-licensed  -> "PRIVATE LICENSE · licensed <e-mail> for commercial use"
 *       private-pending   -> "PRIVATE USE · license not acquired"
 *       licensed          -> "COMMERCIAL USE — LICENSED · assigned to <Company>"
 *       pending           -> "COMMERCIAL USE · pending license confirmation"
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
        // Bottom-left info strip of the big input frame (.input-container): it sits
        // UNDER the centered prompt frame, BELOW the clock line.
        const host = document.getElementById('inputInfo')
            || document.querySelector('.input-container') || document.body;
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
                // SERVER payload without a confirmation state — treated as unconfirmed.
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
