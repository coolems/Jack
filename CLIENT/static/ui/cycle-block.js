/**
 * cycle-block.js - The loop-history block (2026-11-08).
 *
 * ONE collapsed line per agentic turn, replacing the old flat chat view:
 *
 *   [turns 4 · tools 7 · confirmations 2 · ⏱ 1:24 · ⚡ 21 tok/s]   collapsed
 *   ├ expanded body : chronological rows - steps, tools, consent decisions, notices, speech.
 *   │                 ONE ROW = ONE LINE (user request 2026-11-08): turn · kind · name ·
 *   │                 time · speed · text. Everything else of that entry (io frames, full
 *   │                 text, payload, thumb) opens only when the ROW's own line is clicked.
 *   ├ last-tool strip: ONE info line (#number · name · status · live timer) + FULL input/output.
 *   │                 It appears with the FIRST tool_start of the turn and is REMOVED the
 *   │                 moment the final answer starts being delivered (cycle_final), and by
 *   │                 settle() for a turn that ends without that frame (done / error / stop).
 *   │                 It is LIVE-WORK only: a settled turn - and a turn rebuilt from the DB -
 *   │                 shows no strip; its tools stay as rows of the expanded body.
 *   ├ live strip    : the transient consent card (answerable while collapsed)
 *   └ AI strip      : the LAST AI message of THIS loop, streamed live (thinking + answers)
 *                 ONE AI MESSAGE PER LOOP (user request 2026-11-09: "in each history loop
 *                 block we need to see this last ai message ... my message, then the turns
 *                 loop block, then the last AI message for that loop"). refreshAiVisibility()
 *                 marks each block's strip from ITS OWN text: a loop that said something keeps
 *                 its strip, a loop that said nothing shows no strip at all (no placeholder, no
 *                 Copy chips, no border). INSIDE a strip only the NEWEST message is shown
 *                 (CycleShell.showLastSegment) - every reasoning step of a loop is a message,
 *                 so message N overwrites message N-1 instead of stacking under it.
 *                 Nothing is deleted: the earlier messages stay in dataset.raw (Copy reads the
 *                 whole turn), as speech rows of the expanded body and in the turn_log payload.
 *
 * THE ROUTING CONTRACT (the part the user called out as critical): every frame resolves
 * its block through CycleBlock.target(convId, turnId), which requires ALL of:
 *   1. the frame's OWN conv_id is the workspace on screen,
 *   2. the block node still lives inside the current #chatContainer,
 *   3. the turn is still OPEN.
 * Plus an owners map: turn_id -> conv_id. A turn_id already owned by another conversation
 * is REFUSED (open() and rebuild()), so one workspace's history can never merge into
 * another window's block - on a live push, a replayed background frame or a DB reload.
 *
 * STEERING (2026-11-08, user request): a steering message is LOOP WORK, so it must NEVER
 * appear in the chat as a user message. It is a fact of the turn that was running when the
 * user typed it, so it enters THIS block as a row at the moment it was typed
 * (at_ts = received_at) and its status line settles sending -> queued -> applied (or
 * rejected). Blocks own their steers by steer_id, exactly like they own consent cards by
 * request_id, so one steering message is ONE row whose status changes - never a duplicate.
 * Nothing about a steer is painted as a chat-level message on reload either: the DB row is
 * flagged `is_steer` and conversation.js skips it - the turn_log payload carries it in
 * `steers`, which is where it belongs.
 *
 * DOM rendering lives in cycle-shell.js (strips) and cycle-rows.js (rows); this file owns
 * identity, counters, timers and clearing. Blocks live per conversation (blocks[convId])
 * and are cleared when a workspace's view is dropped/deleted. clearAll() wipes owners too.
 */

const CycleBlock = {
    blocks: {},    // convId -> Block[] (chronological, oldest first)
    owners: {},    // turnId -> convId  (one turn can never belong to two workspaces)
    timers: [],    // live tick intervals: turn timer + last-tool timer

    // ===== identity =====

    /** Copy buttons for the AI answer strip (2026-11-08, restored to the REPO shape): in the
     *  last repo version an answer was a .message-content bubble with a Copy chip floating
     *  at its top-right AND bottom-right, revealed on hover. A turn's answer now lives in
     *  this strip, so the strip gets the same two floating chips (.copy-top / .copy-bottom)
     *  and the same hover reveal; copyCycleAnswer(this) reads the strip's raw answer text.
     *
     *  data-visible="0" (2026-11-09, user request "the AI response block shows every turn's
     *  response, I only need the last one"): a fresh strip starts hidden, because a loop's
     *  strip becomes visible only when that loop produces a real AI message - see
     *  refreshAiVisibilityBlock(). Nothing about the answer is lost: the turn's speech rows are
     *  still built in the expanded body and persisted in the turn_log payload. */
    _aiStripHtml() {
        const copyIcon = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> Copy';
        return '<div class="cycle-ai" data-empty="1" data-visible="0">' +
            '<button class="copy-btn copy-top" onclick="copyCycleAnswer(this)">' + copyIcon + '</button>' +
            '<div class="cycle-ai-empty">Waiting for the AI\u2026</div>' +
            '<button class="copy-btn copy-bottom" onclick="copyCycleAnswer(this)">' + copyIcon + '</button></div>';
    },

    ownerOf(turnId) { return turnId ? (this.owners[turnId] || '') : ''; },

    get(convId, turnId) {
        if (!convId || !turnId) return null;
        const list = this.blocks[convId] || [];
        return list.find(b => b.turnId === turnId) || null;
    },

    /** THE routing guard. Returns the block only when it is safe to render into it. */
    target(convId, turnId) {
        if (!convId || !turnId) return null;
        if (convId !== COOLEMS.currentConversation) return null;
        if (this.ownerOf(turnId) !== convId) return null;
        const block = this.get(convId, turnId);
        if (!block || !block.open) return null;
        const container = document.getElementById('chatContainer');
        if (!container || !container.contains(block.node)) return null;
        return block;
    },

    /** The block currently RUNNING in this workspace (frames without a turn_id). */
    current(convId) {
        if (!convId || convId !== COOLEMS.currentConversation) return null;
        const list = this.blocks[convId] || [];
        return list.find(b => b.open) || null;
    },

    // ===== open / rebuild =====

    _makeBlock(convId, turnId, startedAtMs, lastTurnSec) {
        const node = document.createElement('div');
        node.className = 'cycle-block collapsed';
        node.dataset.convId = convId;
        node.dataset.turnId = turnId;
        node.innerHTML =
            '<div class="cycle-head" tabindex="0">' +
                '<span class="cycle-caret">&#9656;</span>' +
                '<span class="cycle-summary"></span>' +
                '<span class="cycle-timer" data-role="turn">0:00</span>' +
            '</div>' +
            '<div class="cycle-body"></div>' +
            '<div class="cycle-last" style="display:none;"></div>' +
            '<div class="cycle-live"></div>' +
            this._aiStripHtml();

        const block = {
            convId, turnId, node, open: true, settled: false,
            startedAt: startedAtMs,
            lastTurnSec: lastTurnSec || 0,
            counts: { turns: 0, tools: 0, tools_ok: 0, consents: 0, grants: 0, steers: 0, notices: 0, children: 0, jobs: 0 },
            generated: 0, speed: 0,
            rows: [],            // {kind, step_no, at_ts, el} kept in at_ts order
            consents: {},        // request_id -> row element
            steerRows: {},       // steer_id -> {el, state} (ONE row per steering message)
            // (2026-12 runtime) ONE row per notice / child / job, settled from its frames —
            // the same discipline as steerRows and toolNoOf: a late frame never duplicates.
            noticeRows: {},      // notice_id -> {el, state}
            childRows: {},       // child_id -> row element
            jobRows: {},         // job_id -> row element
            questionRows: {},    // request_id -> row element (the card itself is transient)
            toolNoOf: {},        // call_id -> 1-based tool number (one row per call)
            toolEls: {},         // call_id -> row element
            imageRows: {},       // call_id -> row element (2026-12: one row per generated image)
            // ONE segment per REASONING STEP (user request 2026-11-08): step_no -> {thinkingEl,
            // answerEl}. Each step's answer is its own element, so answers of turn 5 and turn 6
            // stack one UNDER another instead of running together in one box. A steer is not a
            // step, so it never opens a new segment.
            aiSeg: {},
            lastCallId: '', lastEl_: null, lastTimer: null, lastToolClosed: false,
            finalText: '', turnTimer: null,
        };
        block.head = node.querySelector('.cycle-head');
        block.summaryEl = node.querySelector('.cycle-summary');
        block.timerEl = node.querySelector('.cycle-timer');
        block.bodyEl = node.querySelector('.cycle-body');
        block.lastEl = node.querySelector('.cycle-last');
        block.liveEl = node.querySelector('.cycle-live');
        block.aiEl = node.querySelector('.cycle-ai');
        // THE ONE-MESSAGE-PER-LOOP RULE (2026-11-09, user request): a fresh block starts
        // hidden. It owns a visible AI strip only when its own AI text arrives, which
        // refreshAiVisibilityBlock() decides. The markup already carries data-visible="0".
        block.aiVisible = false;
        block.head.addEventListener('click', () => this.toggle(block));
        block.head.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.toggle(block); }
        });
        return block;
    },

    /**
     * Create one block for one live turn.
     * `before` = node to insert before (DB anchoring), null = append.
     * `live` = a REAL turn start: only then may other open blocks of this workspace be
     * settled, so a rebuilt block or a late cycle_end of an OLD turn can never silently
     * close the turn that is running right now.
     */
    open(convId, turnId, frame, before, live) {
        if (!convId || !turnId) return null;
        const existing = this.get(convId, turnId);
        if (existing) return existing;

        const owner = this.ownerOf(turnId);
        if (owner && owner !== convId) return null;   // turn_id claimed by another workspace
        this.owners[turnId] = convId;

        if (live) {
            for (const b of this.blocks[convId] || []) {
                if (b.open) this.settle(b, b.finalText || '', true);
            }
        }

        const container = document.getElementById('chatContainer');
        if (!container) return null;

        const block = this._makeBlock(convId, turnId,
            Number(frame && frame.turn_started_at ? frame.turn_started_at * 1000 : Date.now()),
            Number(frame && frame.last_turn_sec ? frame.last_turn_sec : 0));

        CycleShell.hideWelcome();
        if (before && before.parentNode === container) container.insertBefore(block.node, before);
        else container.appendChild(block.node);

        (this.blocks[convId] = this.blocks[convId] || []).push(block);
        this._renderSummary(block);
        this._startTurnTimer(block);
        // A new turn has no text yet, so its strip stays hidden - the previous loop's last
        // message remains on screen until this one produces its own.
        this.refreshAiVisibilityBlock(block);
        CycleShell.scrollIfNeeded();
        return block;
    },

    /** Rebuild a SETTLED turn from its persisted turn_log row (delegates to CycleShell). */
    rebuild(convId, row, before) { return CycleShell.rebuild(convId, row, before); },

    toggle(block) {
        const expanded = block.node.classList.toggle('expanded');
        block.node.classList.toggle('collapsed', !expanded);
        block.head.querySelector('.cycle-caret').textContent = expanded ? '\u25BE' : '\u25B6';
    },

    /** Close a turn: stop timers, fold pending cards/chips, freeze the timer line.
     *  The last-tool strip is REMOVED here (user's rule 2026-11-08, revised): it is a live
     *  work surface - it shows the tool that is RUNNING. Once the turn is over (and normally
     *  already at cycle_final, when the answer starts being delivered) it must not stay on
     *  screen. Its history is not lost: every call is a row of the expanded body.
     *  The streamed answer also gets its markdown render here (CycleShell.settleStream),
     *  so the live plain-text stream becomes the formatted final answer. */
    settle(block, finalText, stopped) {
        if (!block || block.settled) return;
        block.settled = true;
        block.open = false;
        block.finalText = finalText || '';
        this._stopTurnTimer(block);
        CycleShell.clearLastTool(block);
        CycleShell.settleStream(block);
        // The turn is over: it keeps its strip only if it actually produced a message, and
        // that strip collapses to its newest message (no stack of steps left on screen).
        this.refreshAiVisibilityBlock(block);
        for (const rid of Object.keys(block.consents)) {
            const el = block.consents[rid];
            if (el && el.dataset.decision === 'pending') this.settleCard(block, rid, 'unanswered');
        }
        // Unanswered question cards must disappear with the turn, not stay on screen.
        for (const rid of Object.keys(block.questionRows || {})) {
            const el = block.questionRows[rid];
            if (el && el.dataset.pending !== '0') this.settleQuestion(block, rid, 'unanswered');
        }
        block.node.classList.add('settled');
        if (stopped) block.node.classList.add('stopped');
        this._renderSummary(block);
    },

    // ===== rows =====

    /** Insert one row at its real time position.
     *  `allowClosed` = also accept a SETTLED block: a steering message that the loop never
     *  reached is rejected AFTER the turn ended, and that rejection is still a fact of
     *  THIS turn - it belongs in the turn's block, not in the chat. */
    addRow(block, kind, d, allowClosed) {
        if (!block) return null;
        if (!block.open && !allowClosed) return null;
        const el = document.createElement('div');
        el.className = 'cycle-row cycle-row-' + kind;
        el.dataset.ownerConvId = block.convId;
        el.dataset.ownerTurnId = block.turnId;
        el.dataset.kind = kind;
        el.dataset.stepNo = String(d.step_no || 0);
        const atTs = Number(d.at_ts || (Date.now() / 1000));
        el.dataset.atTs = String(atTs);
        block.bodyEl.appendChild(el);
        CycleRows.render(kind, el, d);

        const row = { kind, step_no: d.step_no || 0, at_ts: atTs, el };
        block.rows.push(row);
        // Rows arrive nearly in order; only a late frame needs a re-sort.
        if (block.rows.length > 1 && row.at_ts < block.rows[block.rows.length - 2].at_ts) {
            block.rows.sort((a, b) => a.at_ts - b.at_ts);
            for (const r of block.rows) block.bodyEl.appendChild(r.el);
        }
        return el;
    },

    bump(block, key, n) {
        block.counts[key] = (block.counts[key] || 0) + (n || 1);
        this._renderSummary(block);
    },

    _renderSummary(block) {
        const c = block.counts;
        const parts = [`turns ${c.turns}`];
        // ONE tool count only (user request 2026-11-08): the old "(N ok)" suffix was
        // rendered here and was wrong/doubled, so it is gone. Per-call ok/failed still
        // reads on the tool rows and on the live last-tool strip. addTool()
        // counts a call_id exactly once (tool_end and cycle_tool describe the SAME call).
        parts.push(`tools ${c.tools}`);
        if (c.consents) parts.push(`confirmations ${c.consents}`);
        if (c.grants) parts.push(`auto-allow ${c.grants}`);
        // Steering messages are rows of this block (2026-11-08 user request), so the head
        // line counts them - the number always has rows behind it.
        if (c.steers) parts.push(`steers ${c.steers}`);
        if (c.notices) parts.push(`notices ${c.notices}`);
        // (2026-12 runtime) delegated children and background jobs of THIS turn, so the
        // head line shows work that is still running when the turn is open.
        if (c.children) parts.push(`subagents ${c.children}`);
        if (c.jobs) parts.push(`jobs ${c.jobs}`);
        // NO TOKEN COUNTERS ON THE HEAD LINE (user request 2026-11-08): "X.XK gen" is gone.
        // block.generated is still tracked (token_stats / turn_stats fold it in) because the
        // chat-level token bar reads it; only the block stopped printing it. Speed stays -
        // the user asked for speed on the row lines and on this head line.
        if (block.speed) parts.push(`\u26A1 ${block.speed.toFixed(1)} tok/s`);
        if (block.settled && block.lastTurnSec) parts.push(`\u21A9 ${_fmtSec(block.lastTurnSec)} last`);
        block.summaryEl.textContent = parts.join(' \u00b7 ');
    },

    // ===== AI strip visibility (ONE message per agentic loop) =====

    /** Does this loop have a real AI message? The "Waiting for the AI..." placeholder and the
     *  two Copy chips are not text, so only a painted answer/thinking segment counts - a turn
     *  that said nothing shows no AI strip at all (user rule 2026-11-09: one message per loop,
     *  no empty chrome). */
    _hasAiText(block) {
        if (!block || !block.aiEl) return false;
        return !!CycleShell.lastSegmentOf(block);
    },

    /**
     * THE LAST-MESSAGE-PER-LOOP RULE (2026-11-09, user's wording: "when a new ai message from
     * another turn arrives it must overwrite that old message in UI ... and in each history
     * loop block we need to see this last ai message").
     *
     * Visibility is decided PER BLOCK, not per workspace:
     *   * a turn whose AI text exists keeps its strip visible - one AI message per loop, so a
     *     reload reads: my message, that loop's turns block, that loop's last AI message;
     *   * inside that strip only the NEWEST message is shown (CycleShell.showLastSegment), so
     *     while a loop runs message N replaces message N-1 instead of stacking under it;
     *   * a turn with no AI text shows NO strip - no "Waiting for the AI..." chrome, no Copy
     *     chips, no empty border.
     *
     * HIDDEN, never deleted: the earlier messages of a loop stay in each segment's
     * dataset.raw (Copy still copies the whole turn), as speech rows of the expanded body and
     * in the turn_log payload. Blocks of another workspace are untouched - a background
     * workspace's frames can never rewrite the visible one.
     */
    refreshAiVisibilityBlock(block) {
        if (!block || !block.aiEl) return false;
        const show = this._hasAiText(block) ? '1' : '0';
        if (block.aiEl.dataset.visible !== show) block.aiEl.dataset.visible = show;
        block.aiVisible = show === '1';
        // Collapse this loop's strip to its newest message (the overwrite the user asked
        // for); a hidden strip needs no segment flags.
        if (show === '1') CycleShell.showLastSegment(block);
        return block.aiVisible;
    },

    /**
     * The same rule applied to every loop of one workspace (view open, reload rebuild,
     *  workspace switch). Blocks of another workspace are never touched.
     *
     * `owner` = the NEWEST loop that has a real AI message: that is the strip the user is
     *  watching while a turn runs, so a new AI message overwrites the previous one there.
     *  Every other loop that has a message keeps its own strip and collapses it to its own
     *  newest message, so the chat reads: my message, that loop's turns block, that loop's
     *  last AI message - for every loop.
     */
    refreshAiVisibility(convId) {
        const list = this.blocks[convId] || [];
        let owner = null;
        for (const b of list) {
            const show = this._hasAiText(b) ? '1' : '0';
            b.aiEl.dataset.visible = show;
            b.aiVisible = show === '1';
            if (this._hasAiText(b)) owner = b;   // the NEWEST loop with text owns the live strip
        }
        if (owner) CycleShell.showLastSegment(owner);
        for (const b of list) if (b.aiVisible && b !== owner) CycleShell.showLastSegment(b);
        return list.filter(b => b.aiVisible);
    },

    // ===== live timers =====

    _startTurnTimer(block) {
        const tick = () => {
            if (!block.open) return;
            block.timerEl.textContent = _fmtSec((Date.now() - block.startedAt) / 1000);
            block.timerEl.classList.add('live');
        };
        tick();
        const t = setInterval(tick, 1000);
        this.timers.push(t);
        block.turnTimer = t;
    },

    _stopTurnTimer(block) {
        if (block.turnTimer) { clearInterval(block.turnTimer); delete block.turnTimer; }
        block.timerEl.classList.remove('live');
        block.timerEl.textContent = _fmtSec((Date.now() - block.startedAt) / 1000);
    },

    /** Live timer for the newest tool, ticking while that call executes.
     *  Same shape as the frozen value (_fmtMs: 0.4s / 1.2s / 1:04), so the number on the
     *  info line reads consistently while running and after the result lands. */
    startToolTimer(block, callId, startedAtSec) {
        if (!block || !block.lastEl_) return;
        this.stopToolTimer(block);
        block.lastCallId = callId || '';
        const start = startedAtSec ? startedAtSec * 1000 : Date.now();
        const timerEl = block.lastEl_.querySelector('.cycle-last-timer');
        if (!timerEl) return;
        const tick = () => { timerEl.textContent = _fmtMs(Date.now() - start); };
        tick();
        block.lastTimer = setInterval(tick, 250);
        this.timers.push(block.lastTimer);
    },

    stopToolTimer(block) {
        if (!block || !block.lastTimer) return;
        clearInterval(block.lastTimer);
        delete block.lastTimer;
    },

    // ===== frame handlers (websocket.js calls these) =====

    addStep(block, d) {
        const s = d.step || {};
        block.generated = Math.max(block.generated, Number(s.turn_generated || 0));
        if (s.speed_tps) block.speed = s.speed_tps;
        this.addRow(block, 'step', d);
        this.bump(block, 'turns');
    },

    /** tool_start: the last-tool strip appears and its timer starts ticking. */
    addToolStart(block, d) { CycleShell.showRunningTool(block, d); },

    /**
     * cycle_tool / tool_end: ONE row per call, one count.
     *
     * THE COUNT FIX (2011-08 -> 2026-11-08, user request): ToolExecutor sends `tool_end`
     * and the recorder sends `cycle_tool` for the SAME call. Both used to add a row and
     * bump the counter, so the head line printed "tools 48" for 24 calls. A call_id is
     * seen once; the second frame only refreshes the last-tool strip.
     */
    addTool(block, d) {
        if (!block || !block.open) return null;
        const callId = (d.call_id || (d.tool && d.tool.call_id) || '');
        if (callId && block.toolNoOf && block.toolNoOf[callId]) {
            CycleShell.finishLastTool(block, Object.assign({}, d, { no: block.toolNoOf[callId] }));
            return block.toolEls[callId];
        }
        const el = this.addRow(block, 'tool', d);
        this.bump(block, 'tools');
        const no = block.counts.tools;
        if (callId) {
            block.toolNoOf = block.toolNoOf || {};
            block.toolEls = block.toolEls || {};
            block.toolNoOf[callId] = no;
            block.toolEls[callId] = el;
        }
        // ok lives at the frame top level (cycle_tool) and inside the tool row; accept both.
        const okFlag = d.ok !== undefined ? !!d.ok : !!(d.tool && d.tool.ok);
        if (okFlag) this.bump(block, 'tools_ok');
        CycleShell.finishLastTool(block, Object.assign({}, d, { no: no }));
        return el;
    },

    addImage(block, d) {
        // (2026-12) ONE row per call: the live 'image_generated' frame and the recorder's
        // persisted 'cycle_image' identity frame describe the SAME image - a repeat by
        // call_id must not print a second row. Rows without a call_id always paint.
        const imgId = d.call_id || '';
        if (imgId && block.imageRows && block.imageRows[imgId]) return block.imageRows[imgId];
        const el = this.addRow(block, 'image', d);
        if (!el) return null;
        if (imgId) {
            block.imageRows = block.imageRows || {};
            block.imageRows[imgId] = el;
        }
        // Thumb in the last-tool strip while it is on screen. Same two sources as the row:
        // inline base64 when the live frame carries pixels, otherwise the HTTP download URL.
        if (block.lastEl_) {
            const img = document.createElement('img');
            img.className = 'cycle-image-thumb';
            if (d.image_base64) {
                img.src = 'data:image/png;base64,' + d.image_base64;
            } else if (d.rel_path || d.filename) {
                const rel = String(d.rel_path || d.filename).replace(/\\/g, '/');
                img.src = '/api/download?path=' + encodeURIComponent(rel) + '&inline=true';
            }
            img.alt = d.filename || 'Generated image';
            if (d.file_path) {
                img.dataset.filePath = d.file_path;
                img.addEventListener('click', () => window.open(img.dataset.filePath));
            }
            block.lastEl.appendChild(img);
        }
        return el;
    },

    /** A consent card appeared: a pending row inside the block + the card in the live strip. */
    addCard(block, data) {
        if (!block || !block.open) return null;
        if (data.request_id && block.consents[data.request_id]) return block.consents[data.request_id];
        // (2026-12 ask_user_question) A question card gets NO consent row — its durable fact
        // is the 'question' row created by cycle_question_pending (setQuestion), so adding a
        // consent row here would print the same card twice. Only the card is mounted.
        if ((data.kind || '') === 'question') {
            if (data.request_id) block.questionCards = block.questionCards || {};
            if (data.request_id) block.questionCards[data.request_id] = true;
            CycleShell.mountCard(block, data);
            return null;
        }
        const el = this.addRow(block, 'consent', {
            step_no: block.counts.turns, at_ts: Date.now() / 1000,
            kind: data.kind || 'python', decision: 'pending', payload: data.code || '',
        });
        el.dataset.requestId = data.request_id || '';
        el.dataset.kind = data.kind || 'python';
        el.dataset.decision = 'pending';
        if (data.request_id) block.consents[data.request_id] = el;
        this.bump(block, 'consents');
        CycleShell.mountCard(block, data);
        return el;
    },

    /** Fold an answered card into a decision line - the interactive card disappears. */
    settleCard(block, requestId, decision) {
        const el = requestId ? block.consents[requestId] : null;
        if (!el) return false;
        el.dataset.decision = decision || 'answered';
        el.classList.add('cycle-consent-settled');
        const line = el.querySelector('.cycle-consent-line');
        if (line) line.textContent = _consentLabel(decision, el.dataset.kind);
        CycleShell.removeCard(requestId);
        return true;
    },

    /** Fold a QUESTION row that settled WITHOUT the user answering: row folds to the
     *  real decision, card removed by request id. Question rows live in `questionRows`,
     *  not `consents`, so the generic settle cannot reach them.
     */
    settleQuestion(block, requestId, decision) {
        if (!block || !requestId) return false;
        const el = block.questionRows[requestId];
        if (el) {
            el.dataset.decision = decision || 'unanswered';
            el.dataset.pending = '0';
            el.classList.add('cycle-consent-settled');
            const line = el.querySelector('.cycle-consent-line');
            if (line) line.textContent = _questionLabel(el.dataset.decision, false);
        }
        CycleShell.removeCard(requestId);
        return !!el;
    },

    /** A per-kind auto-allow unlock (no card was shown). */
    addGrant(block, d) { this.addRow(block, 'grant', d); this.bump(block, 'grants'); },

    addNotice(block, d) {
        const el = this.addRow(block, 'notice', d);
        if (el && d.level === 'error') el.dataset.level = 'error';
        this.bump(block, 'notices');
        return el;
    },

    /**
     * ONE steering message = ONE row of this block (2026-11-08 user request).
     *
     * `d.at_ts` is WHEN THE USER TYPED it, so addRow() places the row in the block's
     * chronological body at that moment - between the tool rows of the step that was
     * running, never at the end of the chat. `d.state` settles the row's status line
     * (sending -> received/queued -> applied, or rejected with the reason).
     *
     * Called for every steer frame (cycle_steer from the recorder + the steer_received /
     * steer_applied / steer_rejected acks). A steer is NEVER painted as a chat-level
     * message, so there is no fallback bubble anywhere: a frame whose block is not on
     * screen yet is held by message.js and painted when that block opens.
     */
    setSteer(block, d) {
        if (!block) return null;
        const steerId = d.steer_id || '';
        const state = d.state || (d.applied ? 'applied'
            : (d.rejected ? 'rejected' : (d.received ? 'received' : 'sending')));
        // A steer the loop never reached is rejected AFTER the turn ended - that rejection
        // is still a fact of THIS turn, so it may enter the settled block. A queued steer
        // needs the turn still open.
        const allowClosed = state === 'rejected' || state === 'applied';
        if (!block.open && !allowClosed) return null;
        const atTs = d.at_ts || (Date.now() / 1000);
        const existing = steerId ? block.steerRows[steerId] : null;
        if (existing) {
            // Status only ever moves forward; a late ack must not undo an applied row.
            const rank = (typeof _STEER_STATE_RANK === 'object')
                ? _STEER_STATE_RANK : { sending: 0, received: 1, applied: 2, rejected: 2 };
            if ((rank[state] || 0) >= (rank[existing.state] || 0)) {
                existing.state = state;
                existing.el.dataset.steerState = state;
                const status = existing.el.querySelector('.cycle-steer-status');
                if (status) status.textContent = _steerStatusLabel(state, d.reason);
            }
            return existing.el;
        }
        const el = this.addRow(block, 'steer', {
            step_no: d.step_no || block.counts.turns, at_ts: atTs,
            steer_id: steerId, text: d.text || d.content || '', state, reason: d.reason,
        }, allowClosed);
        if (!el) return null;
        if (steerId) block.steerRows[steerId] = { el, state };
        this.bump(block, 'steers');
        return el;
    },

    // ===== RUNTIME lane (2026-12 subagents / background jobs / questions) =====
    //
    // A runtime notice is NOT user speech and NOT loop guidance, so it gets its own row
    // kind ('runtime') instead of reusing 'notice'. One row per notice_id, with a status
    // line that only moves forward (queued -> applied, or deferred) — the same discipline
    // as setSteer(), so a late frame never rewrites a settled row.

    /** notice_queued / notice_applied / notice_deferred / cycle_notice -> ONE row. */
    setNotice(block, d) {
        if (!block) return null;
        const noticeId = d.notice_id || '';
        const state = d.state || (d.applied ? 'applied'
            : (d.deferred ? 'deferred' : (d.queued ? 'queued' : 'arrived')));
        // An applied notice is a fact of the turn even after it settled; a queued one
        // needs the turn still open.
        const allowClosed = state === 'applied' || state === 'deferred';
        if (!block.open && !allowClosed) return null;

        const rank = (typeof _RUNTIME_STATE_RANK === 'object')
            ? _RUNTIME_STATE_RANK : { arrived: 0, queued: 1, deferred: 2, applied: 3 };
        const existing = noticeId ? block.noticeRows[noticeId] : null;
        if (existing) {
            if ((rank[state] || 0) >= (rank[existing.state] || 0)) {
                existing.state = state;
                existing.el.dataset.noticeState = state;
                const status = existing.el.querySelector('.cycle-runtime-status');
                if (status) status.textContent = _runtimeStatusLabel(state, d.reason);
            }
            return existing.el;
        }
        const el = this.addRow(block, 'runtime', {
            step_no: d.step_no || block.counts.turns,
            at_ts: d.at_ts || (Date.now() / 1000),
            notice_id: noticeId, source: d.source || 'runtime',
            text: d.text || d.content || '', state, reason: d.reason,
        }, allowClosed);
        if (!el) return null;
        if (noticeId) block.noticeRows[noticeId] = { el, state };
        this.bump(block, 'notices');
        return el;
    },

    /** cycle_child: one row per delegated child, its status line settling as it runs. */
    setChild(block, d) {
        if (!block) return null;
        const childId = d.child_id || '';
        if (!childId) return null;
        const existing = block.childRows[childId];
        if (existing) {
            // Refresh the status line in place; a settled frame must not add a second row.
            existing.dataset.childStatus = d.status || existing.dataset.childStatus;
            const status = existing.querySelector('.cycle-child-status');
            if (status) status.textContent = _runtimeEntityLabel(d.status, d.stop_reason || d.detail);
            if (d.output) CycleRows.appendChildOutput(existing, d.output, 'answer');
            return existing;
        }
        const el = this.addRow(block, 'child', {
            step_no: block.counts.turns, at_ts: d.at_ts || (Date.now() / 1000),
            child_id: childId, label: d.label || '', mode: d.mode || 'spawn',
            depth: d.depth, status: d.status || 'running',
            stop_reason: d.stop_reason || '', output: d.output || '',
            turns: d.turns, steps: d.steps, tool_calls: d.tool_calls,
        }, true);
        if (!el) return null;
        block.childRows[childId] = el;
        this.bump(block, 'children');
        return el;
    },

    /** cycle_job: one row per background job. */
    setJob(block, d) {
        if (!block) return null;
        const jobId = d.job_id || '';
        if (!jobId) return null;
        const existing = block.jobRows[jobId];
        if (existing) {
            existing.dataset.jobStatus = d.status || existing.dataset.jobStatus;
            const status = existing.querySelector('.cycle-job-status');
            if (status) status.textContent = _runtimeEntityLabel(d.status, d.detail || '');
            const meta = existing.querySelector('.cycle-runtime-meta');
            if (meta && d.progress) meta.textContent = 'progress: ' + d.progress;
            return existing;
        }
        const el = this.addRow(block, 'job', {
            step_no: block.counts.turns, at_ts: d.at_ts || (Date.now() / 1000),
            job_id: jobId, label: d.label || '', kind: d.kind || 'job',
            status: d.status || 'running', detail: d.detail || '',
            progress: d.progress || '', output: d.output || '',
            spill_path: d.spill_path || '',
        }, true);
        if (!el) return null;
        block.jobRows[jobId] = el;
        this.bump(block, 'jobs');
        return el;
    },

    /** cycle_child_output: one streamed chunk of a delegated child, painted in THAT
     *  child's row. Observers only — the parent gets the report through the settlement
     *  notice, so painting here never steals anything. */
    appendChildOutput(block, d) {
        if (!block || !d.child_id) return null;
        let el = block.childRows[d.child_id];
        if (!el) el = this.setChild(block, d);
        if (el && d.text) CycleRows.appendChildOutput(el, d.text, d.channel || 'answer');
        return el;
    },

    /** cycle_job_output: live output appended to that job's row. Observers only — the
     *  model reads the ring through job_output, so painting here never steals its bytes. */
    appendJobOutput(block, d) {
        if (!block || !d.job_id) return null;
        let el = block.jobRows[d.job_id];
        if (!el) el = this.setJob(block, d);
        if (el && d.text) CycleRows.appendJobOutput(el, d.text, d.channel || 'stdout');
        return el;
    },

    /** cycle_question_pending / cycle_question: the durable row for a question card. The
     *  card itself is transient (like a consent card) — this row is the fact. */
    setQuestion(block, d) {
        if (!block) return null;
        const requestId = d.request_id || '';
        if (!requestId) return null;
        const existing = block.questionRows[requestId];
        if (existing) {
            if (d.decision) {
                existing.dataset.decision = d.decision;
                existing.dataset.pending = '0';
                const line = existing.querySelector('.cycle-consent-line');
                if (line) line.textContent = _questionLabel(d.decision, false);
            }
            return existing;
        }
        const el = this.addRow(block, 'question', {
            step_no: block.counts.turns, at_ts: d.at_ts || (Date.now() / 1000),
            request_id: requestId, question: d.question || '',
            options: d.options || [], decision: d.decision || '',
            pending: d.pending !== false && !d.decision,
        }, true);
        if (!el) return null;
        block.questionRows[requestId] = el;
        this.bump(block, 'consents');
        return el;
    },

    /** One AI text block of a step: the body row AND the visible AI strip. The strip gets
     *  the step number, so each reasoning step's answer is its own block under the others.
     *  The new message overwrites the previous one of this loop (2026-11-09 rule), so only
     *  this block needs re-marking - no walk over the other loops. */
    addSpeech(block, d) {
        this.addRow(block, 'speech', d);
        CycleShell.appendStream(block, d.kind, d.text, d.step_no, !!d.final);
        this.refreshAiVisibilityBlock(block);
    },

    /** Live streamed chunk (content|thinking) - visible strip only; the row comes at settle. */
    addStreamChunk(block, kind, text) {
        CycleShell.appendStream(block, kind, text, null);
        this.refreshAiVisibilityBlock(block);
    },

    /**
     * Drop the last-tool strip. Called when the final answer starts being delivered
     * (cycle_final, websocket.js) and by settle() for a turn that ends without that frame
     * (done / error / stop / cycle_end), so the strip never outlives the answer.
     * The user's rule (2026-11-08, revised): visible while a tool runs, gone once the
     * final AI response is delivered.
     */
    clearLastTool(block) { CycleShell.clearLastTool(block); },

    // ===== clearing =====

    clear(convId) {
        for (const b of this.blocks[convId] || []) {
            this.settle(b, b.finalText || '', true);
        }
        delete this.blocks[convId];
        for (const tid of Object.keys(this.owners)) {
            if (this.owners[tid] === convId) delete this.owners[tid];
        }
        for (const mount of Array.from(document.querySelectorAll('.cycle-card-mount'))) {
            if (mount.dataset.ownerConvId === convId) mount.remove();
        }
    },

    clearAll() {
        for (const t of this.timers) clearInterval(t);
        this.timers = [];
        this.blocks = {};
        this.owners = {};
    },
};

function _fmtSec(total) {
    if (!isFinite(total) || total < 0) return '0:00';
    const s = Math.floor(total % 60);
    const m = Math.floor((total / 60) % 60);
    const h = Math.floor(total / 3600);
    const mm = (h > 0 ? String(m).padStart(2, '0') : String(m));
    return (h > 0 ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}

function _consentLabel(decision, kind) {
    const k = kind || 'python';
    switch (decision) {
        case 'allow': return '\u2705 Allowed \u2014 ' + k;
        case 'allow_category_until_done': return '\u26A1 Allowed until task done \u2014 ' + k;
        case 'deny': return '\u274C Denied \u2014 ' + k;
        case 'explain': return '\uD83D\uDCAC Explained \u2014 ' + k + ' (nothing allowed)';
        case 'unanswered': return '\u26A0 Unanswered \u2014 ' + k + ' (turn ended before a decision)';
        case 'unavailable': return '\u26A0 Could not ask \u2014 ' + k + ' (fail-closed, nothing ran)';
        case 'pending': return '\u23F3 Waiting for your decision \u2014 ' + k;
        default: return '\u2713 ' + k + ': ' + (decision || 'answered');
    }
}

window.CycleBlock = CycleBlock;
window._fmtSec = _fmtSec;
window._consentLabel = _consentLabel;
