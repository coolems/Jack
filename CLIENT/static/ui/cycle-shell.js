/**
 * cycle-shell.js - DOM shell helpers for the loop-history block (2026-11-08).
 *
 * Split from cycle-block.js so each file stays small and debuggable:
 *   cycle-block.js = identity/routing + counters + timers (the logic)
 *   cycle-shell.js = the visible strips (last tool, live consent cards, AI response)
 *                    and the rebuild of a settled turn from its persisted turn_log row.
 *
 * THE IO FRAME CONTRACT the user asked for (2026-11-08, revised twice):
 *   * EVERY tool block AND the LIVE LAST-TOOL strip shows input and output
 *     inside a framed window (border + scrollable body).
 *   * input frame cap = 5 visible lines, output frame cap = 4 lines, scrollable.
 *   * The DOM keeps the full text; only the cap clips it, so nothing is lost.
 *   * NO 'input' / 'output' TITLE BAR (2026-11-08): that line ate a row per frame in every
 *     tool block and in the last-tool strip - the frame's border is the separator now.
 *   * A frame is as small as its information (2026-11-08): `max-height`, so short content
 *     renders a short frame instead of a fixed-size window.
 *   * The LAST-TOOL strip's info line stays ONE line (tool number + name + FILE + status +
 *     timer; no call id, no start time, no extra stats row). The FILE sits next to the tool
 *     name (2026-11-08 user request) - edit_file / write_file no longer print it inside the
 *     input frame, and a content tool's input frame is real lines with real indentation.
 * Status colour is honest: amber while running, green only for a real ok, red for a real
 * failure.
 */

const CycleShell = {
    /** Line budget: .cycle-io-pre has an INTEGER line box of 16px (font 12px, line-height 16px),
     *  so height = lines * 16px shows EXACTLY that many lines. An em multiplier of the unitless
     *  1.35 line-height rounded to 16.5px in Chrome and sliced the last line (measured 2026-11-08). */
    LINE_PX: 16,
    INPUT_LINES: 5,
    OUTPUT_LINES: 4,

    /** Live repaint budget (2026-11-08, user request: "while streaming the text is
     *  unformatted and without colours, and only after the stream ends it becomes the look
     *  I like"). The streamed answer now renders markdown WHILE the turn runs, throttled to
     *  one full parseMarkdown pass per this many ms - the buffer grows every chunk, so a
     *  per-token re-parse of the whole answer would be O(n^2) work for nothing. */
    LIVE_PAINT_MS: 120,

    hideWelcome() {
        const welcome = document.getElementById('welcome');
        if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';
    },

    scrollIfNeeded() {
        if (window.COOLEMS && COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp
            && typeof scrollToBottom === 'function') scrollToBottom();
    },

    /**
     * A framed io window: a scrollable <pre> capped at `lines` visible lines (input 5 /
     * output 4 - the same numbers in cycle-rows.js, so both shapes agree). The <pre> still
     * holds the FULL text, so the scrollbar reveals everything the cap hides. An empty value
     * shows a placeholder instead of a blank frame.
     *
     * TWO REVISIONS THE USER ASKED FOR (2026-11-08):
     *   * NO 'input' / 'output' TITLE BAR - that line was pure chrome and cost a row per
     *     frame here too. The frame's border separates the two windows; the strip's info line
     *     already names the tool, and input is always the first frame, output the second.
     *   * SHRINK TO THE CONTENT - `max-height`, not `height`: the frame is capped at its
     *     budget but never taller than its own text, so a short input or a short output is as
     *     little as that information is.
     */
    pre(text, lines) {
        const box = document.createElement('pre');
        box.className = 'cycle-io-pre';
        box.style.maxHeight = (lines * this.LINE_PX) + 'px';
        box.textContent = text || window.EMPTY_IO;
        box.dataset.empty = text ? '0' : '1';
        const wrap = document.createElement('div');
        wrap.className = 'cycle-io';
        wrap.dataset.lines = String(lines);
        wrap.appendChild(box);
        return wrap;
    },

    // ===== last-tool strip =====

    /**
     * Build the newest-tool strip. The info line is ONE line, as the user asked:
     *   last tool · #N · name · FILE · status · timer
     * No call id, no start time, no step line, no separate stats row - everything that
     * identifies the call fits on that single line. The FILE (user request 2026-11-08) sits
     * next to the tool name here too, like the tool row in the expanded body, so the input
     * frame below carries only the content.
     *
     * The io boxes are FRAMED and CAPPED here, exactly like the expanded body rows:
     * input 5 lines, output 4 lines, scrollable (the user's rule 2026-11-08 - every tool
     * block AND the last-tool info show input/output inside a window). The full text stays
     * in the DOM, so scrolling inside the frame reveals the rest.
     *
     * While the call is still running the state is 'running' (amber), NOT 'failed': the
     * old code passed ok:false for a running tool and printed "\u2716 failed" before the
     * tool had ever finished. data-ok is only written once the result is known, so the
     * red/green CSS can never fire on a guess.
     */
    /** One span of the info line (built as a real element, not innerHTML, so the DOM stays inspectable). */
    _span(cls, text) {
        const s = document.createElement('span');
        s.className = cls;
        s.textContent = text;
        return s;
    },

    buildLastTool(block, d) {
        if (!block) return null;
        const t = d.tool || {};
        block.lastEl.style.display = '';
        const wrap = document.createElement('div');
        wrap.className = 'cycle-last-tool';
        wrap.dataset.callId = t.call_id || '';
        wrap.dataset.state = 'running';
        const head = document.createElement('div');
        head.className = 'cycle-last-head';
        head.appendChild(this._span('cycle-last-label', 'last tool'));
        head.appendChild(this._span('cycle-last-no', '#' + (Number(t.no || block.counts.tools || 0) || 1)));
        head.appendChild(this._span('cycle-last-name', t.name || 'tool'));
        // THE FILE MOVES TO THE INFO LINE (user request 2026-11-08), exactly like the tool
        // row in the expanded body: the name of the tool carries the file it acts on, so the
        // input frame below is pure content.
        if (t.target) head.appendChild(this._span('cycle-last-target', t.target));
        head.appendChild(this._span('cycle-last-status', '\u23F3 running'));
        head.appendChild(this._span('cycle-last-timer', '0.0ms'));
        wrap.appendChild(head);
        wrap.appendChild(this.pre(t.args || '', this.INPUT_LINES));
        const out = this.pre(t.output || '', this.OUTPUT_LINES);
        out.classList.add('cycle-last-out');
        wrap.appendChild(out);
        block.lastEl.innerHTML = '';
        block.lastEl.appendChild(wrap);
        block.lastEl_ = wrap;
        block.lastTool_ = t;
        return wrap;
    },

    /** tool_start: fresh strip + live timer ticking from the call's own start time. */
    showRunningTool(block, data) {
        if (!block) return;
        // A tool is RUNNING now, so the strip must be visible again - the flag only keeps
        // it gone for finished-tool frames after the answer started being delivered.
        block.lastToolClosed = false;
        // The tool number = this call's position in the turn (1-based). It matches the
        // `tools N` on the head line, because addTool() counts each call exactly once.
        const wrap = this.buildLastTool(block, { tool: {
            name: data.tool, args: _asText(data.input), target: data.target || '',
            ok: false, call_id: data.call_id || '',
            step_no: block.counts.turns, no: (block.counts.tools || 0) + 1, started_at: data.started_at,
        } });
        if (!wrap) return;
        CycleBlock.startToolTimer(block, data.call_id || '', data.started_at);
        if (typeof SetupProgressUI !== 'undefined' && data.tool === 'generate_image') {
            SetupProgressUI.start('generate_tool', this.setupMount(block));
        }
        this.scrollIfNeeded();
    },

    /**
     * tool_end / cycle_tool: freeze the timer, write the REAL ok flag, fill the output box.
     * The info line stays ONE line - only the status word and the timer change.
     *
     * A finished tool with NO strip yet (silent start, background replay, a frame that
     * arrived while the view was away) still builds the strip, so the live last-tool block
     * never ends up empty - it always shows the newest tool. Once the strip has been closed
     * (the final answer is being delivered - lastToolClosed) no tool frame re-opens it.
     *
     * `data-ok` is set on BOTH the strip container (.cycle-last) and the tool wrap, so the
     * CSS red rule (.cycle-last[data-ok="0"] ...) actually matches. Without it a failed
     * tool stayed green while its label said "\u2716 failed".
     */
    finishLastTool(block, d) {
        if (!block) return;
        if (block.lastToolClosed) return;   // the answer is being delivered - strip stays gone
        if (!block.lastEl_) {
            block.toolNo = (block.toolNo || 0) + 1;
            const fresh = this.buildLastTool(block, { tool: Object.assign(
                { no: block.toolNo }, d.tool || {}, { args: d.args || (d.tool || {}).args || '' }) });
            if (!fresh) return;
        }
        CycleBlock.stopToolTimer(block);
        const t = Object.assign({}, block.lastTool_ || {}, d.tool || {});
        const ok = d.ok !== undefined ? !!d.ok : !!(t.ok);
        block.lastEl.dataset.ok = ok ? '1' : '0';
        block.lastEl_.dataset.ok = ok ? '1' : '0';
        block.lastEl_.dataset.state = ok ? 'ok' : 'failed';
        const status = block.lastEl_.querySelector('.cycle-last-status');
        if (status) status.textContent = ok ? '\u2713 done' : '\u2716 failed';
        const timer = block.lastEl_.querySelector('.cycle-last-timer');
        if (timer) timer.textContent = _fmtMs(t.duration_ms);
        const outs = block.lastEl_.querySelectorAll('.cycle-io-pre');
        if (outs.length > 1) {
            const out = outs[outs.length - 1];
            out.textContent = t.output || window.EMPTY_IO;
            out.dataset.empty = t.output ? '0' : '1';
        }
        block.lastTool_ = t;
        if (typeof SetupProgressUI !== 'undefined' && (t.name === 'generate_image')) {
            SetupProgressUI.settle('generate_tool');
        }
    },

    /**
     * Drop the strip completely: the final answer is being delivered (cycle_final) or the
     * turn settled without that frame (done / error / stop / cycle_end), and also when a
     * workspace's view is destroyed.
     *
     * THE RULE (2026-11-08, revised by the user): the strip is a LIVE work surface - it
     * appears with the first tool_start of the turn and must be GONE once the final AI
     * response starts being delivered. Nothing is lost: every tool call is still a row of
     * the expanded body, and a rebuilt (settled) turn therefore shows no strip at all.
     */
    clearLastTool(block) {
        if (!block) return;
        if (block.lastToolClosed) return;   // already gone - answer chunks must not re-clear it
        CycleBlock.stopToolTimer(block);
        block.lastToolClosed = true;   // the answer is delivered/settled - no tool frame re-opens it
        block.lastEl.style.display = 'none';
        block.lastEl.innerHTML = '';
        delete block.lastEl.dataset.ok;
        block.lastEl_ = null;
        block.lastTool_ = null;
        block.lastCallId = '';
        if (typeof SetupProgressUI !== 'undefined') SetupProgressUI.stop('generate_tool', false);
    },

    /** Mount point for a tool's live setup/progress card (generate_image downloads). */
    setupMount(block) {
        if (!block) return null;
        let slot = block.lastEl.querySelector('.cycle-setup-slot');
        if (!slot) {
            slot = document.createElement('div');
            slot.className = 'cycle-setup-slot';
            block.lastEl.appendChild(slot);
        }
        return slot;
    },

    /** A loop-level fact with no owning block (provider error / recovery) - a chat-level notice. */
    systemNotice(text, level) {
        const container = document.getElementById('chatContainer');
        if (!container || !text) return null;
        this.hideWelcome();
        const d = document.createElement('div');
        d.className = 'cycle-system' + (level === 'error' ? ' error' : '');
        d.dataset.ownerConvId = (window.COOLEMS && COOLEMS.currentConversation) || '';
        d.textContent = text;
        container.appendChild(d);
        this.scrollIfNeeded();
        return d;
    },

    // ===== transient consent card =====

    /** Mount an interactive consent card in the block's visible strip (never on the chat). */
    mountCard(block, data) {
        if (!block) return null;
        const mount = document.createElement('div');
        mount.className = 'cycle-card-mount';
        mount.dataset.requestId = data.request_id || '';
        mount.dataset.ownerConvId = block.convId;
        mount.dataset.ownerTurnId = block.turnId;
        block.liveEl.appendChild(mount);
        if (typeof ApprovalDialog !== 'undefined') ApprovalDialog.appendCard(data, mount);
        this.scrollIfNeeded();
        return mount;
    },

    /**
     * (2026-12 ask_user_question) Re-render cards the AI asked that are STILL unanswered.
     *
     * A question card is transient like a consent card, but an unanswered question is a
     * live fact: the tool call is waiting on it (blocking mode) or the timed wait already
     * returned PENDING and the answer still arrives later. A reload must therefore show
     * the card again, or the user could never answer a question they asked before the
     * page reloaded. The source is GET /api/runtime/{conv} -> pending_questions, which is
     * the channel's own pending store (dropped the moment an answer arrives), so anything
     * listed here is genuinely still open.
     *
     * Mount rule mirrors the live turn: the newest block of THIS conversation owns the
     * card (answerable while collapsed); with no block on screen it renders in the chat
     * stream, exactly like the no-owning-block fallback in websocket.js.
     */
    restoreOpenCards(convId, pending) {
        if (!convId || !Array.isArray(pending) || !pending.length) return 0;
        const blocks = (window.CycleBlock && CycleBlock.blocks[convId]) || [];
        const block = blocks.length ? blocks[blocks.length - 1] : null;
        let mounted = 0;
        for (const q of pending) {
            const requestId = String(q.request_id || '');
            if (!requestId) continue;
            const data = {
                type: 'exec_approval_request',
                conv_id: convId,
                request_id: requestId,
                kind: 'question',
                // The card body parses this JSON payload (ApprovalDialog._questionCardBody).
                code: JSON.stringify({
                    question: String(q.question || ''),
                    options: Array.isArray(q.options) ? q.options : [],
                    context: String(q.context || ''),
                }),
                description: String(q.context || ''),
                // A re-rendered card uses the budget live NOW (Settings > Runtime), so a
                // reload cannot resurrect it on a stale timer. 0 = no timer.
                dismiss_sec: (typeof COOLEMS !== 'undefined' && COOLEMS.runtimeSettings)
                    ? Number(COOLEMS.runtimeSettings.questionCardSeconds || 0) : 0,
            };
            if (block && !block.settled) {
                const mount = block.liveEl ? block.liveEl.querySelector(
                    '.cycle-card-mount[data-request-id="' + requestId + '"]') : null;
                if (mount) continue;                 // the replayed frame already mounted it
                this.mountCard(block, data);
            } else {
                const container = document.getElementById('chatContainer');
                if (!container) continue;
                if (container.querySelector('.exec-approval-card[data-request-id="' + requestId + '"]')) continue;
                if (typeof ApprovalDialog !== 'undefined') ApprovalDialog.appendCard(data);
            }
            mounted++;
        }
        if (mounted) this.scrollIfNeeded();
        return mounted;
    },

    /**
     * Remove the interactive card once answered; the block row keeps the decision.
     * (2026-11-08 user rule) A dialog must disappear after ANY click OR after the
     * auto-approve timer fired - so this also removes a card that was rendered as a plain
     * chat bubble (the no-owning-block fallback), not only a block-mounted one.
     */
    removeCard(requestId) {
        if (!requestId) return;
        const sel = '.exec-approval-card[data-request-id="' + CSS.escape(requestId) + '"]';
        for (const card of Array.from(document.querySelectorAll(sel))) {
            card.dataset.answered = '1';
            card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
            const mount = card.closest('.cycle-card-mount');
            const target = mount || card.closest('.message') || card;
            // Stop the auto-approve countdown (owned by the message wrapper inside the mount)
            // so a backend decision never leaves a timer ticking on a removed card.
            for (const el of [target, card, card.closest('.message')]) {
                if (el && el._autoTimer) { clearInterval(el._autoTimer); el._autoTimer = null; }
            }
            target.remove();
        }
        // A mount whose card is gone (answered before the frame, or an empty mount) must not
        // leave an empty strip line behind.
        for (const mount of Array.from(document.querySelectorAll('.cycle-card-mount'))) {
            if (mount.dataset.requestId === requestId && !mount.querySelector('.exec-approval-card')) {
                mount.remove();
            }
        }
    },

    // ===== AI response strip =====

    /**
     * ONE SEGMENT PER REASONING STEP (user request 2026-11-08: "in turn 5 the AI told
     * something1 and in turn 6 something2, but in the AI response block I see them one
     * after another instead of one under another").
     *
     * The strip used to be ONE element for the whole turn (aiSegIdx never moved), so the
     * text of step 5 and step 6 was concatenated into a single markdown document - the two
     * answers ran together on one line and were hard to read. Now each reasoning step owns
     * its own answer/thinking element, and .cycle-ai (a flex column) stacks them one under
     * another with a divider.
     *
     * THE DUPLICATION RULE still holds: a step's LIVE chunks and that step's settled
     * speech row must land in the SAME element, never in two boxes. A live chunk therefore
     * belongs to `counts.turns + 1` - the step that is streaming RIGHT NOW (cycle_step for
     * step N only arrives after that step's chunks, so counts.turns is N-1 while step N
     * streams). A settled row carries its own step_no, which resolves to the same element.
     * A steering message is NOT a step boundary, so it never opens a new segment.
     */
    _seg(block, stepNo) {
        const idx = Number(stepNo || 0);
        let seg = block.aiSeg[idx];
        if (!seg) {
            seg = { wrapEl: null, labelEl: null, answerEl: null, thinkingEl: null,
                    hasAnswer: false, hasThinking: false };
            block.aiSeg[idx] = seg;
        }
        return seg;
    },

    /**
     * THE STREAMING FIX (2026-11-08, user report "AI response is not streamed").
     *
     * The old `_merge()` used one rule for BOTH live chunks and settled speech rows:
     *   if (have.includes(text)) return false;
     * For a live chunk that is a plain word ("the", "of", "and") that already appears
     * somewhere in the accumulated text, that test is TRUE - so the chunk was DROPPED and
     * the answer never grew on screen. It looked like "not streamed".
     *
     * Two separate paths now, each honest about what the frame is:
     *   _appendLive()  - a streamed chunk: APPEND it. Only an exact repeat of the chunk we
     *                    just appended is skipped (that is a socket replay, not new text).
     *   _mergeRow()    - a settled `cycle_speech` row carrying the FULL text of the segment
     *                    the stream already painted: merge without printing it twice.
     */
    _appendLive(el, text) {
        const have = el.dataset.raw || '';
        if (text === el.dataset.lastChunk) return false;   // duplicate replay of the same chunk
        el.dataset.lastChunk = text;
        el.dataset.raw = have + text;
        return true;
    },

    _mergeRow(el, text) {
        const have = el.dataset.raw || '';
        if (have === text) return false;                   // the stream already painted exactly this
        if (text.startsWith(have)) el.dataset.raw = text;  // the row IS the whole streamed text
        else if (have.includes(text)) return false;        // row is a subset already on screen
        else el.dataset.raw = have + text;
        return true;
    },

    /**
     * Mount a segment's element in STEP ORDER (ascending step_no), so answers read top-down
     * exactly as the loop produced them. Normally an element is created while its own step
     * streams and the arrival order is already correct; this guard covers the case where a
     * settled speech row of an older step arrives AFTER a newer step already opened its box
     * (a dropped replay chunk, or a late flush) - the older answer must not land below it.
     */
    _mount(block, el, stepNo) {
        const sn = Number(stepNo || 0);
        const later = Array.from(block.aiEl.children)
            .filter(c => Number(c.dataset.stepNo || 0) > sn);
        if (later.length) block.aiEl.insertBefore(el, later[0]);
        else block.aiEl.appendChild(el);
        return el;
    },

    /** The per-step wrapper: ONE .cycle-ai-seg per reasoning step, carrying its own turn
     *  label, so answers read one UNDER another instead of running together. Built on the
     *  first text of that step, mounted in step order, then reused by every later frame of
     *  the same step (live chunks AND the settled speech row) - never a second box. */
    _segWrap(block, sn, kind) {
        const seg = this._seg(block, sn);
        if (!seg.wrapEl) {
            const wrap = document.createElement('div');
            wrap.className = 'cycle-ai-seg';
            wrap.dataset.stepNo = String(sn);
            const lab = document.createElement('div');
            lab.className = 'cycle-ai-seg-label';
            wrap.appendChild(lab);
            seg.labelEl = lab;
            seg.wrapEl = wrap;
            this._mount(block, wrap, sn);
        }
        if (kind === 'answer') seg.hasAnswer = true;
        if (kind === 'thinking') seg.hasThinking = true;
        if (seg.labelEl) {
            seg.labelEl.textContent = 'turn ' + sn + ' \u00b7 ' +
                (seg.hasAnswer && seg.hasThinking ? 'thinking + AI'
                    : (seg.hasAnswer ? 'AI' : 'thinking'));
        }
        return seg;
    },

    /**
     * Paint a SETTLED segment (a speech row, the end of the turn, or a DB rebuild): markdown,
     * with the `.live` class dropped. The live streaming path is _paintLive() below.
     *
     * THE MARKER SCRUB (2026-11-08): a protocol marker can be SPLIT across two streamed
     * chunks ('... Agentic AI' + ': TASK DONE'), so per-chunk scrubbing alone lets the first
     * half through. The accumulated text is scrubbed here, at paint time, where the whole
     * block is assembled. Cheap guard: the regexes only run when a sentinel is present.
     */
    _paint(el, markdown) {
        el.dataset.raw = el.dataset.raw || '';
        if (typeof scrubAiText === 'function' && _hasAiNoise(el.dataset.raw)) {
            el.dataset.raw = scrubAiText(el.dataset.raw);
        }
        if (markdown && typeof parseMarkdown === 'function') {
            el.classList.remove('live');
            el.innerHTML = parseMarkdown(el.dataset.raw, !!this._openOf(el));
        } else {
            el.classList.add('live');
            el.textContent = el.dataset.raw;
        }
    },

    /** Whether this segment's block is expanded (the markdown renderer honours it). */
    _openOf(el) {
        const node = el.closest('.cycle-block');
        return !!(node && node.classList.contains('expanded'));
    },

    /**
     * THE LIVE-FORMATTING FIX (2026-11-08, user report: "while streaming, the text is
     * unformatted and without colours; only after the stream ends it becomes the look I like").
     *
     * The old live path painted `textContent` (plain text) and left markdown for settle(), so
     * bold/em, code frames, inline code and links appeared only once the turn finished.
     * A live chunk now renders the SAME markdown as the settled answer, but throttled: one
     * full parseMarkdown pass per LIVE_PAINT_MS. The buffer grows with every chunk, so
     * re-parsing on every token would re-read the whole answer each time (O(n^2) DOM work)
     * for no visual gain - a repaint every ~120ms is already faster than the eye reads.
     * A trailing timer guarantees the LAST chunks land even when the stream stops mid-budget.
     * parseMarkdown reads COOLEMS.isStreaming (true while a turn runs), so an UNCLOSED ```
     * fence renders as a code frame while the code block is still being streamed - the same
     * behavior the plain (non-block) answer bubble already had.
     */
    _paintLive(el) {
        el.dataset.raw = el.dataset.raw || '';
        if (typeof scrubAiText === 'function' && _hasAiNoise(el.dataset.raw)) {
            el.dataset.raw = scrubAiText(el.dataset.raw);
        }
        if (typeof parseMarkdown !== 'function') {
            // No markdown renderer loaded: plain text still needs newlines preserved.
            el.classList.add('live');
            el.textContent = el.dataset.raw;
            return;
        }
        // Markdown paints <p> + <br>, so the live pre-wrap class must go - keeping it would
        // render literal newlines around a streamed code fence as extra blank lines.
        el.classList.remove('live');
        const now = Date.now();
        if (!el._lastPaint || now - el._lastPaint >= this.LIVE_PAINT_MS) {
            if (el._paintTimer) { clearTimeout(el._paintTimer); el._paintTimer = null; }
            el._lastPaint = now;
            el.innerHTML = parseMarkdown(el.dataset.raw, true);
            return;
        }
        if (!el._paintTimer) {
            el._paintTimer = setTimeout(() => {
                el._paintTimer = null;
                el._lastPaint = Date.now();
                el.innerHTML = parseMarkdown(el.dataset.raw, true);
            }, this.LIVE_PAINT_MS - (now - el._lastPaint));
        }
    },

    /**
     * Live chunk (content|thinking) or one settled speech row, appended to the AI strip of
     * THIS loop. The strip is ALWAYS built (collapsed or expanded) and grows with the text -
     * no max-height on the answer, so the whole message is readable. WHICH strip the user
     * sees is decided by CycleBlock.refreshAiVisibilityBlock() (2026-11-09): a loop shows a
     * strip only once it has a real message, and that strip collapses to its NEWEST message
     * (showLastSegment), so the chat is one message per loop, not the whole run.
     *
     * `stepNo === null` means a LIVE streamed chunk (websocket content/thinking frame);
     * anything else is a settled `cycle_speech` row (or a DB rebuild row). Live chunks append
     * and paint markdown (throttled, _paintLive); settled rows merge and paint markdown.
     * `final` marks the delivered answer of the loop (the speech row carries it) - it is
     * emphasised, the earlier steps' text stays normal weight.
     */
    appendStream(block, kind, text, stepNo, final) {
        if (!block || !text) return;
        // Protocol lines never enter the AI strip (see CycleRows/scrubAiText): a live chunk
        // that is only marker markup paints NOTHING, so the answer grows with real words only.
        const visible = (typeof scrubAiText === 'function') ? scrubAiText(text) : text;
        if (!visible) return;
        const empty = block.aiEl.querySelector('.cycle-ai-empty');
        if (empty) empty.remove();
        // The strip now has text, so its Copy chip becomes visible (CSS hides it while
        // data-empty="1", i.e. before the first answer chunk of this turn).
        block.aiEl.dataset.empty = '0';
        const live = stepNo == null;
        // WHICH STEP THIS TEXT BELONGS TO. cycle_step for step N only arrives AFTER that
        // step's chunks (settle_step runs when the provider call is done), so while step N
        // streams counts.turns is still N-1 - a live chunk belongs to N, the step streaming
        // right now. That is what makes the settled speech row of step N merge into the SAME
        // element the stream painted, instead of opening a second box.
        const sn = live ? (Number(block.counts.turns || 0) + 1) : Number(stepNo || 0);
        const seg = this._segWrap(block, sn, kind);

        if (kind === 'thinking') {
            if (!seg.thinkingEl) {
                seg.thinkingEl = document.createElement('div');
                seg.thinkingEl.className = 'cycle-ai-thinking';
                seg.thinkingEl.dataset.stepNo = String(sn);
                seg.thinkingEl.dataset.raw = '';
                seg.wrapEl.appendChild(seg.thinkingEl);
            }
            const changed = live ? this._appendLive(seg.thinkingEl, visible)
                                 : this._mergeRow(seg.thinkingEl, visible);
            if (!changed) return;
            // Thinking has no markdown repaint, so scrub the accumulated text directly.
            seg.thinkingEl.textContent = (typeof scrubAiText === 'function' && _hasAiNoise(seg.thinkingEl.dataset.raw))
                ? scrubAiText(seg.thinkingEl.dataset.raw) : seg.thinkingEl.dataset.raw;
            seg.thinkingEl.scrollTop = seg.thinkingEl.scrollHeight;
            if (live) this.scrollIfNeeded();
            return;
        }
        if (!seg.answerEl) {
            seg.answerEl = document.createElement('div');
            seg.answerEl.className = 'cycle-ai-answer';
            seg.answerEl.dataset.stepNo = String(sn);
            seg.answerEl.dataset.raw = '';
            seg.wrapEl.appendChild(seg.answerEl);
        }
        // The delivered answer of the loop is emphasised; the earlier steps' text stays
        // normal weight, so the strip reads as a step-by-step log with one final answer.
        if (final) {
            seg.answerEl.dataset.final = '1';
            seg.wrapEl.dataset.final = '1';
        }
        // The SCRUBBED chunk is what accumulates, so dataset.raw matches the scrubbed
        // speech row the backend flushes for this same text - _mergeRow can then recognise
        // it as already painted instead of appending the answer a second time.
        const changed = live ? this._appendLive(seg.answerEl, visible)
                             : this._mergeRow(seg.answerEl, visible);
        if (!changed) return;
        // A live chunk renders markdown too (throttled) so the answer looks formatted WHILE it
        // streams; a settled row (or a rebuild) paints the final markdown. The strip has NO
        // height cap, so the whole answer stays visible.
        if (live) this._paintLive(seg.answerEl);
        else this._paint(seg.answerEl, true);
        this.scrollIfNeeded();
    },

    /**
     * The NEWEST AI message of THIS loop - the one box the user should see.
     *
     * A reasoning step owns its own .cycle-ai-seg box, so one agentic loop is a sequence of
     * messages, not one long one. "Newest" is read from the strip's DOM ORDER (which _mount()
     * keeps ascending by step), NOT from step_no: a live chunk of step N lands while
     * counts.turns is still N-1, so the box the stream is painting right now is the message
     * currently being delivered. A settled speech row of an older step that arrives late (a
     * replay flush) stays mounted in step order but stays hidden.
     */
    lastSegmentOf(block) {
        if (!block || !block.aiEl) return null;
        let newest = null;
        for (const wrap of Array.from(block.aiEl.children)) {
            if (!wrap.classList || !wrap.classList.contains('cycle-ai-seg')) continue;
            const seg = block.aiSeg ? block.aiSeg[wrap.dataset.stepNo] : null;
            if (!seg) continue;
            const hasText = (seg.answerEl && (seg.answerEl.dataset.raw || '').trim())
                || (seg.thinkingEl && (seg.thinkingEl.dataset.raw || '').trim());
            if (hasText) newest = seg;            // last box with real text wins
        }
        return newest;
    },

    /**
     * ONE AI MESSAGE PER AGENTIC LOOP (user report 2026-11-09: "i asked you to say something
     * in each turn and i see all turns one below another ... i wanted to see this last ai
     * message for each agentic loop").
     *
     * Inside a loop's strip only the newest message stays shown; every older message of that
     * same loop gets data-visible="0" (display:none), so message N OVERWRITES message N-1 on
     * screen instead of stacking under it.
     *
     * Hidden, never deleted: each message's text stays in dataset.raw (Copy still copies the
     * whole turn), as a speech row of the expanded body, and in the turn_log payload.
     *
     * `data-empty` is re-derived here, not only on the first chunk: once a strip has text, an
     * empty placeholder must not sit next to the visible message, and a strip whose segments
     * all lost their text must show the placeholder again instead of an empty box.
     */
    showLastSegment(block) {
        if (!block) return null;
        const newest = this.lastSegmentOf(block);
        for (const seg of Object.values(block.aiSeg || {})) {
            if (seg && seg.wrapEl) seg.wrapEl.dataset.visible = (seg === newest) ? '1' : '0';
        }
        block.aiEl.dataset.empty = newest ? '0' : '1';
        if (!newest) {
            if (!block.aiEl.querySelector('.cycle-ai-empty')) {
                const ph = document.createElement('div');
                ph.className = 'cycle-ai-empty';
                ph.textContent = 'Waiting for the AI\u2026';
                block.aiEl.appendChild(ph);
            }
        }
        return newest;
    },

    /**
     * The turn ended. The streamed answers of this block get their markdown render, so the
     * settled text reads formatted instead of staying in the plain-text live state.
     */
    settleStream(block) {
        if (!block) return;
        for (const seg of Object.values(block.aiSeg || {})) {
            // Drop any pending throttled repaint: the settled pass below is the final one,
            // and a trailing timer would re-paint with isStreaming=true after the turn ended.
            if (seg.answerEl && seg.answerEl._paintTimer) {
                clearTimeout(seg.answerEl._paintTimer);
                seg.answerEl._paintTimer = null;
            }
            if (seg.answerEl) this._paint(seg.answerEl, true);
        }
    },

    // ===== rebuild a settled turn from the DB =====

    /**
     * Render one persisted turn_log row as a collapsed block. Same identity rules as
     * CycleBlock.open(): a turn_id owned by another conversation is REFUSED, so a reload
     * of workspace A can never paint workspace B's history.
     * `before` = the answer message node to insert before (keeps history order).
     */
    rebuild(convId, row, before) {
        if (!convId || !row || !row.payload) return null;
        const turnId = row.payload.turn_id || row.id;
        if (!turnId) return null;
        if (CycleBlock.get(convId, turnId)) return null;         // already rendered live
        if (CycleBlock.ownerOf(turnId) && CycleBlock.ownerOf(turnId) !== convId) return null;
        CycleBlock.owners[turnId] = convId;

        const container = document.getElementById('chatContainer');
        if (!container) return null;

        const p = row.payload;
        const block = CycleBlock._makeBlock(convId, turnId,
            Number(p.turn_started_at ? p.turn_started_at * 1000 : Date.now()),
            Number(p.last_turn_sec || 0));
        block.open = false;
        block.settled = true;
        block.counts = Object.assign(block.counts, p.counts || {});
        block.generated = Number(p.turn_generated || 0);
        block.speed = Number(p.speed_tps || 0);
        block.finalText = (p.final && p.final.text) || '';

        if (before && before.parentNode === container) container.insertBefore(block.node, before);
        else container.appendChild(block.node);
        (CycleBlock.blocks[convId] = CycleBlock.blocks[convId] || []).push(block);
        block.node.classList.add('settled');
        if (p.final && p.final.stopped) block.node.classList.add('stopped');

        // Rows in real time order. A steering message IS one of them (2026-11-08 user
        // request): it is loop work, so a reload shows it as a row of this block at the
        // moment it was typed - never as a chat-level user bubble.
        const rows = [];
        (p.steps || []).forEach(s => rows.push({ kind: 'step', d: { step_no: s.step_no, step: s, at_ts: s.at_ts } }));
        (p.tools || []).forEach(t => rows.push({ kind: 'tool', d: { step_no: t.step_no, tool: t, at_ts: t.at_ts } }));
        (p.steers || []).forEach(s => rows.push({ kind: 'steer', d: {
            step_no: s.step_no, steer_id: s.steer_id || '', text: s.text,
            state: s.applied ? 'applied' : 'received', reason: s.reason || '', at_ts: s.at_ts,
        } }));
        (p.consents || []).forEach(c => rows.push({ kind: 'consent', d: { step_no: c.step_no, kind: c.kind, decision: c.decision, payload: c.payload, at_ts: c.at_ts } }));
        (p.grants || []).forEach(g => rows.push({ kind: 'grant', d: { step_no: g.step_no, kind: g.kind, at_ts: g.at_ts } }));
        (p.feedback || []).forEach(f => rows.push({ kind: 'notice', d: { step_no: f.step_no, text: f.text, at_ts: f.at_ts } }));
        // (2026-12 runtime) runtime notices + delegated children + background jobs, rebuilt
        // from the persisted turn row so a reload shows exactly what the live turn showed.
        (p.notices || []).forEach(n => rows.push({ kind: 'runtime', d: {
            step_no: n.step_no, notice_id: n.notice_id || '', source: n.source || 'runtime',
            text: n.text, state: 'applied', at_ts: n.at_ts,
        } }));
        (p.children || []).forEach(c => rows.push({ kind: 'child', d: {
            step_no: c.step_no, child_id: c.child_id, label: c.label, mode: c.mode,
            depth: c.depth, status: c.status, stop_reason: c.stop_reason,
            output: c.output, turns: c.turns, steps: c.steps, tool_calls: c.tool_calls,
            at_ts: c.at_ts,
        } }));
        (p.jobs || []).forEach(j => rows.push({ kind: 'job', d: {
            step_no: j.step_no, job_id: j.job_id, label: j.label, kind: j.kind,
            status: j.status, detail: j.detail, progress: j.progress,
            output: j.output, spill_path: j.spill_path, at_ts: j.at_ts,
        } }));
        (p.questions || []).forEach(q => rows.push({ kind: 'question', d: {
            step_no: q.step_no, request_id: q.request_id, question: q.question,
            options: q.options || [], decision: q.decision, pending: false, at_ts: q.at_ts,
        } }));
        // (2026-12 images) generated-image artifacts of this turn. The persisted row carries
        // FILE IDENTITY only - CycleRows.image() renders it over HTTP from rel_path, so a
        // reload shows the same image the live 'image_generated' frame painted.
        (p.images || []).forEach(im => rows.push({ kind: 'image', d: {
            step_no: im.step_no, call_id: im.call_id || '', file_path: im.file_path || '',
            rel_path: im.rel_path || '', filename: im.filename || '',
            width: im.width, height: im.height, pipeline: im.pipeline || '',
            model_id: im.model_id || '', dtype: im.dtype || '', setup: im.setup,
            at_ts: im.at_ts,
        } }));
        (p.speech || []).forEach(s => rows.push({ kind: 'speech', d: { step_no: s.step_no, kind: s.kind, text: s.text, final: s.final, at_ts: s.at_ts } }));
        rows.sort((a, b) => Number(a.d.at_ts || 0) - Number(b.d.at_ts || 0));

        for (const r of rows) {
            const el = document.createElement('div');
            el.className = 'cycle-row cycle-row-' + r.kind;
            el.dataset.ownerConvId = convId;
            el.dataset.ownerTurnId = turnId;
            el.dataset.kind = r.kind;
            el.dataset.stepNo = String(r.d.step_no || 0);
            block.bodyEl.appendChild(el);
            CycleRows.render(r.kind, el, r.d);
            block.rows.push({ kind: r.kind, step_no: r.d.step_no || 0, at_ts: Number(r.d.at_ts || 0), el });
            // The AI strip is rebuilt from the same speech rows (there are no live chunks
            // here), one segment per reasoning step, mounted in step order.
            if (r.kind === 'speech') this.appendStream(block, r.d.kind, r.d.text, r.d.step_no, !!r.d.final);
        }

        // NO last-tool strip here (user rule 2026-11-08, revised): a rebuilt turn is a
        // SETTLED turn - its answer was already delivered, so the live strip must not come
        // back. The tool rows above carry the same information in the expanded body.
        block.timerEl.textContent = _fmtSec(Number(p.elapsed_sec || 0));
        CycleBlock._renderSummary(block);
        // A rebuilt turn is SETTLED: each loop keeps ONLY its own last message (2026-11-09
        // rule), so a reload of a long run reads: my message, the turns block, that loop's
        // last AI message - for every loop, not the whole history. Rebuilt oldest-first, so
        // the newest loop with a message owns the live strip.
        CycleBlock.refreshAiVisibility(convId);
        return block;
    },
};

/** tool_start/tool_end carry the input as formatted text (app.turn_log.format_args); a
 *  string passes through untouched, anything else is stringified for safety. */
function _asText(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
}

function _fmtMs(ms) {
    const v = Number(ms || 0);
    if (v < 1000) return v.toFixed(1) + 'ms';
    if (v < 60000) return (v / 1000).toFixed(1) + 's';
    return _fmtSec(v / 1000);
}

window.CycleShell = CycleShell;
window._fmtMs = _fmtMs;
