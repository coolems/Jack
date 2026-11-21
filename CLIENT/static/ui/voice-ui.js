
/**
 * voice-ui.js - Mic + Speaker toggles in the prompt footer (2026-11-26).
 *
 * Two buttons next to the Think chip, same grey style, .active when on:
 *
 *  MIC (🎙)  - toggle. While on, it runs a LOOP: the mic is monitored for
 *    speech (RMS level, sustained 300 ms so coughs don't count). Leading
 *    silence is never recorded - only the voiced segment goes into the
 *    buffer. After 5 s of silence the voiced buffer is sent to
 *    POST /api/addons/transcribe and the transcript is APPENDED to the
 *    prompt; if the prompt is empty and no turn is running it is SENT
 *    directly to the AI. The loop then keeps listening for the next
 *    utterance. Clicking the button again stops the loop and appends the
 *    current buffer to the prompt (never auto-sent to the AI).
 *
 *  SPEAKER (🔊) - toggle. While on, when a turn ends with a final answer and NO
 *    tool call happened in that turn, the answer is spoken via
 *    GET /api/addons/speech (language auto-detected server-side). Tool-heavy
 *    turns are never spoken. A new spoken answer stops the previous one.
 *
 * Both honor the device pickers from Settings > Addons (addons.json mic/speaker).
 * If the matching addon is disabled the toggle turns itself off and says why.
 */

const VoiceUI = (() => {
    'use strict';

    const SILENCE_MS = 5000;      // pause of silence that triggers transcribe+send
    const CHECK_MS = 500;         // silence-check interval
    const LEVEL_MS = 100;         // mic level sampling interval
    const VOICE_RMS = 0.012;      // level that counts as speech
    const VOICE_SUSTAIN_MS = 300; // level must hold this long (filters coughs)
    const MAX_REC_MS = 120000;    // hard cap: one voiced segment never exceeds 2 min

    let micBtn = null, spkBtn = null;
    let recorder = null, recStream = null;
    let recChunks = [];           // { blob, voiced } - one entry per 1 s timeslice
    let recActive = false, recTimer = null, levelTimer = null;
    let analyser = null, levelData = null;
    let lastVoiceAt = 0, voiceStartAt = null, recStartedAt = 0;
    let voicedSince = null;       // when the current buffer's first voiced slice arrived
    let processing = false;
    let stopRequested = false;    // click-to-stop: transcript goes to the prompt, never auto-sent

    let audio = null;             // one shared <audio> for TTS playback
    let speakQueue = Promise.resolve();
    let ttsEnabled = null, sttEnabled = null;  // null = unknown, filled by /api/addons reads
    let spkOn = false;
    let devMic = '', devSpk = ''; // device ids from addons.json ('' = system default)

    // Per-turn trackers (the "no tool calls at all" rule for auto-speak).
    // (2026-11-26 fix) keyed by conversation: one global tracker was wiped by the
    // FIRST frame of ANY turn - a background workspace's frame arriving mid-turn
    // reset the on-screen turn's flags, so onTurnDone saw tracked=false and never
    // spoke. Each conversation now tracks its own turn independently.
    const turns = {};

    // ===== helpers =============================================================

    function _audioEl() {
        if (!audio) {
            audio = new Audio();
            audio.preload = 'auto';
        }
        return audio;
    }

    function _stopPlayback() {
        try { if (audio) { audio.pause(); audio.removeAttribute('src'); } } catch (e) {}
    }

    /** Apply TTS-enabled flag + the selected mic/speaker devices from one /api/addons
        payload. Cheap: that endpoint only reads config + registry state. */
    function applyAddonsState(d) {
        const tts = (d.addons || []).find(a => a.name === 'tts');
        const stt = (d.addons || []).find(a => a.name === 'stt');
        ttsEnabled = !!(tts && tts.enabled);
        sttEnabled = !!(stt && stt.enabled);
        devSpk = (tts && tts.speaker) || '';
        devMic = (stt && stt.mic) || '';
    }

    /** Refresh from the server (no engine load). Pass a payload to skip the fetch. */
    async function refreshTtsState(payload) {
        try {
            if (payload) { applyAddonsState(payload); return; }
            const r = await fetch('/api/addons');
            applyAddonsState(await r.json());
        } catch (e) { ttsEnabled = null; }
    }

    // ===== MIC =================================================================

    function _paintMic() {
        if (!micBtn) return;
        micBtn.classList.toggle('active', recActive);
        micBtn.classList.toggle('recording', recActive);
        micBtn.title = recActive
            ? 'Listening - each utterance is sent after a 5 s pause (click to stop)'
            : 'Voice input - click to start listening';
    }

    async function _startRec() {
        if (recActive || processing) return;
        if (sttEnabled === false) {
            refreshTtsState().then(() => {
                if (sttEnabled === false) {
                    showNotification('STT is disabled - enable it in Settings > Addons.', 'warning');
                } else _startRec();
            });
            return;
        }
        if (typeof COOLEMS !== 'undefined' && COOLEMS.isStreaming) {
            showNotification('Voice input pauses while a response is running.', 'info');
            return;
        }
        let stream;
        try {
            const dev = devMic;
            if (dev && navigator.mediaDevices) {
                try { stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: { exact: dev } }, video: false }); }
                catch (e) { stream = null; }        // device gone -> fall through to default
            }
            if (!stream) stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        } catch (e) {
            showNotification('Microphone unavailable: ' + (e.name || e), 'error');
            return;
        }
        recStream = stream;
        recChunks = [];
        voicedSince = null;
        lastVoiceAt = 0;
        voiceStartAt = null;

        // Level monitor: decides which timeslices are "voiced" so leading
        // silence never enters the buffer.
        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            const ctx = new AC();
            analyser = ctx.createAnalyser();
            analyser.fftSize = 1024;
            levelData = new Uint8Array(analyser.fftSize);
            ctx.createMediaStreamSource(stream).connect(analyser);
            levelTimer = setInterval(_levelTick, LEVEL_MS);
        } catch (e) {
            analyser = null;                 // no level info -> treat every slice as voiced
        }

        let rec;
        try {
            rec = new MediaRecorder(stream, { mimeType: 'audio/webm' });
        } catch (e) {
            rec = new MediaRecorder(stream);
        }
        rec.ondataavailable = (e) => {
            if (!e.data || !e.data.size) return;
            const now = Date.now();
            const voiced = analyser ? (now - lastVoiceAt) < 1100 : true;
            recChunks.push({ blob: e.data, voiced });
            if (voiced && voicedSince === null) voicedSince = now;
        };
        rec.onstop = () => { _recCleanup(); _flushBuffer(); };
        rec.start(1000);                       // 1 s timeslices keep the silence timer honest
        recorder = rec;
        recActive = true;
        recStartedAt = Date.now();
        recTimer = setInterval(_checkSilence, CHECK_MS);
        _paintMic();
    }

    /** Sample the mic level; track sustained speech so a cough doesn't arm the buffer. */
    function _levelTick() {
        if (!analyser) return;
        analyser.getByteTimeDomainData(levelData);
        let sum = 0;
        for (let i = 0; i < levelData.length; i++) {
            const v = (levelData[i] - 128) / 128;
            sum += v * v;
        }
        const rms = Math.sqrt(sum / levelData.length);
        const now = Date.now();
        if (rms > VOICE_RMS) {
            if (voiceStartAt === null) voiceStartAt = now;
            if (now - voiceStartAt >= VOICE_SUSTAIN_MS) lastVoiceAt = now;
        } else if (now - (lastVoiceAt || 0) > 400) {
            voiceStartAt = null;              // silence held long enough: speech ended
        }
    }

    function _checkSilence() {
        if (!recActive) return;
        const now = Date.now();
        const hasVoice = recChunks.some(c => c.voiced);
        if (hasVoice) {
            if (now - lastVoiceAt >= SILENCE_MS) { _flushBuffer(); return; }
            if (voicedSince && now - voicedSince > MAX_REC_MS) { _flushBuffer(); return; }
        } else if (recChunks.length > 1 && now - recStartedAt > 5000) {
            // Only leading silence so far: discard, keep waiting.
            // KEEP slice 0 - it carries the WebM header; a blob without it
            // is unparseable by the STT engine.
            recChunks = [recChunks[0]];
        }
    }

    /** Cut the voiced segment out of the buffer (keeping one slice before the first
        voiced one so the opening syllable isn't clipped) and send it. The loop
        then keeps listening for the next utterance. */
    function _flushBuffer() {
        let first = recChunks.findIndex(c => c.voiced);
        if (first < 0) { recChunks = []; return; }
        if (first > 0) first -= 1;
        let blobs = recChunks.slice(first).map(c => c.blob);
        if (first > 0) blobs = [recChunks[0].blob].concat(blobs);
        // ^ slice 0 holds the WebM header - every blob must start with it or
        //   the decoder rejects the file ("Invalid data found when processing input").
        recChunks = [];
        voicedSince = null;
        lastVoiceAt = 0;
        voiceStartAt = null;
        recStartedAt = Date.now();
        if (blobs.length) _processBuffer(blobs);
    }

    function _stopRec(send) {
        if (!recActive) return;
        recActive = false;
        stopRequested = true;      // click-to-stop: transcript goes to the prompt, never auto-sent
        if (recTimer) { clearInterval(recTimer); recTimer = null; }
        if (levelTimer) { clearInterval(levelTimer); levelTimer = null; }
        if (analyser) { try { analyser.context.close(); } catch (e) {} analyser = null; }
        _paintMic();
        if (send) {
            // Let the recorder flush its last timeslice; onstop then processes the buffer.
            try { if (recorder && recorder.state !== 'inactive') recorder.stop(); }
            catch (e) { _recCleanup(); _flushBuffer(); }
        } else {
            _recCleanup();
            _flushBuffer();
        }
    }

    function _recCleanup() {
        if (recStream) { recStream.getTracks().forEach(t => t.stop()); recStream = null; }
        recorder = null;
    }

    async function _processBuffer(blobs) {
        const blob = new Blob(blobs, { type: 'audio/webm' });
        if (blob.size < 2000) return;          // nothing recorded
        processing = true;
        let text = '';
        try {
            const fd = new FormData();
            fd.append('file', blob, 'speech.webm');
            const r = await fetch('/api/addons/transcribe', { method: 'POST', body: fd });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
            text = (d.text || '').trim();
        } catch (e) {
            showNotification('Transcription failed: ' + e.message + ' (Settings > Addons > STT)', 'error');
            return;
        } finally {
            processing = false;
        }
        if (!text) return;
        const input = document.getElementById('messageInput');
        if (!input) return;
        const toPrompt = stopRequested;   // user clicked stop: always land in the prompt
        stopRequested = false;
        if (input.value.trim() || toPrompt) {
            // The user is composing (or stopped the mic): append, never send.
            input.value = input.value + (input.value.endsWith('\n') || !input.value ? '' : '\n') + text;
            if (typeof autoResize === 'function') autoResize(input);
            input.focus();
        } else if (typeof COOLEMS !== 'undefined' && COOLEMS.isStreaming) {
            input.value = text;
            if (typeof autoResize === 'function') autoResize(input);
        } else {
            showNotification('🎙 ' + text, 'info');
            input.value = text;
            if (typeof autoResize === 'function') autoResize(input);
            sendMessage();                     // empty prompt + idle -> straight to the AI
        }
    }

    function toggleMic() {
        if (recActive) _stopRec(true);      // click while listening: stop the loop + send now
        else _startRec();
    }

    // ===== SPEAKER ==============================================================

    function _paintSpk() {
        if (!spkBtn) return;
        spkBtn.classList.toggle('active', spkOn);
        spkBtn.title = spkOn
            ? 'Auto-speak on - the last answer is spoken when a turn ends without tool calls'
            : 'Auto-speak - click to hear the AI\u2019s final answers';
    }

    function toggleSpeaker() {
        spkOn = !spkOn;
        _paintSpk();
        if (!spkOn) _stopPlayback();
        if (spkOn && ttsEnabled === false) {
            refreshTtsState().then(() => {
                if (ttsEnabled === false) {
                    spkOn = false;
                    _paintSpk();
                    showNotification('TTS is disabled - enable it in Settings > Addons.', 'warning');
                }
            });
        }
    }

    /** Queue one text for playback (FIFO; a new answer stops the current one). */
    function speak(text) {
        if (!text || !text.trim()) return;
        speakQueue = speakQueue.then(async () => {
            _stopPlayback();
            let url = '';
            try {
                const r = await fetch('/api/addons/speech?text=' + encodeURIComponent(text.trim()));
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    throw new Error(d.detail || ('HTTP ' + r.status));
                }
                const buf = await r.arrayBuffer();
                url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
                const a = _audioEl();
                try { if (devSpk && a.setSinkId) await a.setSinkId(devSpk); } catch (e) {}
                a.src = url;
                await a.play();
            } catch (e) {
                showNotification('Speech failed: ' + e.message + ' (Settings > Addons > TTS)', 'error');
            }
            if (url) setTimeout(() => URL.revokeObjectURL(url), 60000);
        }).catch(() => {});
    }

    // ===== TURN TRACKING (hooks called from websocket.js) =======================

    /** First frame of a turn (content/thinking) - reset the tool flag.
        Keyed by turn_id, falling back to the conv id (normal turns carry no turn_id),
        so later frames of the SAME turn never wipe the flags. Per conversation:
        a background workspace's frames must not touch the on-screen turn's tracker. */
    function onTurnStart(convId, turnId) {
        const key = turnId || convId || null;
        if (!key) return;
        const t = turns[convId];
        if (t && t.id === key) return;
        turns[convId] = { id: key, tools: false };
    }

    function onToolStart(convId) {
        if (!convId) return;
        // The turn may start straight into a tool call (no content frame first),
        // so the tracker might not exist yet - create it and mark the tool.
        const t = turns[convId] || (turns[convId] = { id: convId, tools: false });
        t.tools = true;
    }

    /** done: if the speaker is on, there IS a final answer and THIS turn used NO
        tool calls, speak the answer. (2026-11-26 fix) the old 'answered' flag was
        only set when a cycle block existed - normal one-shot turns have no block,
        so it was always false and clean answers were never spoken. The final text
        arrives here, so its presence IS the answer check. A turn that ran tools
        but still delivered a final answer (finalText non-empty) is NOT spoken -
        the user rule is "no tool calls at all". */
    function onTurnDone(convId, finalText) {
        const clean = (finalText || '').trim();
        const t = turns[convId];
        const noTools = !t || !t.tools;
        delete turns[convId];
        if (!spkOn) return;
        if (typeof COOLEMS === 'undefined') return;
        if (convId && COOLEMS.currentConversation && convId !== COOLEMS.currentConversation) return;
        if (!clean || !noTools) return;
        speak(clean);
    }

    // ===== INIT =================================================================

    function init() {
        micBtn = document.getElementById('micToggle');
        spkBtn = document.getElementById('spkToggle');
        if (micBtn) micBtn.addEventListener('click', toggleMic);
        if (spkBtn) spkBtn.addEventListener('click', toggleSpeaker);
        _paintMic(); _paintSpk();
        refreshTtsState();
        window.addEventListener('beforeunload', _stopPlayback);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    return { onTurnStart, onToolStart, onTurnDone, refreshTtsState };
})();

window.VoiceUI = VoiceUI;
