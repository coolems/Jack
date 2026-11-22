
/**
 * voice-ui.js - Mic + Speaker toggles in the prompt footer (2026-11-26).
 *
 * Two buttons next to the Think chip, same grey style, .active when on:
 *
 *  MIC (🎙)  - 3-state cycle (off -> "Audio to prompt" -> "Directly to AI" -> off).
 *    Each click transitions IMMEDIATELY to the next state (no grayed-out
 *    intermediate) - a click while listening does NOTHING else: it stops the
 *    current recorder (the last buffer still flushes) and starts the next mode.
 *    Both listening modes run a loop: the mic is monitored for speech (RMS
 *    level, sustained 300 ms so coughs don't count). After a pause the buffer
 *    is transcribed and APPENDED to the prompt LIVE (text appears while you
 *    keep talking, no matter how many pauses you take):
 *      - "Audio to prompt": 2 s pause -> append only. NEVER sent to the AI.
 *      - "Directly to AI": 5 s pause -> append live AND send the prompt to
 *        the AI automatically (if idle).
 *
 *  SPEAKER (🔊) - toggle. While on, when a turn ends with a final answer and NO
 *    tool call happened in that turn, the answer is spoken via
 *    GET /api/addons/speech (language auto-detected server-side). The answer
 *    is split into sentences ("Agentic AI: TASK DONE" stripped, empties
 *    dropped) and spoken one after another; sentence N+1 is synthesized while
 *    N is still playing, so there is no gap between them. Tool-heavy turns
 *    are never spoken. A new spoken answer stops the previous one. Turning
 *    the speaker OFF stops playback AND cancels the whole remaining queue
 *    (active sentence + prefetched next + everything left).
 *
 * Both honor the device pickers from Settings > Addons (addons.json mic/speaker).
 * If the matching addon is disabled the toggle turns itself off and says why.
 */

const VoiceUI = (() => {
    'use strict';

    const LIVE_FLUSH_MS = 2000;   // mode 1: pause that triggers a live transcribe+append
    const AUTO_SEND_MS = 5000;    // mode 2: pause after the last word -> auto-send to the AI
    const CHECK_MS = 500;         // silence-check interval
    const LEVEL_MS = 100;         // mic level sampling interval
    const VOICE_RMS = 0.012;      // level that counts as speech
    const VOICE_SUSTAIN_MS = 300; // level must hold this long (filters coughs)
    const MAX_REC_MS = 120000;    // hard cap: one voiced segment never exceeds 2 min

    let micBtn = null, micLabel = null, spkBtn = null;
    let micMode = 0;              // 0 = off, 1 = "Audio to prompt", 2 = "Directly to AI"
    let recorder = null, recStream = null;
    let recChunks = [];           // { blob, voiced } - one entry per 1 s timeslice
    let recHeader = null;         // the very first data chunk (carries the WebM header)
    let recActive = false, recTimer = null, levelTimer = null;
    let analyser = null, levelData = null;
    let lastVoiceAt = 0, voiceStartAt = null, recStartedAt = 0;
    let voicedSince = null;       // when the current buffer's first voiced slice arrived
    let recStarting = false;      // async start in flight (getUserMedia) - clicks are ignored
    let processing = false;       // a transcription is in flight
    let inflight = 0;             // transcriptions currently in flight

    let audio = null;             // one shared <audio> for TTS playback
    let speakQueue = Promise.resolve();
    let speakSeq = 0;             // generation counter: a new answer invalidates the old loop
    let playWaiter = null;        // resolves the current onended wait when playback is stopped
    let pendingUrl = '';          // prefetched next sentence - revocable from the stop path
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
        if (playWaiter) { const w = playWaiter; playWaiter = null; w(); }  // wake the speaking loop
        if (pendingUrl) { URL.revokeObjectURL(pendingUrl); pendingUrl = ''; }  // drop the prefetched next sentence
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
        const label = micMode === 1 ? 'Audio to prompt' : micMode === 2 ? 'Directly to AI' : '';
        if (micLabel) micLabel.textContent = label;
        micBtn.title = !recActive
            ? (micMode === 0 ? 'Voice input - click to start listening (Audio to prompt)'
                             : 'Click to cycle: ' + (micMode === 1 ? 'Directly to AI' : 'off'))
            : (micMode === 1 ? 'Listening - text appears live in the prompt; click to switch to Directly to AI'
                             : 'Listening - 5 s pause sends to the AI; click to switch to off');
    }

    async function _startRec(force) {
        if (recActive || recStarting) return;
        recStarting = true;
        if (sttEnabled === false) {
            recStarting = false;
            refreshTtsState().then(() => {
                if (sttEnabled === false) {
                    showNotification('STT is disabled - enable it in Settings > Addons.', 'warning');
                } else _startRec();
            });
            return;
        }
        if (typeof COOLEMS !== 'undefined' && COOLEMS.isStreaming) {
            recStarting = false;
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
            recStarting = false;
            showNotification('Microphone unavailable: ' + (e.name || e), 'error');
            return;
        }
        recStream = stream;
        recChunks = [];
        recHeader = null;
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
            if (!recHeader) recHeader = e.data;   // first chunk = WebM header, keep it forever
            const now = Date.now();
            const voiced = analyser ? (now - lastVoiceAt) < 1100 : true;
            recChunks.push({ blob: e.data, voiced });
            if (voiced && voicedSince === null) voicedSince = now;
        };
        rec.onstop = () => { _recCleanup(); _flushBuffer(); };
        rec.start(1000);                       // 1 s timeslices keep the silence timer honest
        recorder = rec;
        recActive = true;
        recStarting = false;
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
            // A pause triggers a flush in BOTH modes: the transcript is appended
            // to the prompt LIVE while the user keeps talking. The pause length
            // differs per mode: mode 1 (Audio to prompt) flushes after 2 s;
            // mode 2 (Directly to AI) waits 5 s after the LAST word, then the
            // buffer is transcribed AND the prompt is sent to the AI.
            const pauseMs = micMode === 2 ? AUTO_SEND_MS : LIVE_FLUSH_MS;
            if (now - lastVoiceAt >= pauseMs) { _flushBuffer(); return; }
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
        const mode = micMode;      // the mode this buffer was recorded in (a mode
        let first = recChunks.findIndex(c => c.voiced);   // switch mid-flight must not change it)
        if (first < 0) { recChunks = []; return; }
        if (first > 0) first -= 1;
        let blobs;
        if (first === 0 && recChunks[0] === recHeader) {
            blobs = recChunks.map(c => c.blob);      // buffer starts with the header chunk
        } else {
            // The voiced segment starts mid-stream (or the buffer was already
            // flushed once, so its first slice is NOT the header): a blob without
            // the WebM header is rejected by the decoder ("Invalid data found
            // when processing input"). Prepend the original first chunk.
            blobs = [(recHeader || recChunks[0])].concat(recChunks.slice(first).map(c => c.blob));
        }
        recChunks = [];
        voicedSince = null;
        lastVoiceAt = 0;
        voiceStartAt = null;
        recStartedAt = Date.now();
        if (blobs.length) _processBuffer(blobs, mode);
    }

    function _stopRec(send) {
        if (!recActive) return;
        recActive = false;
        if (recTimer) { clearInterval(recTimer); recTimer = null; }
        if (levelTimer) { clearInterval(levelTimer); levelTimer = null; }
        if (analyser) { try { analyser.context.close(); } catch (e) {} analyser = null; }
        _paintMic();
        if (send) {
            // A click while listening does NOTHING except switch the icon state:
            // the last buffer still flushes (its text lands in the prompt), but
            // nothing is sent - only a 5 s pause in "Directly to AI" auto-sends.
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

    async function _processBuffer(blobs, mode) {
        const blob = new Blob(blobs, { type: 'audio/webm' });
        if (blob.size < 2000) return;          // nothing recorded
        processing = true;
        inflight++;
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
            inflight--;
        }
        if (!text) return;
        const input = document.getElementById('messageInput');
        if (!input) return;
        // Both modes append the transcript to the prompt LIVE. "Directly to AI"
        // (mode 2) then sends the prompt to the AI automatically - this is the
        // 5 s pause auto-send. "Audio to prompt" (mode 1) NEVER sends.
        input.value = input.value + (input.value.endsWith('\n') || !input.value ? '' : '\n') + text;
        if (typeof autoResize === 'function') autoResize(input);
        input.focus();
        if (mode === 2) _sendVoice();
    }

    /** "Directly to AI" auto-send: fires after a mode-2 buffer's transcript has
        landed in the prompt. If the AI is busy, the text stays in the prompt and
        the NEXT mode-2 buffer's landing retries the send. */
    function _sendVoice() {
        const input = document.getElementById('messageInput');
        if (!input || !input.value.trim()) return;
        if (typeof COOLEMS !== 'undefined' && COOLEMS.isStreaming) return;   // AI busy - retry on next landing
        showNotification('🎙 ' + input.value, 'info');
        sendMessage();
    }

    /** 3-state cycle: off -> "Audio to prompt" -> "Directly to AI" -> off.
        Each click immediately transitions to the next state (no grayed-out
        intermediate). While listening, a click stops the current recorder and
        starts the next mode (or stops if cycling back to off). */
    function toggleMic() {
        if (recActive) {
            _stopRec(true);
        }
        micMode = (micMode + 1) % 3;
        _paintMic();
        if (micMode === 0) return;          // cycled back to off
        _startRec(true);
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
        if (!spkOn) {
            _stopPlayback();
            speakSeq++;   // invalidate the running loop: it must NOT continue to the next sentence
        }
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

    /** Split a final answer into speakable sentences: strip the
        "Agentic AI: TASK DONE" marker, break on . ! ?, drop empty pieces. */
    function _splitSentences(text) {
        const clean = (text || '').replace(/Agentic AI:\s*TASK DONE/gi, '').trim();
        if (!clean) return [];
        return clean.split(/(?<=[.!?])\s+/).map(s => s.trim()).filter(Boolean);
    }

    /** Mirror of the server fingerprint (app/addons/tts/supertonic/text_utils
        detect_language): RO diacritics -> 'ro', ß/umlauts -> 'de', else '' (let the
        server decide). Detected ONCE on the WHOLE answer and passed to every
        sentence: a German sentence without umlauts ("Der Arzt gibt Peter ein
        Rezept.") would be detected as English on its own and get the wrong voice. */
    function _detectAnswerLang(text) {
        if (/[ăâîșțĂÂÎȘȚ]/.test(text)) return 'ro';
        if (/[ßäöüÄÖÜ]/.test(text)) return 'de';
        return '';
    }

    async function _fetchSpeech(sentence, lang) {
        let url = '/api/addons/speech?text=' + encodeURIComponent(sentence);
        if (lang) url += '&lang=' + lang;
        const r = await fetch(url);
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || ('HTTP ' + r.status));
        }
        const buf = await r.arrayBuffer();
        return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
    }

    /** Queue one answer for playback. It is split into sentences and spoken one
        after another; while sentence N is playing, sentence N+1 is ALREADY being
        synthesized (prefetched), so there is no silence between sentences.
        A new answer stops the current one immediately. */
    function speak(text) {
        const sentences = _splitSentences(text);
        if (!sentences.length) return;
        const lang = _detectAnswerLang(text);   // one detection for the WHOLE answer
        const seq = ++speakSeq;
        _stopPlayback();   // stop the currently playing answer now (also wakes its loop)
        speakQueue = speakQueue.then(async () => {
            pendingUrl = '';
            try {
                for (let i = 0; i < sentences.length; i++) {
                    if (seq !== speakSeq) return;              // superseded by a newer answer
                    let url = (i > 0 && pendingUrl) ? pendingUrl : await _fetchSpeech(sentences[i], lang);
                    pendingUrl = '';
                    // Prepare the next sentence while this one is still playing.
                    if (i + 1 < sentences.length) {
                        _fetchSpeech(sentences[i + 1], lang)
                            .then(u => { if (seq === speakSeq) pendingUrl = u; else URL.revokeObjectURL(u); })
                            .catch(() => { /* the next iteration fetches on demand */ });
                    }
                    const a = _audioEl();
                    try { if (devSpk && a.setSinkId) await a.setSinkId(devSpk); } catch (e) {}
                    a.src = url;
                    await a.play();
                    await new Promise(res => {
                        a.onended = res;
                        a.onerror = res;
                        playWaiter = res;                      // _stopPlayback() can wake us
                    });
                    playWaiter = null;
                    if (seq === speakSeq) setTimeout(() => URL.revokeObjectURL(url), 5000);
                    else URL.revokeObjectURL(url);
                }
            } catch (e) {
                if (seq === speakSeq) showNotification('Speech failed: ' + e.message + ' (Settings > Addons > TTS)', 'error');
            }
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
        micLabel = document.getElementById('micLabel');
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
