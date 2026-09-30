/**
     * setup-progress.js - Live "Setup / Downloads" section for self-unpacking tools.
     * Uses COOLEMS shared state from app.js (escapeHtml, scrollToBottom available there).
     *
     * 2026-09-21: when a tool like generate_image runs on a FRESH machine it first has to
     * create its venv, pip-install multi-GB wheels and download model weights. That work is
     * tracked in <runtime>/.setup_progress.json by tools/tool_bootstrap.py (venv + pip) and
     * the tool itself (model). This module renders that state as a chat section - exactly
     * where results appear today - with per-pip-package status (and live percent/speed/ETA
     * while a wheel downloads) and, for the model download, EXACTLY TWO LIVE LINES:
     *   1. the CURRENT download  (the ACTIVE file - server-picked most recently updated
 *      unfinished row with its own live bytes/speed/ETA; aggregate fallback)
     *   2. the TOTAL             (files done/total + X / Y GB with speed + ETA + bar)
     * Both lines update continuously until every file is downloaded - there is no "paused"
     * state: the server-side progress events keep flowing through the download AND the
     * post-download finalize phase, so the card only ever shows live numbers or a final
     * completion line.
     *
     * HONEST COMPLETION (v3, 2026-09-21 field fix): the card says "complete" ONLY when
     * every stage that exists in the state file has reached a terminal state (done/error).
     * While pip is still installing packages the header stays "in progress…" and each row
     * shows its real status; after venv+pip finish, a short grace window keeps polling so a
     * model download that starts right afterwards is picked up live instead of being missed.
     * Stale state from an interrupted earlier run cannot fake completion: every setup run on
     * the server side starts a new generation and wipes its own stages first.
     *
     * Data path: GET /api/setup/status?tool=<name> (CLIENT/app/routers/downloads.py).
     * The api-key.js fetch override injects X-API-Key + X-User-Email automatically, so no
     * auth plumbing is needed here - same as every other /api/* call in the UI.
     *
     * SHOW ONLY WHEN NEEDED (v16, 2026-09-24): the server writes a stage row only for work it ACTUALLY performed this run. On a fully-cached machine nothing is written - so the card stays INVISIBLE: it is attached to the chat only once fresh setup data appears, and stale 'done' leftovers from an earlier first run are filtered out by age (STALE_DONE_MS). First-run UX (live venv/pip/model progress) is unchanged.
     * Lifecycle: SetupProgressUI.start(toolName) renders one card and polls while any stage
     * is active; it finalizes itself when everything reached a terminal state (or removes
     * the empty card when no setup happened this turn). Multiple cards are allowed (one per
     * tool_start frame); re-starting for the same tool just resumes polling on the existing
     * card.
     */

    (function () {
        'use strict';

        const POLL_MS = 1500;          // poll interval while a stage is running
        const MAX_POLLS_NO_STATE = 40; // ~60 s of empty responses before giving up quietly
        const MODEL_GRACE_POLLS = 20;  // ~30 s after venv+pip finish: wait for the model stage

        /** @type {Map<string, Object>} toolName -> live card controller */
        const activeCards = new Map();

        function fmtBytes(n) {
            if (!n || n <= 0) return '—';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let i = 0, v = Number(n);
            while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
            return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + units[i];
        }

        function fmtEta(sec) {
            if (sec == null || !isFinite(sec)) return '';
            sec = Math.max(0, Math.round(Number(sec)));
            if (sec < 60) return sec + 's';
            const m = Math.floor(sec / 60), s = sec % 60;
            if (m < 60) return m + 'm' + (s ? ' ' + s + 's' : '');
            return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
        }

        function fmtSpeed(bps) {
            if (!bps || bps <= 0) return '';
            const mb = bps / 1024 / 1024;
            return (mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)) + ' MB/s';
        }

        /** Status pill: running spinner / done check / error cross. */
        function statusIcon(status) {
            if (status === 'done') return '<span class="sp-icon sp-done">✓</span>';
            if (status === 'error') return '<span class="sp-icon sp-error">✕</span>';
            if (status === 'running' || status === 'downloading' || status === 'installing') {
                return '<span class="sp-icon sp-run"></span>';
            }
            return '<span class="sp-icon sp-idle">·</span>';
        }

        function isActive(s) { return !!(s && s.status && (s.status === 'running' || s.status === 'pending')); }

        // (2026-09-24) A stage row is shown only when it belongs to THIS run. The server writes a
        // row only for work it actually performed, so on a fully-cached machine the file either has
        // no rows or holds stale 'done' leftovers from an earlier first run - those are hidden by
        // age: a terminal row counts as this-turn's only if the state file was updated within this
        // window (live running/pending rows always count).
        const STALE_DONE_MS = 90 * 1000;

        function _rowIsFresh(stage, updatedAtMs) {
            if (!stage || !stage.status) return false;
            if (stage.status === 'running' || stage.status === 'pending') return true; // live work - always shown
            const age = updatedAtMs ? Math.max(0, Date.now() - Number(updatedAtMs)) : Infinity;
            return age <= STALE_DONE_MS;
        }

        // The state file's updated_at is epoch SECONDS (time.time()) - convert to ms.
        function _updatedMs(data) {
            const s = data && Number(data.updated_at);
            return (s > 0) ? s * 1000 : 0;
        }

        function _freshStages(stages, data) {
            const updatedAtMs = _updatedMs(data);
            const out = {};
            Object.keys(stages || {}).forEach(k => {
                if (_rowIsFresh((stages || {})[k], updatedAtMs)) out[k] = stages[k];
            });
            return out;
        }

        function _dropRow(container, name) {
            const old = container.querySelector('[data-stage="' + name + '"]');
            if (old && old.parentNode) old.remove();
        }

        /** One pip package row: status icon + name (+ size) and, while downloading, a live
         *  percent/speed/ETA readout with a small progress bar. */
        function pkgRow(p) {
            const st = p.status || 'pending';
            let extra = '';
            if (st === 'downloading') {
                const bits = [];
                if (p.speed_bps > 0) bits.push(fmtSpeed(p.speed_bps));
                if (p.eta_sec != null && isFinite(p.eta_sec)) bits.push('ETA ' + fmtEta(p.eta_sec));
                if (typeof p.percent === 'number' && p.size_bytes > 0) {
                    const pct = Math.min(100, Math.max(0, p.percent));
                    extra = '<span class="sp-pkg-prog">' + pct.toFixed(0) + '%' +
                            (bits.length ? ' <b>' + bits.join(' · ') + '</b>' : '') + '</span>' +
                            '<div class="sp-bar sp-bar-sm"><i style="width:' + pct.toFixed(1) + '%"></i></div>';
                } else {
                    extra = '<span class="sp-pkg-prog">' + (bits.length ? bits.join(' · ') : 'downloading…') + '</span>';
                }
            }
            const size = p.size_bytes ? ' <span class="sp-dim">(' + fmtBytes(p.size_bytes) + ')</span>' : '';
            return '<div class="sp-pkg sp-pkg-' + st + '">' + statusIcon(st === 'downloading' ? 'running' : st) +
                   ' ' + escapeHtml(p.name || '') + size + extra + '</div>';
        }

        /** Model stage body: EXACTLY TWO live lines - the current download and the total. */
        function modelBody(stage) {
            const done = stage.status === 'done';
            const err = stage.status === 'error';
            let html = '';

            // ---- line 1: CURRENT DOWNLOAD - only the file downloading right now (v15) -----
            // current_file comes from the server = the ACTIVE row with a real per-file bar event;
            // pass 2 keeps naming the most recently updated REAL file between files / at finalize,
            // so this line always shows a real file name + its own live numbers. The only non-file
            // case is the first seconds before any bytes land: plain 'Downloading model files…'
            // with live speed and NO bar (overall progress lives on the Total line).
            let curName = null, curGot = null, curSize = null, curSpeed = null, curEta = null;
            const files = stage.files || {};
            if (!done && !err) {
                const pickKey = stage.current_file;
                if (pickKey && files[pickKey]) {
                    const f = files[pickKey];
                    curName = f.filename || pickKey; curGot = f.downloaded || 0; curSize = f.size || 0;
                    curSpeed = f.speed_bps; curEta = f.eta_sec;
                } else if (stage.total_size > 0) {
                    // No active file row right now (first seconds) - plain line, live speed, no bar.
                    curName = 'Downloading model files';
                    curGot = stage.downloaded || 0; curSize = stage.total_size || 0;
                    curSpeed = stage.speed_bps; curEta = stage.eta_sec;
                }
            }
            if (curName != null) {
                const pct = (curSize > 0 && curGot != null) ? Math.min(100, curGot / curSize * 100) : 0;
                const bits = [];
                if (curSpeed > 0) bits.push(fmtSpeed(curSpeed));
                if (!done && !err && curEta != null && isFinite(curEta)) bits.push('ETA ' + fmtEta(curEta));
                let meta;
                if (curName === 'Downloading model files') {
                    // no file row yet - live speed only, never a progress bar here
                    meta = bits.length ? '<b>' + bits.join(' · ') + '</b>' : '';
                } else if (curSize > 0 && curGot != null) {
                    // real per-file row: its own live numbers; checkmark once that file is done
                    const starting = !done && !err && curGot <= 0;
                    const check = files[curName] && files[curName].done ? ' ✓' : '';
                    meta = starting ? ('waiting for first bytes…' + (bits.length ? ' <b>' + bits.join(' · ') + '</b>' : ''))
                         : fmtBytes(curGot) + ' / ' + fmtBytes(curSize) + ' (' + pct.toFixed(0) + '%)' + check +
                           (bits.length ? ' · <b>' + bits.join(' · ') + '</b>' : '');
                } else {
                    meta = '';
                }
                const bar = (curName !== 'Downloading model files' && curSize > 0 && !done && !err && curGot > 0)
                    ? '<div class="sp-bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>' : '';
                html += '<div class="sp-model-line">' +
                        '<span class="sp-model-label">Current</span>' +
                        '<span class="sp-file-name" title="' + escapeHtml(curName) + '">' +
                            escapeHtml(String(curName).split('/').pop()) + '</span>' +
                        '<span class="sp-file-meta">' + meta + '</span></div>' + bar;
            }
            // ---- line 2: TOTAL -------------------------------------------------------
            const tGot = stage.downloaded || 0;
            const tSize = stage.total_size || 0;
            const fDone = (stage.files_done != null) ? stage.files_done : null;
            const fTotal = (stage.files_total != null) ? stage.files_total : null;
            let tBits = [];
            if (!done && !err && stage.speed_bps > 0) tBits.push(fmtSpeed(stage.speed_bps));
            if (!done && !err && stage.eta_sec != null && isFinite(stage.eta_sec)) tBits.push('ETA ' + fmtEta(stage.eta_sec));
            let tMeta = '';
            const filePart = (fDone != null && fTotal != null) ? ('Files: ' + fDone + ' of ' + fTotal + ' done') : '';
            if (tSize > 0 || filePart) {
                const pct = (tSize > 0) ? Math.min(100, tGot / tSize * 100) : 0;
                const sizePart = (tSize > 0) ? (fmtBytes(tGot) + ' / ' + fmtBytes(tSize) + ' (' + pct.toFixed(0) + '%)') : '';
                const parts = [filePart, sizePart].filter(Boolean);
                tMeta = parts.join(' · ') + (tBits.length ? ' · <b>' + tBits.join(' · ') + '</b>' : '');
            } else if (!done && !err) {
                tMeta = 'checking files…';
            }
            const tBar = (tSize > 0 && !done && !err)
                ? '<div class="sp-bar"><i style="width:' + Math.min(100, tGot / tSize * 100).toFixed(1) + '%"></i></div>' : '';
            html += '<div class="sp-model-line">' +
                    '<span class="sp-model-label">Total</span>' +
                    '<span class="sp-file-meta sp-total-meta">' + tMeta + '</span></div>' + tBar;

            if (err && stage.detail) {
                html += '<div class="sp-line sp-detail">' + escapeHtml(stage.detail) + '</div>';
            }
            return html;
        }

        function renderStageRow(container, name, title, stage) {
            let row = container.querySelector('[data-stage="' + name + '"]');
            if (!row) {
                row = document.createElement('div');
                row.className = 'sp-row';
                row.dataset.stage = name;
                row.innerHTML = '<span class="sp-status"></span><span class="sp-title">' + escapeHtml(title) + '</span>' +
                                '<div class="sp-body"></div>';
                container.appendChild(row);
            }
            const body = row.querySelector('.sp-body');
            row.querySelector('.sp-status').innerHTML = statusIcon(stage ? stage.status : 'idle');

            if (!stage || !stage.status) { body.innerHTML = ''; return; }

            let html = '';
            if (name === 'pip' && stage.packages) {
                const pkgs = Object.values(stage.packages);
                html = '<div class="sp-pkgs">' + pkgs.map(pkgRow).join('') + '</div>' +
                        (stage.line ? '<div class="sp-line">' + escapeHtml(stage.line) + '</div>' : '');
            } else if (name === 'model') {
                html = modelBody(stage);
            } else {
                if (stage.detail) html += '<div class="sp-line sp-detail">' + escapeHtml(stage.detail) + '</div>';
                if (name === 'venv' && stage.status === 'running') html += '<div class="sp-line">Creating virtual environment…</div>';
            }
            body.innerHTML = html;
        }

        function renderCard(card, data) {
            const stagesEl = card.el.querySelector('.sp-stages');
            // (2026-09-24) Only THIS run's rows: the server writes a stage only when it actually
            // performed that work, and stale 'done' leftovers from an earlier first run are filtered
            // out by age. venv + pip in setup order; model only when present.
            const st = _freshStages(data.stages || {}, data);
            renderStageRow(stagesEl, 'venv', 'Virtual environment', st.venv);
            if (!st.venv) _dropRow(stagesEl, 'venv');
            renderStageRow(stagesEl, 'pip', 'Installing packages (pip)', st.pip);
            if (!st.pip) _dropRow(stagesEl, 'pip');
            if (st.model && st.model.status) {
                renderStageRow(stagesEl, 'model', 'Downloading model files', st.model);
            } else {
                _dropRow(stagesEl, 'model');
            }

            // Header summary: honest overall status word. "complete" is only set by stopCard()
            // once EVERY present stage reached a terminal state - never while one is running.
            const header = card.el.querySelector('.sp-header-status');
            if (header) {
                let state = 'waiting';
                if (Object.values(st).some(s => s && s.status === 'error')) state = 'error';
                else if (Object.values(st).some(isActive)) state = 'running';
                else if (st.venv && st.venv.status === 'done' && st.pip && st.pip.status === 'done') state = 'ready';
                header.textContent = { running: 'in progress…', error: 'failed', ready: 'dependencies ready', waiting: 'waiting for setup data…' }[state];
                card.el.dataset.state = state;
            }

            // (2026-09-24) Attach the card to the chat ONLY when this run has real setup data -
            // a fully-cached machine produces no rows, so its section stays invisible.
            if (!card.attached && Object.keys(st).length > 0 && card.el.parentNode === null) {
                const container = document.getElementById('chatContainer');
                if (container) {
                    container.appendChild(card.el);
                    card.attached = true;
                    if (window.COOLEMS && COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
                }
            }
        }

        function schedule(card, ms) {
            clearTimeout(card.timer);
            card.timer = setTimeout(() => poll(card), ms || POLL_MS);
        }

        /** Decide whether polling continues after one response. Returns true when the next
         *  poll is scheduled, false when the run reached its end (caller finalizes). */
        function decideNext(card, data) {
            const st = _freshStages((data && data.stages) || {}, data);
            if (Object.values(st).some(isActive)) { schedule(card); return true; }

            // Nothing active. No state file yet: setup has not started (or the runtime is fully
            // cached from a previous run) - keep waiting up to ~60 s before dropping the card.
            if (!data.updated_at) {
                if (card.emptyPolls >= MAX_POLLS_NO_STATE) return false;
                schedule(card);
                return true;
            }

            // (2026-09-24) Only stale leftovers from an EARLIER run remain - nothing happened this
            // turn: finalize immediately. The card was never attached, so the section stays invisible.
            if (!Object.keys(st).length) return false;

            // venv + pip finished but no model stage yet: the tool starts its model pre-flight
            // right after bootstrap - keep a short grace window open so that download is picked
            // up live instead of being invisible. After the grace expires with nothing coming,
            // this run genuinely has no more setup work and can be finalized as complete.
            const venvDone = st.venv && st.venv.status === 'done';
            const pipDone = st.pip && st.pip.status === 'done';
            const modelPresent = !!(st.model && st.model.status);
            if (venvDone && pipDone && !modelPresent) {
                card.gracePolls = (card.gracePolls || 0) + 1;
                if (card.gracePolls < MODEL_GRACE_POLLS) { schedule(card); return true; }
            }
            return false; // every present stage is terminal - finalize
        }

        function stopCard(toolName, lastData) {
            const card = activeCards.get(toolName);
            if (!card) return;
            clearTimeout(card.timer);
            card.timer = null;
            // Final state: mark the card finished and add an explicit completion line so it is
            // unambiguous that setup is over (only reached when nothing is still running).
            if (lastData && lastData.updated_at && card.el) {
                // (2026-09-24) fresh rows only: stale 'done' leftovers from an earlier first run must
                // not be reported as this turn's completion.
                const st = _freshStages(lastData.stages || {}, lastData);
                const failed = Object.values(st).some(s => s && s.status === 'error');
                const header = card.el.querySelector('.sp-header-status');
                if (header) header.textContent = failed ? 'failed' : 'complete';
                card.el.dataset.state = failed ? 'error' : 'done';
                if (!failed && !card.el.querySelector('.sp-complete')) {
                    const parts = [];
                    if (st.venv && st.venv.status === 'done') parts.push('virtual environment ready');
                    if (st.pip && st.pip.status === 'done') {
                        const n = Object.values(st.pip.packages || {}).length;
                        parts.push(n ? n + ' package' + (n > 1 ? 's' : '') + ' installed' : 'packages ready');
                    }
                    if (st.model && st.model.status === 'done') {
                        const m = st.model.total_size ? fmtBytes(st.model.total_size) : '';
                        parts.push('model files downloaded' + (m ? ' (' + m + ')' : ''));
                    }
                    const d = document.createElement('div');
                    d.className = 'sp-complete';
                    d.textContent = '✓ Setup complete — ' + (parts.join(', ') || 'all stages done') + '.';
                    card.el.querySelector('.sp-body').appendChild(d);
                }
            }
            activeCards.delete(toolName);
        }

        function poll(card) {
            if (!activeCards.has(card.toolName)) return; // stopped in the meantime
            fetch('/api/setup/status?tool=' + encodeURIComponent(card.toolName))
                .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
                .then(data => {
                    if (!activeCards.has(card.toolName)) return;
                    card.emptyPolls = (data && data.updated_at) ? 0 : card.emptyPolls + 1;
                    renderCard(card, data);
                    const keepGoing = decideNext(card, data);
                    if (!keepGoing) {
                        stopCard(card.toolName, data);
                        // No fresh setup data this turn (runtime already cached): the card was
                        // never attached to the DOM - nothing to clean up, section stays invisible.
                        if (card.el && !Object.keys(_freshStages((data || {}).stages || {}, data)).length && card.el.parentNode) card.el.remove();
                    }
                })
                .catch(() => {
                    // Transient network error: retry a few times, then give up quietly.
                    if (!activeCards.has(card.toolName)) return;
                    card.emptyPolls += 1;
                    if (card.emptyPolls >= MAX_POLLS_NO_STATE) stopCard(card.toolName, null);
                    else schedule(card, POLL_MS * 2);
                });
        }

        /** Render (or resume) the Setup/Downloads section for one tool. Idempotent per tool. */
        function start(toolName) {
            if (!toolName || activeCards.has(toolName)) return;
            const container = document.getElementById('chatContainer');
            if (!container) return;

            const el = document.createElement('div');
            el.className = 'sp-card';
            el.dataset.state = 'waiting';
            el.innerHTML =
                '<div class="sp-header"><span class="sp-title">⬇️ Setup / Downloads — ' + escapeHtml(toolName) + '</span>' +
                '<span class="sp-header-status">waiting for setup data…</span></div>' +
                '<div class="sp-body"><div class="sp-stages"></div></div>';
            // (2026-09-24) NOT appended yet: renderCard() attaches it to the chat only when this
            // run actually produces setup data - a fully-cached machine never shows the section.
            void container;

            const card = { toolName, el, timer: null, emptyPolls: 0, attached: false };
            activeCards.set(toolName, card);
            poll(card);

            if (window.COOLEMS && COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
        }

        /** One-shot settle: called from 'tool_end'. If no setup state ever appeared this turn
         *  (runtime was already cached / the tool bailed before bootstrap), drop the empty card
         *  immediately; otherwise render the final state and let decideNext() keep polling while
         *  anything is still running - it never declares completion on its own. */
        function settle(toolName) {
            const card = activeCards.get(toolName);
            if (!card) return;
            fetch('/api/setup/status?tool=' + encodeURIComponent(card.toolName))
                .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
                .then(data => {
                    if (!activeCards.has(card.toolName)) return;
                    card.emptyPolls = (data && data.updated_at) ? 0 : card.emptyPolls + 1;
                    renderCard(card, data);
                    const keepGoing = decideNext(card, data);
                    if (!keepGoing) {
                        stopCard(toolName, data);
                        // No fresh setup data this turn -> the card was never attached; drop it if
                        // an older state file ever made it visible.
                        if (card.el && !Object.keys(_freshStages((data || {}).stages || {}, data)).length && card.el.parentNode) card.el.remove();
                    }
                })
                .catch(() => { /* transient - the regular poll loop keeps running */ });
        }

        /** Stop + remove the section for a tool (e.g. when its turn ends without setup). */
        function stop(toolName, keepCard) {
            const card = activeCards.get(toolName);
            if (!card) return;
            clearTimeout(card.timer);
            activeCards.delete(toolName);
            if (!keepCard && card.el && card.el.parentNode) card.el.remove();
        }

        window.SetupProgressUI = { start, stop, settle };
    })();
