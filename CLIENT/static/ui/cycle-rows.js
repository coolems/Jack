/**
 * cycle-rows.js - Row renderers for the loop-history block (2026-11-08).
 *
 * ONE ROW = ONE LINE (user request 2026-11-08): clicking a collapsed turn block reveals
 * EVERY entry of that turn, but each entry shows only its FIRST row:
 *   turn N · kind · name · time · speed · text (a steer or an AI block)
 * The rest of an entry (tool input/output frames, the full steer/AI/notice text, the
 * consent payload, the image thumb) stays in the DOM inside .cycle-row-detail and is
 * revealed by clicking THAT ROW's own line - the list stays readable, nothing is lost.
 *
 * NO TOKEN COUNTERS IN THE BLOCK (user request 2026-11-08): "N tok out" and "X.XK sent"
 * are gone from the step rows, and "X.XK gen" is gone from the head line. Speed stays -
 * the user asked for it on the row line. The chat-level token bar (websocket.js
 * updateHistoryStatusBar) still carries the counters; only the turn block stopped showing them.
 *
 * STEERING IS A ROW OF THE BLOCK (2026-11-08, user request): a message typed while the
 * task runs is NOT a chat message and must never paint as a user bubble - it is a fact of
 * the RUNNING turn, so it lands here, placed at the moment the user typed it (at_ts =
 * received_at), between the tool rows of that step. Its status line settles from the
 * steer_received / steer_applied / steer_rejected frames (one row per steer_id).
 *
 * THE IO FRAME CONTRACT (2026-11-08, revised twice): every tool entry still carries input and
 * output inside a framed window - caps of 5 / 4 lines, scrollable. The frames live in the
 * row's detail instead of being always open, so the DOM keeps the full (already clipped by
 * TurnLog) text. The same numbers are used by the live last-tool strip (cycle-shell.js).
 *
 * TWO REVISIONS THE USER ASKED FOR (2026-11-08):
 *   * NO 'input' / 'output' TITLE BAR - that line was pure chrome and cost a row per frame,
 *     in every tool block AND in the last-tool strip. The frame's border is what separates
 *     the two windows now; the row line already says which tool they belong to.
 *   * SHRINK TO THE CONTENT - a frame is capped at its line budget, but it is no longer a
 *     FIXED window: with less information the frame is exactly as tall as that information.
 *
 * A THIRD REVISION (2026-11-08): the input frame of a CONTENT tool (edit_file / write_file /
 * python_exec) used to read as "all lines one after another with just a line-breaker printed"
 * - json.dumps escaped every newline of a string value, so a whole file became one wrapped
 *   line with literal \n in it. The backend now sends that payload as REAL lines with its
 *   indentation (app.turn_log.format_args), and the file identity moved OUT of the frame onto
 *   the tool's own line, next to the tool name (t.target -> .cycle-tool-target).
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

/** Preview text for a collapsed row line: one line, whitespace collapsed, hard cap so the
 *  DOM stays light. The FULL text lives in the row's detail, never here. */
function _oneLine(text) {
    const flat = String(text || '').replace(/\s+/g, ' ').trim();
    return flat.length > 240 ? flat.slice(0, 240) + '\u2026' : flat;
}
window._oneLine = _oneLine;

const CycleRows = {
    /** Line budget: .cycle-io-pre has an INTEGER line box of 16px (font 12px, line-height 16px),
     *  so the cap = lines * 16px shows EXACTLY that many lines and never more. An em multiplier
     *  of the unitless 1.35 line-height rounded to 16.5px in Chrome and sliced the last line
     *  (measured 2026-11-08). */
    LINE_PX: 16,
    INPUT_LINES: 5,
    OUTPUT_LINES: 4,

    /**
     * One framed io window: a scrollable <pre> whose HEIGHT CAP is the line budget.
     * `max-height` (not `height`) is the user's 2026-11-08 revision: a frame is capped at
     * its budget but never taller than its own text, so a short input or a short output is
     * exactly as tall as that information instead of a fixed window.
     *
     * NO TITLE BAR for input/output (user request 2026-11-08): the 'input' / 'output' label
     * line was pure chrome and cost a row per frame, in every tool block AND in the last-tool
     * strip. The frame's own border separates the two windows; the row line already names the
     * tool, and the strip always puts input first, output second. `label` stays OPTIONAL - the
     * consent payload frame keeps its 'what was asked' caption, because that frame is not a
     * tool io frame and the caption is the only thing saying what the text is.
     */
    _pre(text, lines, label) {
        const box = document.createElement('pre');
        box.className = 'cycle-io-pre';
        box.style.maxHeight = (lines * this.LINE_PX) + 'px';
        box.textContent = text || EMPTY_IO;
        box.dataset.empty = text ? '0' : '1';
        const wrap = document.createElement('div');
        wrap.className = 'cycle-io';
        wrap.dataset.lines = String(lines);
        if (label) {
            const lab = document.createElement('div');
            lab.className = 'cycle-io-label';
            lab.textContent = label;
            wrap.appendChild(lab);
        }
        wrap.appendChild(box);
        return wrap;
    },

    /** THE FIRST ROW of an entry - the only thing a collapsed row shows.
     *  Order is exactly what the user asked for: turn · kind · name · time · speed · text.
     *  `nameEl` / `extra` / `timeEl` let a row keep its own classes (tool name, steer label,
     *  consent decision) inside the same line shape; `extra` sits after the time, so the
     *  requested order never moves. */
    _line(el, d, o) {
        const opts = o || {};
        const line = document.createElement('div');
        line.className = 'cycle-row-line';
        line.dataset.rowKind = el.dataset.kind || '';

        const turn = document.createElement('span');
        turn.className = 'cycle-row-turn';
        turn.textContent = 'turn ' + (Number(d.step_no || 0) || 0);
        line.appendChild(turn);

        if (opts.kind) {
            const k = document.createElement('span');
            k.className = 'cycle-row-kind';
            k.textContent = opts.kind;
            line.appendChild(k);
        }

        if (opts.nameEl) line.appendChild(opts.nameEl);
        else if (opts.name) {
            const n = document.createElement('span');
            n.className = 'cycle-row-name';
            n.textContent = opts.name;
            line.appendChild(n);
        }

        if (opts.timeEl) line.appendChild(opts.timeEl);
        else {
            const t = document.createElement('span');
            t.className = 'cycle-row-time';
            t.textContent = _fmtClock(d.at_ts);
            line.appendChild(t);
        }

        if (opts.extra) line.appendChild(opts.extra);

        // Only printed when the entry actually has a speed/duration - an empty span would
        // just add a gap in the line.
        if (opts.speed) {
            const speed = document.createElement('span');
            speed.className = 'cycle-row-speed';
            speed.textContent = opts.speed;
            line.appendChild(speed);
        }

        if (opts.text) {
            const txt = document.createElement('span');
            txt.className = 'cycle-row-text';
            txt.textContent = _oneLine(opts.text);
            line.appendChild(txt);
        }

        el.appendChild(line);
        return line;
    },

    /** The hidden part of an entry (io frames, full text, payload, thumb). CSS keeps it
     *  closed until the row's own line is clicked (data-open="1"). */
    _detail(el) {
        const box = document.createElement('div');
        box.className = 'cycle-row-detail';
        el.appendChild(box);
        return box;
    },

    /** Open/close one entry's detail. Independent of the block-level toggle: the head line
     *  opens the turn, a row line opens that entry only. */
    _openRow(el) {
        el.dataset.open = el.dataset.open === '1' ? '0' : '1';
    },

    /** A row with a detail is clickable; a row with none (a step, a grant) is not. */
    _wire(el) {
        const line = el.querySelector('.cycle-row-line');
        if (!line || !el.querySelector('.cycle-row-detail')) return;
        line.classList.add('cycle-row-toggle');
        line.setAttribute('role', 'button');
        line.tabIndex = 0;
        line.addEventListener('click', (ev) => {
            if (ev.target.closest('button, a, img')) return;   // an image thumb opens the file
            this._openRow(el);
        });
        line.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); this._openRow(el); }
        });
    },

    /** Reasoning step: one line - turn · step · time · speed (+ duration). NO token counters. */
    step(el, d) {
        const s = d.step || {};
        const bits = [];
        if (s.speed_tps) bits.push(`\u26A1 ${s.speed_tps} tok/s`);
        if (s.duration_sec) bits.push(`${s.duration_sec}s`);
        this._line(el, d, { kind: 'step', speed: bits.join(' \u00b7 ') });
    },

    /** Tool call: one line - turn · tool · name · FILE · ok/failed · time · duration.
     *  The input (5 lines) and output (4 lines) frames open on the row's own click.
     *
     *  THE FILE MOVES TO THE ROW LINE (user request 2026-11-08): "i want filename moved in
     *  tool line where the name of the tool is". So edit_file / write_file print their file
     *  identity next to the tool name (path folded in: config/config.py) and their input
     *  frame carries ONLY the content, as real lines with the original indentation. */
    tool(el, d) {
        const t = d.tool || {};
        el.dataset.ok = t.ok ? '1' : '0';
        el.dataset.callId = t.call_id || '';

        const name = document.createElement('span');
        name.className = 'cycle-tool-name';
        name.textContent = t.name || 'tool';
        if (t.target) {
            const target = document.createElement('span');
            target.className = 'cycle-tool-target';
            target.textContent = t.target;
            name.appendChild(target);
        }
        const status = document.createElement('span');
        status.className = 'cycle-tool-status';
        status.textContent = t.ok ? '\u2713 ok' : '\u2716 failed';

        this._line(el, d, {
            kind: 'tool', nameEl: name, extra: status,
            speed: t.duration_ms != null ? _fmtMs(t.duration_ms) : '0.0s',
        });

        const detail = this._detail(el);
        detail.appendChild(this._pre(t.args || '', this.INPUT_LINES));
        detail.appendChild(this._pre(t.output || '', this.OUTPUT_LINES));
    },

    /** Consent decision line (the interactive card itself is transient). Payload in the detail. */
    consent(el, d) {
        const line = document.createElement('div');
        line.className = 'cycle-consent-line';
        line.textContent = _consentLabel(d.decision || 'pending', d.kind || 'python');
        this._line(el, d, { kind: 'consent', extra: line });
        if (d.payload) {
            this._detail(el).appendChild(this._pre(String(d.payload), 3, 'what was asked'));
        }
    },

    /** A per-kind auto-allow unlock (timer / "allow until task done"). One line, no detail. */
    grant(el, d) {
        const line = document.createElement('div');
        line.className = 'cycle-consent-line';
        line.textContent = `\u26A1 Auto-allowed \u2014 ${d.kind || 'python'} (no card shown)`;
        this._line(el, d, { kind: 'grant', extra: line });
    },

    /** A loop notice: text preview on the line, full text in the detail. */
    notice(el, d) {
        this._line(el, d, { kind: 'notice', text: d.text || '' });
        const text = document.createElement('div');
        text.className = 'cycle-notice-text';
        text.textContent = d.text || '';
        this._detail(el).appendChild(text);
    },

    /**
     * A RUNTIME notice (2026-12): a delegated subagent settled, a background job settled,
     * or the user answered a question the AI asked. It is NOT user speech and NOT loop
     * guidance, so it reads as its own row kind with a source label. The status line
     * settles through CycleBlock.setNotice() (one row per notice_id), exactly like a steer.
     */
    runtime(el, d) {
        el.dataset.noticeId = d.notice_id || '';
        el.dataset.noticeState = d.state || (d.applied ? 'applied'
            : (d.deferred ? 'deferred' : (d.queued ? 'queued' : 'arrived')));

        const label = document.createElement('span');
        label.className = 'cycle-runtime-label';
        label.textContent = _runtimeSourceLabel(d.source || 'runtime');

        const status = document.createElement('span');
        status.className = 'cycle-runtime-status';
        status.textContent = _runtimeStatusLabel(el.dataset.noticeState, d.reason);

        this._line(el, d, { nameEl: label, extra: status, text: d.text || '' });
        const text = document.createElement('div');
        text.className = 'cycle-runtime-text';
        text.textContent = d.text || '';
        this._detail(el).appendChild(text);
    },

    /** A delegated subagent (2026-12): id + label on the line, mode/depth/status/time in
     *  the detail. `status` settles from the cycle_child frame (running -> settled/aborted). */
    child(el, d) {
        el.dataset.childId = d.child_id || '';
        el.dataset.childStatus = d.status || 'running';

        const name = document.createElement('span');
        name.className = 'cycle-child-name';
        name.textContent = 'subagent ' + (d.child_id || '?');
        if (d.label) {
            const lab = document.createElement('span');
            lab.className = 'cycle-child-label';
            lab.textContent = d.label;
            name.appendChild(lab);
        }
        const status = document.createElement('span');
        status.className = 'cycle-child-status';
        status.textContent = _runtimeEntityLabel(d.status, d.stop_reason);

        this._line(el, d, { kind: 'child', nameEl: name, extra: status });

        const detail = this._detail(el);
        const meta = document.createElement('div');
        meta.className = 'cycle-runtime-meta';
        const bits = [];
        if (d.mode) bits.push(d.mode);
        if (d.depth != null) bits.push('depth ' + d.depth);
        // `steps` = the child's own provider iterations; `turns` only counts the
        // delegation calls, so showing it alone read as "the child barely worked".
        if (d.steps) bits.push('steps ' + d.steps);
        else if (d.turns) bits.push('turns ' + d.turns);
        if (d.tool_calls) bits.push('tools ' + d.tool_calls);
        if (d.at_ts) bits.push('finished ' + _fmtClock(d.at_ts));
        meta.textContent = bits.join(' \u00b7 ');
        detail.appendChild(meta);
        // The child's own streamed output lines (2026-11-30). A child's content/thinking
        // chunks arrive as cycle_child_output frames — they must paint HERE, inside this
        // child's row, never in the parent's answer strip.
        const out = document.createElement('div');
        out.className = 'cycle-child-output';
        detail.appendChild(out);
        if (d.output) this.appendChildOutput(el, d.output, 'answer');
    },

    /** Append one streamed chunk to a child row's detail. `channel` separates the child's
     *  reasoning from its answer. Observers only — the parent reads the report through the
     *  settlement notice, so painting here never steals it. */
    appendChildOutput(el, text, channel) {
        const out = el.querySelector('.cycle-child-output');
        if (!out) return;
        const line = document.createElement('div');
        line.className = 'cycle-child-line' + (channel === 'thinking' ? ' thinking' : '');
        line.textContent = String(text || '');
        out.appendChild(line);
        // A child can stream a lot; keep the DOM light, the report itself is bounded.
        while (out.childElementCount > 200) out.removeChild(out.firstElementChild);
    },

    /** A background job (2026-12): id + label, status, and its live output lines (appended
     *  by cycle_job_output frames - observers only, never the model's read cursor). */
    job(el, d) {
        el.dataset.jobId = d.job_id || '';
        el.dataset.jobStatus = d.status || 'running';

        const name = document.createElement('span');
        name.className = 'cycle-job-name';
        name.textContent = 'job ' + (d.job_id || '?');
        if (d.label) {
            const lab = document.createElement('span');
            lab.className = 'cycle-child-label';
            lab.textContent = d.label;
            name.appendChild(lab);
        }
        const status = document.createElement('span');
        status.className = 'cycle-job-status';
        status.textContent = _runtimeEntityLabel(d.status, d.detail);

        this._line(el, d, { kind: 'job', nameEl: name, extra: status });

        const detail = this._detail(el);
        const meta = document.createElement('div');
        meta.className = 'cycle-runtime-meta';
        const bits = [];
        if (d.kind) bits.push(d.kind);
        if (d.progress) bits.push('progress: ' + d.progress);
        if (d.at_ts) bits.push('finished ' + _fmtClock(d.at_ts));
        if (d.spill_path) bits.push('full stream: ' + d.spill_path);
        meta.textContent = bits.join(' \u00b7 ');
        detail.appendChild(meta);
        const out = document.createElement('div');
        out.className = 'cycle-job-output';
        detail.appendChild(out);
        if (d.output) this.appendJobOutput(el, d.output, 'stdout');
    },

    /** Append one live output chunk to a job row's detail. `channel` distinguishes the
     *  producer's stdout/stderr from its narration ('log'). */
    appendJobOutput(el, text, channel) {
        const out = el.querySelector('.cycle-job-output');
        if (!out) return;
        const line = document.createElement('div');
        line.className = 'cycle-job-line' + (channel === 'stderr' ? ' stderr' : '');
        line.textContent = String(text || '');
        out.appendChild(line);
        // A job can stream a lot; keep the DOM light, the full stream is in the spill file.
        while (out.childElementCount > 200) out.removeChild(out.firstElementChild);
    },

    /** The answer to a question the AI asked (2026-12). The card itself is transient, like
     *  a consent card: this row is the durable fact. */
    question(el, d) {
        el.dataset.requestId = d.request_id || '';
        const line = document.createElement('div');
        line.className = 'cycle-consent-line';
        line.textContent = _questionLabel(d.decision, d.pending);
        this._line(el, d, { kind: 'question', extra: line, text: d.question || '' });
        if (d.options && d.options.length) {
            const opts = document.createElement('div');
            opts.className = 'cycle-runtime-meta';
            opts.textContent = 'options: ' + d.options.join(' | ');
            this._detail(el).appendChild(opts);
        }
    },

    /**
     * A steering message the user typed WHILE this turn was running.
     * It is loop work, never a chat-level user message: the row sits inside the block at
     * the moment the user typed it (at_ts = received_at), so it reads as "the user
     * redirected the task here", between the tool rows of that step. The line carries the
     * label, the typed time, the status and the text preview; the full text is in the detail.
     * The status line settles through CycleBlock.setSteer() (one row per steer_id).
     */
    steer(el, d) {
        el.dataset.steerId = d.steer_id || '';
        el.dataset.steerState = d.state || (d.applied ? 'applied'
            : (d.rejected ? 'rejected' : (d.received ? 'received' : 'sending')));

        const label = document.createElement('span');
        label.className = 'cycle-steer-label';
        label.textContent = '\u21B3 steering';

        // WHEN the user typed it - the moment this entry was added to the loop history.
        const time = document.createElement('span');
        time.className = 'cycle-steer-time';
        time.textContent = _fmtClock(d.at_ts);

        const status = document.createElement('span');
        status.className = 'cycle-steer-status';
        status.textContent = _steerStatusLabel(el.dataset.steerState, d.reason);

        this._line(el, d, { nameEl: label, extra: status, timeEl: time, text: d.text || '' });

        const text = document.createElement('div');
        text.className = 'cycle-steer-text';
        text.textContent = d.text || '';
        this._detail(el).appendChild(text);
    },

    /** One AI text block of one step: thinking (dim) or answer (markdown).
     *  Line = turn · AI/thinking · time · preview; the whole text opens in the detail. */
    speech(el, d) {
        const kind = d.kind === 'thinking' ? 'thinking' : 'answer';
        el.dataset.speechKind = kind;
        if (d.final) el.dataset.finalSpeech = '1';
        this._line(el, d, { kind: kind === 'thinking' ? 'thinking' : 'AI', text: d.text || '' });

        const detail = this._detail(el);
        const lab = document.createElement('div');
        lab.className = 'cycle-speech-label';
        lab.textContent = kind === 'thinking' ? 'thinking' : 'AI';
        detail.appendChild(lab);
        const body = document.createElement('div');
        body.className = 'cycle-speech-text';
        if (kind === 'thinking') body.textContent = d.text || '';
        else if (typeof parseMarkdown === 'function') body.innerHTML = parseMarkdown(d.text || '', false);
        else body.textContent = d.text || '';
        detail.appendChild(body);
    },

    /** A generated image: filename on the line, thumb + setup line in the detail.
     *  TWO SOURCES (2026-12): a live 'image_generated' frame carries inline base64; a row
     *  rebuilt from the persisted turn_log payload carries FILE IDENTITY only and renders
     *  over HTTP (/api/download?path=...&inline=true) - so a reload shows the same image. */
    image(el, d) {
        el.dataset.callId = d.call_id || '';
        this._line(el, d, { kind: 'image', text: d.filename || 'generated image' });
        const detail = this._detail(el);
        const img = document.createElement('img');
        img.className = 'cycle-image-thumb';
        if (d.image_base64) {
            img.src = 'data:image/png;base64,' + d.image_base64;
        } else if (d.rel_path || d.filename) {
            const rel = String(d.rel_path || d.filename).replace(/\\/g, '/');
            img.src = '/api/download?path=' + encodeURIComponent(rel) + '&inline=true';
        }
        img.alt = d.filename || 'Generated image';
        img.title = 'Click to open file';
        if (d.file_path) {
            img.dataset.filePath = d.file_path;
            img.addEventListener('click', () => window.open(img.dataset.filePath));
        }
        detail.appendChild(img);
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
        detail.appendChild(meta);
    },

    render(kind, el, d) {
        const fn = this[kind];
        if (!fn) { el.textContent = String(d && d.text ? d.text : ''); return el; }
        try { fn.call(this, el, d || {}); } catch (e) {
            el.textContent = String((d && d.text) || '');
            console.error('[CYCLE-ROWS] render failed for', kind, e);
        }
        this._wire(el);
        return el;
    },
};

function _fmtMs(ms) {
    const v = Number(ms || 0);
    if (v < 1000) return v.toFixed(1) + 'ms';
    if (v < 60000) return (v / 1000).toFixed(1) + 's';
    return _fmtSec(v / 1000);
}

/** Clock time of an event (epoch seconds -> HH:MM:SS, local). Every row line shows WHEN
 *  the entry happened, so the loop history reads as a timeline, not a wall of text. */
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

/**
 * (2026-12 runtime) Labels for the runtime lane. ONE source of truth, mirroring
 * logic/runtime_notice.py's sources: a notice is the RUNTIME speaking (a child settled, a
 * job settled, the user answered a question the AI asked), never user speech.
 */
function _runtimeSourceLabel(source) {
    switch (String(source || '')) {
        case 'subagent': return '\u2935 subagent settled';
        case 'job': return '\u2935 background job settled';
        case 'question': return '\u21B3 your answer';
        default: return '\u2935 runtime notice';
    }
}
window._runtimeSourceLabel = _runtimeSourceLabel;

function _runtimeStatusLabel(state, reason) {
    switch (state) {
        case 'queued': return '\u23F3 queued \u2014 injected at the loop\u2019s next step';
        case 'applied': return '\u2713 applied \u2014 the agent has read this';
        case 'deferred': return '\u26A0 waiting \u2014 ' + (reason || 'no wake budget left; it waits for your next message.');
        case 'arrived': return '\u21B3 arrived';
        default: return '\u2713 ' + (state || 'recorded');
    }
}
window._runtimeStatusLabel = _runtimeStatusLabel;

/** Status rank so a late frame never downgrades a settled runtime row. */
const _RUNTIME_STATE_RANK = { arrived: 0, queued: 1, deferred: 2, applied: 3 };
window._RUNTIME_STATE_RANK = _RUNTIME_STATE_RANK;

/** A child/job entity status on its row line. Honest about what the runtime knows. */
function _runtimeEntityLabel(status, detail) {
    switch (String(status || '')) {
        case 'running': return '\u23F3 running';
        case 'stopping': return '\u23F3 stopping';
        case 'settled': return '\u2713 done';
        case 'completed': return '\u2713 done';
        case 'aborted': return '\u26A0 aborted' + (detail ? ' \u2014 ' + detail : '');
        case 'killed': return '\u26A0 killed' + (detail ? ' \u2014 ' + detail : '');
        case 'failed': return '\u2716 failed' + (detail ? ' \u2014 ' + detail : '');
        case 'error': return '\u2716 error' + (detail ? ' \u2014 ' + detail : '');
        case 'incomplete': return '\u26A0 incomplete \u2014 the process ended before it settled';
        default: return String(status || 'unknown');
    }
}
window._runtimeEntityLabel = _runtimeEntityLabel;

/** The question row's decision label (the card itself is transient, like a consent card). */
function _questionLabel(decision, pending) {
    if (pending) return '\u23F3 waiting for your answer';
    switch (String(decision || '')) {
        case 'deny': return '\u274C skipped \u2014 the AI decides';
        case 'allow_category_until_done': return '\u26A1 you decided \u2014 no more questions this task';
        case 'unanswered': return '\u26A0 unanswered';
        default: return '\u2713 answered';
    }
}
window._questionLabel = _questionLabel;

window.CycleRows = CycleRows;
window._fmtMs = _fmtMs;
