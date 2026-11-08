/**
 * cycle-rows.js - Row renderers for the loop-history block (2026-11-08).
 *
 * One row = one fact of one agentic turn: a reasoning step, a tool call, a consent
 * decision, a loop notice, an AI text block, a generated image, a steering message.
 * Rows live inside the block's expanded body; the interactive consent card does NOT live
 * here - it sits in the block's visible strip so the user can still answer it while the
 * block is collapsed.
 *
 * STEERING IS A ROW OF THE BLOCK (2026-11-08, user request): a message typed while the
 * task runs is NOT a chat message and must never paint as a user bubble - it is a fact of
 * the RUNNING turn, so it lands here, placed at the moment the user typed it (at_ts =
 * received_at), between the tool rows of that step. Its status line settles from the
 * steer_received / steer_applied / steer_rejected frames (one row per steer_id).
 *
 * THE IO FRAME CONTRACT the user asked for (2026-11-08, revised):
 *   every tool block shows input and output inside a framed window:
 *     tool INPUT frame  -> 5 visible lines, scrollable
 *     tool OUTPUT frame -> 4 visible lines, scrollable
 *   The DOM keeps the full (already clipped by TurnLog) text; only the frame is capped, so
 *   nothing is lost - the scrollbar reveals the rest. The same numbers are used by the
 *   always-visible last-tool strip (cycle-shell.js).
 */

/** Shown inside an io frame when there is nothing to show yet (a running tool has no output).
 *  Declared once here (this script loads before cycle-shell.js) and shared via window. */
const EMPTY_IO = '\u2014 empty \u2014';
window.EMPTY_IO = EMPTY_IO;

/**
 * THE AI-SPEECH SCRUB (2026-11-08, user request: "in the AI response block there are lines
 * I do not need there at all").
 *
 * The AI strip must carry ONLY what the AI said to the user. These lines are protocol or
 * bookkeeping and were appearing inside the answer:
 *   'Loop stats: N / M | ...'  - the spliced turn-stats line (already on the block head),
 *   '<tool_call>...'      - llama.cpp streams tool-call markup as content when the
 *                                 model emits it as text; the tool row already shows the call,
 *   '[SYSTEM NOTICE] ...'      - loop plumbing; it belongs to a notice row, not the answer.
 *
 * THE COMPLETION MARKER IS NOT SCRUBBED (restored 2026-11-08, user request: "i want to have
 * this as it was before - i do not like code that hides it"). 'Agentic AI: TASK DONE' is the
 * signal that stopped the loop, so it must stay visible in the answer - it is the proof of why
 * the turn ended. Only markup and notice lines are removed.
 *
 * EXACT-SHAPE matching only, so prose that merely mentions one of these words survives.
 * Mirrors app/turn_log.py `scrub_ai_text()` - the backend scrubs the persisted rows, this
 * scrubs the LIVE stream (chunks arrive before any row exists). Both must stay in sync.
 */
const AI_NOISE_PATTERNS = [
    /^[ \t]*Loop stats:\s*\d+\s*\/\s*\d+[^\n]*$/gm,
    /^[ \t]*<tool_call>[\s\S]*?<\/tool_call>[ \t]*$/gm,
    /^[ \t]*<tool_call>[^\n]*$/gm,
    /^[ \t]*<function[^\n]*>$/gm,
    /^[ \t]*<\/function>[ \t]*$/gm,
    /^[ \t]*\[SYSTEM NOTICE\][^\n]*$/gm,
    /^[ \t]*\[USER MESSAGE WHILE YOU WERE WORKING[^\n]*$/gm,
    /^[ \t]*\[USER STOP REQUEST[^\n]*$/gm,
];

function scrubAiText(text) {
    if (!text) return '';
    let cleaned = String(text);
    for (const re of AI_NOISE_PATTERNS) cleaned = cleaned.replace(re, '');
    if (cleaned === text) return cleaned;
    return cleaned.replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
}
window.scrubAiText = scrubAiText;

/** Cheap sentinel test so the scrub regexes only run on text that could contain noise
 *  (the strip repaints on every chunk - a full regex pass per token would be wasteful).
 *  No marker sentinels here: the marker is never scrubbed, so it must not trigger a pass. */
const AI_NOISE_SENTINELS = ['Loop stats:', '<tool_call', '<function', '[SYSTEM NOTICE]', '[USER MESSAGE WHILE', '[USER STOP REQUEST'];

function _hasAiNoise(text) {
    if (!text) return false;
    for (const s of AI_NOISE_SENTINELS) if (text.includes(s)) return true;
    return false;
}
window._hasAiNoise = _hasAiNoise;

const CycleRows = {
    /** Line budget: .cycle-io-pre has an INTEGER line box of 16px (font 12px, line-height 16px),
     *  so height = lines * 16px shows EXACTLY that many lines. An em multiplier of the unitless
     *  1.35 line-height rounded to 16.5px in Chrome and sliced the last line (measured 2026-11-08). */
    LINE_PX: 16,
    INPUT_LINES: 5,
    OUTPUT_LINES: 4,
    /**
     * One framed io window: title bar + a scrollable <pre> whose HEIGHT is the line budget.
     * `height` (not `max-height`) because .cycle-io-pre is content-box (see 09-cycle-block.css):
     * the inline value is the text area, so 5 lines really show 5 lines and a short value still
     * reads as a fixed window instead of collapsing.
     */
    _pre(text, lines, label) {
        const box = document.createElement('pre');
        box.className = 'cycle-io-pre';
        box.style.height = (lines * this.LINE_PX) + 'px';
        box.textContent = text || EMPTY_IO;
        box.dataset.empty = text ? '0' : '1';
        const wrap = document.createElement('div');
        wrap.className = 'cycle-io';
        wrap.dataset.lines = String(lines);
        const lab = document.createElement('div');
        lab.className = 'cycle-io-label';
        lab.textContent = label;
        wrap.appendChild(lab);
        wrap.appendChild(box);
        return wrap;
    },

    /** Reasoning step: token counters + speed + duration of that provider call. */
    step(el, d) {
        const s = d.step || {};
        const meta = document.createElement('div');
        meta.className = 'cycle-row-meta';
        const parts = [`step ${d.step_no}`];
        if (s.generated_tokens) parts.push(`${s.generated_tokens} tok out`);
        if (s.speed_tps) parts.push(`\u26A1 ${s.speed_tps} tok/s`);
        if (s.duration_sec) parts.push(`${s.duration_sec}s`);
        if (s.prompt_tokens) parts.push(`${(s.prompt_tokens / 1024).toFixed(1)}K sent`);
        meta.textContent = parts.join(' \u00b7 ');
        el.appendChild(meta);
    },

    /** Tool call: name + status + timer, then input (5 lines) and output (4 lines). */
    tool(el, d) {
        const t = d.tool || {};
        el.dataset.ok = t.ok ? '1' : '0';
        el.dataset.callId = t.call_id || '';

        const head = document.createElement('div');
        head.className = 'cycle-tool-head';
        const name = document.createElement('span');
        name.className = 'cycle-tool-name';
        name.textContent = t.name || 'tool';
        const status = document.createElement('span');
        status.className = 'cycle-tool-status';
        status.textContent = t.ok ? '\u2713 ok' : '\u2716 failed';
        const timer = document.createElement('span');
        timer.className = 'cycle-tool-timer';
        timer.dataset.callId = t.call_id || '';
        timer.textContent = t.duration_ms != null ? _fmtMs(t.duration_ms) : '0.0s';
        head.appendChild(name);
        head.appendChild(status);
        head.appendChild(timer);
        el.appendChild(head);

        el.appendChild(this._pre(t.args || '', this.INPUT_LINES, 'input'));
        el.appendChild(this._pre(t.output || '', this.OUTPUT_LINES, 'output'));
    },

    /** Consent decision line (the interactive card itself is transient). */
    consent(el, d) {
        const line = document.createElement('div');
        line.className = 'cycle-consent-line';
        line.textContent = _consentLabel(d.decision || 'pending', d.kind || 'python');
        el.appendChild(line);
        if (d.payload) {
            el.appendChild(this._pre(String(d.payload), 3, 'what was asked'));
        }
    },

    /** A per-kind auto-allow unlock (timer / "allow until task done"). */
    grant(el, d) {
        const line = document.createElement('div');
        line.className = 'cycle-consent-line';
        line.textContent = `\u26A1 Auto-allowed \u2014 ${d.kind || 'python'} (no card shown)`;
        el.appendChild(line);
    },

    /** A loop notice (guardian, interjection resume, truncation resume...). */
    notice(el, d) {
        const text = document.createElement('div');
        text.className = 'cycle-notice-text';
        text.textContent = d.text || '';
        el.appendChild(text);
    },

    /**
     * A steering message the user typed WHILE this turn was running.
     * It is loop work, never a chat-level user message: the row sits inside the block at
     * the moment the user typed it (at_ts = received_at), so it reads as "the user
     * redirected the task here", between the tool rows of that step. The status line
     * settles through CycleBlock.setSteer() (one row per steer_id).
     */
    steer(el, d) {
        el.dataset.steerId = d.steer_id || '';
        el.dataset.steerState = d.state || (d.applied ? 'applied'
            : (d.rejected ? 'rejected' : (d.received ? 'received' : 'sending')));
        const head = document.createElement('div');
        head.className = 'cycle-steer-head';
        const label = document.createElement('span');
        label.className = 'cycle-steer-label';
        label.textContent = '\u21B3 steering';
        const time = document.createElement('span');
        time.className = 'cycle-steer-time';
        // WHEN the user typed it - the moment this entry was added to the loop history.
        time.textContent = _fmtClock(d.at_ts);
        const status = document.createElement('span');
        status.className = 'cycle-steer-status';
        status.textContent = _steerStatusLabel(el.dataset.steerState, d.reason);
        head.appendChild(label);
        head.appendChild(time);
        head.appendChild(status);
        el.appendChild(head);
        const text = document.createElement('div');
        text.className = 'cycle-steer-text';
        text.textContent = d.text || '';
        el.appendChild(text);
    },

    /** One AI text block of one step: thinking (dim) or answer (markdown). */
    speech(el, d) {
        const kind = d.kind === 'thinking' ? 'thinking' : 'answer';
        el.dataset.speechKind = kind;
        const lab = document.createElement('div');
        lab.className = 'cycle-speech-label';
        lab.textContent = kind === 'thinking' ? 'thinking' : 'AI';
        el.appendChild(lab);
        const body = document.createElement('div');
        body.className = 'cycle-speech-text';
        if (kind === 'thinking') body.textContent = d.text || '';
        else if (typeof parseMarkdown === 'function') body.innerHTML = parseMarkdown(d.text || '', false);
        else body.textContent = d.text || '';
        el.appendChild(body);
        if (d.final) el.dataset.finalSpeech = '1';
    },

    /** A generated image (artifact of a tool call). */
    image(el, d) {
        const img = document.createElement('img');
        img.className = 'cycle-image-thumb';
        img.src = 'data:image/png;base64,' + d.image_base64;
        img.alt = d.filename || 'Generated image';
        img.title = 'Click to open file';
        if (d.file_path) {
            img.dataset.filePath = d.file_path;
            img.addEventListener('click', () => window.open(img.dataset.filePath));
        }
        el.appendChild(img);
        const meta = document.createElement('div');
        meta.className = 'cycle-image-meta';
        const bits = [];
        if (d.filename) bits.push(d.filename);
        if (d.width && d.height) bits.push(`${d.width}\u00d7${d.height}`);
        if (typeof _formatSetupLine === 'function') {
            const setup = _formatSetupLine(d);
            if (setup) bits.push(setup);
        }
        meta.textContent = bits.join(' \u00b7 ');
        el.appendChild(meta);
    },

    render(kind, el, d) {
        const fn = this[kind];
        if (!fn) { el.textContent = String(d && d.text ? d.text : ''); return el; }
        try { fn.call(this, el, d || {}); } catch (e) {
            el.textContent = String((d && d.text) || '');
            console.error('[CYCLE-ROWS] render failed for', kind, e);
        }
        return el;
    },
};

function _fmtMs(ms) {
    const v = Number(ms || 0);
    if (v < 1000) return v.toFixed(1) + 'ms';
    if (v < 60000) return (v / 1000).toFixed(1) + 's';
    return _fmtSec(v / 1000);
}

/** Clock time of an event (epoch seconds -> HH:MM:SS, local). A steering row shows WHEN
 *  the user typed it, so the loop history reads as a timeline, not a wall of text. */
function _fmtClock(epochSec) {
    const v = Number(epochSec || 0);
    if (!v) return '';
    const d = new Date(v * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
window._fmtClock = _fmtClock;

/**
 * The steer row's status line. ONE source of truth: CycleBlock.setSteer() paints a LIVE row
 * with it and CycleRows.steer() paints a rebuilt row with it, so a live turn and a reload
 * can never show different wording for the same steering message.
 */
function _steerStatusLabel(state, reason) {
    switch (state) {
        case 'sending': return '\u23F3 sending to the running task\u2026';
        case 'received': return '\u21B3 queued \u2014 applied at the loop\u2019s next step';
        case 'applied': return '\u2713 applied \u2014 the agent is following this now';
        case 'rejected': return '\u26A0 not applied \u2014 ' + (reason || 'the turn ended before it could be applied.');
        default: return '\u2713 ' + (state || 'recorded');
    }
}
window._steerStatusLabel = _steerStatusLabel;

/** Status order for one steering row: a late ack must never downgrade a settled row. */
const _STEER_STATE_RANK = { sending: 0, received: 1, applied: 2, rejected: 2 };
window._STEER_STATE_RANK = _STEER_STATE_RANK;

window.CycleRows = CycleRows;
window._fmtMs = _fmtMs;
