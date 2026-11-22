/**
 * addons-settings.js - Settings > Addons tab (2026-07): manage client_config/addons.json live.
 *
 * One page per addon (TTS / STT), picked with the addon selector at the top of the pane.
 * Each page: enable toggle, provider picker (local free/private <-> OpenAI paid), the
 * provider's fields (incl. speaker/microphone device pickers), and a live test
 * box (speak a sample / transcribe a recording).
 *
 * GET  /api/addons -> every addon (enabled AND disabled) + live engine state.
 * POST /api/addons -> full replacement of the "addons" list; the backend validates with
 *                    the startup validator, writes the file and re-wires live (no restart).
 *                    On any validation problem nothing is written (400 + reasons).
 *
 * Test endpoints (only work while the addon is enabled + loaded):
 *   GET  /api/addons/speech?text=...&voice=...&lang=... -> audio bytes (play inline)
 *   POST /api/addons/transcribe (multipart audio)       -> {"text": "..."}
 */

let addonsList = [];        // full entries from GET /api/addons
let addonsProblems = [];    // config problems reported by the backend
let addonsActive = 'tts';   // which addon page is shown
let addonsSaving = false;   // one save at a time - keeps UI and file in sync
let addonsSaveQueued = false; // a toggle happened while a save was running -> save again after
let addonsAudioUrl = null;  // object URL of the last test speech (revoked on replace)
let addonsSaveTimer = null; // auto-save debounce (600 ms after the last field change)

const ADDON_LABELS = { tts: '🔊 Text to Speech (TTS)', stt: '🎙 Speech to Text (STT)' };

function addonsStatus(msg, isError) {
    const el = document.getElementById('addonsStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = isError ? 'var(--danger)' : 'var(--text-secondary)';
}

/** Fill a <select> with options, keeping the current value. */
function addonsFillSelect(id, values, current) {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '';
    for (const v of values) {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v;
        sel.appendChild(o);
    }
    if (values.includes(current)) sel.value = current;
}

/** Show/hide the local vs openai field groups for an addon page. */
function addonsToggleProviderFields(kind, provider) {
    const local = document.getElementById(kind + 'LocalFields');
    const openai = document.getElementById(kind + 'OpenaiFields');
    if (local) local.style.display = provider === 'openai' ? 'none' : 'grid';
    if (openai) openai.style.display = provider === 'openai' ? 'grid' : 'none';
}

/** Build the two addon selector buttons (top of the pane). */
function addonsRenderSelector() {
    const wrap = document.getElementById('addonsSelector');
    if (!wrap) return;
    wrap.innerHTML = '';
    for (const name of ['tts', 'stt']) {
        const a = addonsList.find(x => x.name === name) || { name, enabled: false, state: 'disabled' };
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'addons-addon-btn' + (addonsActive === name ? ' active' : '');
        const dot = a.enabled ? (a.state === 'loaded' ? '🟢' : a.state === 'error' ? '🔴' : '🟡') : '⚪';
        btn.textContent = dot + ' ' + ADDON_LABELS[name];
        btn.title = 'Open the ' + name.toUpperCase() + ' configuration page';
        btn.onclick = () => { addonsActive = name; addonsRenderPage(); };
        wrap.appendChild(btn);
    }
}

/** Fill one addon's page fields from its config entry. */
function addonsFillPage(a) {
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    const setSel = (id, values, v) => { addonsFillSelect(id, values, v); };

    if (a.name === 'tts') {
        // The enable checkbox is the user's live intent: while a save is running
        // (incl. the post-save download polling) a re-render must NOT reset it,
        // or a toggle made mid-save would visibly "save itself off".
        if (!addonsSaving) setChk('ttsEnabled', a.enabled);
        setSel('ttsProvider', ['supertonic', 'openai'], a.provider);
        addonsToggleProviderFields('tts', a.provider);
        setSel('ttsModel', ['supertonic-2', 'supertonic-3'], a.model);
        setVal('ttsSpeed', a.speed);
        setVal('ttsSteps', a.total_steps);
        setVal('ttsSilence', a.silence_duration);
        setChk('ttsDetect', a.detect_language);
        setSel('ttsFallback', ['en', 'de', 'ro'], a.fallback_lang);
        setSel('ttsVoiceEn', ['M1', 'M2', 'M3', 'M4', 'M5', 'F1', 'F2', 'F3', 'F4', 'F5'], (a.voices || {}).en || 'M1');
        setSel('ttsVoiceDe', ['M1', 'M2', 'M3', 'M4', 'M5', 'F1', 'F2', 'F3', 'F4', 'F5'], (a.voices || {}).de || 'M1');
        setSel('ttsVoiceRo', ['M1', 'M2', 'M3', 'M4', 'M5', 'F1', 'F2', 'F3', 'F4', 'F5'], (a.voices || {}).ro || 'M1');
        setVal('ttsSpeaker', a.speaker || '');
        setSel('ttsOaModel', ['gpt-4o-mini-tts', 'gpt-4o-tts'], a.openai_model);
        setSel('ttsOaVoice', ['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer', 'coral', 'verse', 'ballad', 'ash', 'sage', 'amuch', 'shield'], a.openai_voice);
        setVal('ttsOaKey', a.openai_api_key || '');
    } else {
        if (!addonsSaving) setChk('sttEnabled', a.enabled);
        setSel('sttProvider', ['faster_whisper', 'openai'], a.provider);
        addonsToggleProviderFields('stt', a.provider);
        setSel('sttModel', ['tiny', 'base', 'small', 'medium', 'large-v3'], a.model);
        setSel('sttDevice', ['auto', 'cpu', 'cuda'], a.device);
        setSel('sttLang', ['auto', 'en', 'de', 'ro'], a.language);
        setVal('sttMic', a.mic || '');
        setSel('sttOaModel', ['gpt-4o-mini-transcribe', 'gpt-4o-transcribe'], a.openai_model);
        setVal('sttOaKey', a.openai_api_key || '');
    }
}

/** Read one addon's page fields back into its config entry (mutates addonsList). */
function addonsReadPage(a) {
    const getVal = (id, d) => { const el = document.getElementById(id); return el ? (el.value || d) : d; };
    const getChk = id => { const el = document.getElementById(id); return el ? el.checked : false; };
    const getNum = (id, d, lo, hi) => {
        const v = parseFloat(getVal(id, d));
        return Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;
    };
    a.enabled = getChk(a.name + 'Enabled');
    a.provider = getVal(a.name + 'Provider', a.provider);
    if (a.name === 'tts') {
        a.model = getVal('ttsModel', 'supertonic-3');
        a.speed = getNum('ttsSpeed', 1.0, 0.5, 2.0);
        a.total_steps = Math.round(getNum('ttsSteps', 10, 1, 100));
        a.silence_duration = getNum('ttsSilence', 0.3, 0.0, 2.0);
        a.detect_language = getChk('ttsDetect');
        a.fallback_lang = getVal('ttsFallback', 'en');
        a.voices = { en: getVal('ttsVoiceEn', 'M1'), de: getVal('ttsVoiceDe', 'M1'), ro: getVal('ttsVoiceRo', 'M1') };
        a.speaker = getVal('ttsSpeaker', '').trim();
        a.openai_model = getVal('ttsOaModel', 'gpt-4o-mini-tts');
        a.openai_voice = getVal('ttsOaVoice', 'alloy');
        a.openai_api_key = getVal('ttsOaKey', '').trim();
    } else {
        a.model = getVal('sttModel', 'medium');
        a.device = getVal('sttDevice', 'auto');
        a.language = getVal('sttLang', 'auto');
        a.mic = getVal('sttMic', '').trim();
        a.openai_model = getVal('sttOaModel', 'gpt-4o-mini-transcribe');
        a.openai_api_key = getVal('sttOaKey', '').trim();
    }
    return a;
}

/** Render the active addon's page (selector + its form + live state line). */
function addonsRenderPage() {
    addonsRenderSelector();
    const a = addonsList.find(x => x.name === addonsActive);
    const body = document.getElementById('addonsPageBody');
    const stateEl = document.getElementById('addonsState');
    if (!body) return;
    if (!a) {
        body.innerHTML = '<div style="color:var(--danger);">No addons configured.</div>';
        return;
    }
    // The pane carries both pages; show only the active one (grid layout per page).
    for (const name of ['tts', 'stt']) {
        const page = document.getElementById('addonsPage' + name.toUpperCase());
        if (page) page.style.display = name === addonsActive ? 'grid' : 'none';
    }
    addonsFillPage(a);
    if (stateEl) {
        if (!a.enabled) {
            stateEl.textContent = 'Disabled - toggle it on and the model downloads immediately in the background (no waiting at first use).';
            stateEl.style.color = 'var(--text-secondary)';
        } else if (a.state === 'loaded') {
            stateEl.textContent = 'Loaded - provider: ' + a.provider + '. Ready to use.';
            stateEl.style.color = 'var(--success, #4caf50)';
        } else if (a.state === 'error') {
            stateEl.textContent = 'Engine failed to load: ' + (a.error || 'unknown error');
            stateEl.style.color = 'var(--danger)';
        } else {
            stateEl.textContent = a.info || 'Model downloading/loading in the background - first use will be instant once done.';
            stateEl.style.color = 'var(--warning, #f59e0b)';
        }
    }
}

/** Load the config via API and render. */
async function loadAddons() {
    try {
        const r = await fetch('/api/addons');
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        addonsList = Array.isArray(data.addons) ? data.addons : [];
        addonsProblems = Array.isArray(data.problems) ? data.problems : [];
        // (2026-11-26) the main-UI voice toggles read devices/enabled state from here.
        if (typeof VoiceUI !== 'undefined') VoiceUI.refreshTtsState(data);
        addonsRenderPage();
        let msg = addonsList.length + ' addon(s) in client_config/addons.json.';
        if (addonsProblems.length) msg += ' Problems: ' + addonsProblems.join('; ');
        addonsStatus(msg, addonsProblems.length > 0);
    } catch (e) {
        addonsStatus('Load failed: ' + e.message + ' - is the CLIENT backend running?', true);
    }
}

/**
 * The enable checkboxes call this on change. If a save is already running
 * (e.g. the user toggled twice quickly), queue a follow-up save so the newest
 * toggle is never lost.
 */
function addonsToggleEnabled() {
    if (addonsSaving) { addonsSaveQueued = true; return; }
    saveAddons();
}

/**
 * (2026-11-26) Auto-save: every field change saves after 600 ms of quiet -
 * no Save button needed. The enable checkboxes save immediately (above); all
 * other fields (voices, provider, model, speed, keys, devices...) go through
 * this debounce so rapid typing/switching produces one save, not ten.
 */
function addonsAutoSave() {
    if (addonsSaveTimer) clearTimeout(addonsSaveTimer);
    addonsSaveTimer = setTimeout(() => { addonsSaveTimer = null; saveAddons(); }, 600);
}

/** Collect both pages, save the whole list, re-read the live state. */
async function saveAddons() {
    if (addonsSaving) { addonsSaveQueued = true; return; }
    addonsSaving = true;
    addonsStatus('Saving...');
    try {
        // Re-fill only the INACTIVE page from the last server state before
        // reading it back: its DOM still holds stale values and a read would
        // otherwise overwrite the other addon's saved config.
        // The ACTIVE page is NEVER re-filled here - its DOM is the user's live
        // intent (they may have just changed a voice/model/toggle), and a
        // re-fill would reset it to the not-yet-saved server state (the
        // "voice switches back after a few seconds" bug).
        for (const a of addonsList) {
            if (a.name === addonsActive) continue;
            addonsFillPage(a);
        }
        for (const a of addonsList) addonsReadPage(a);
        const r = await fetch('/api/addons', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ addons: addonsList })
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        addonsList = Array.isArray(data.addons) ? data.addons : addonsList;
        addonsProblems = Array.isArray(data.problems) ? data.problems : [];
        addonsStatus('Saved - applied immediately (no restart).');
        // The re-wire loads engines in the background; poll state a few times.
        for (let i = 0; i < 6; i++) {
            await new Promise(res => setTimeout(res, 2000));
            await loadAddons();
            const active = addonsList.find(x => x.name === addonsActive);
            if (active && (active.state === 'loaded' || active.state === 'error')) break;
        }
    } catch (e) {
        addonsStatus('Save failed: ' + e.message, true);
        loadAddons();
    } finally {
        addonsSaving = false;
        if (addonsSaveQueued) { addonsSaveQueued = false; saveAddons(); }
    }
}

/** TTS test: speak the sample text with the CURRENT page settings (unsaved). */
async function addonsTestTts() {
    const text = (document.getElementById('ttsTestText') || {}).value || '';
    const lang = (document.getElementById('ttsTestLang') || {}).value || '';
    const player = document.getElementById('ttsTestPlayer');
    const status = document.getElementById('ttsTestStatus');
    if (!text.trim()) { if (status) status.textContent = 'Type some text first.'; return; }
    // If an auto-save is still pending (600 ms debounce), flush it first: the
    // server-side engine reads voices from addons.json, so a test fired before
    // the save would still hear the previous voice.
    if (addonsSaveTimer) { clearTimeout(addonsSaveTimer); addonsSaveTimer = null; await saveAddons(); }
    if (status) { status.textContent = 'Synthesizing (first run loads the model)...'; status.style.color = 'var(--warning, #f59e0b)'; }
    if (player) player.style.display = 'none';
    try {
        // (2026-11-26 fix) the test must use the voice the page shows: the endpoint
        // fell back to the per-language voice from addons.json, so switching the
        // voice dropdowns (unsaved) still heard the old voice. The test language
        // picks the voice; 'auto' uses the fallback language's voice.
        // (audit fix) when the provider is OpenAI the voice is the OpenAI dropdown
        // (alloy/nova/...) - sending a local name (M1/F1) makes the API 400.
        // OpenAI also auto-detects the language from the text, so no lang param.
        const provider = (document.getElementById('ttsProvider') || {}).value || 'supertonic';
        let voice = '';
        let langParam = lang;
        if (provider === 'openai') {
            voice = (document.getElementById('ttsOaVoice') || {}).value || 'alloy';
            langParam = '';
        } else if (lang) {
            // Explicit test language: use THAT language's voice dropdown.
            const voiceSel = document.getElementById('ttsVoice' + { en: 'En', de: 'De', ro: 'Ro' }[lang]);
            voice = voiceSel ? voiceSel.value : '';
        } else {
            // Auto-detect: mirror the server's fingerprint (RO diacritics,
            // eszett/umlauts) so the test ALWAYS sends the voice the user picked
            // for the detected language - even if detect_language is off
            // server-side (that was the "stuck to one male voice" bug: with
            // detection off the engine fell back to the EN voice for every text).
            const t = text.slice(0, 2000);
            let det = (document.getElementById('ttsFallback') || {}).value || 'en';
            if (/[\u0103\u00e2\u00ee\u0219\u021b]/.test(t)) det = 'ro';
            else if (/\u00df|[\u00e4\u00f6\u00fc]/.test(t)) det = 'de';
            langParam = det;
            const voiceSel = document.getElementById('ttsVoice' + { en: 'En', de: 'De', ro: 'Ro' }[det]);
            voice = voiceSel ? voiceSel.value : '';
        }
        const url = '/api/addons/speech?text=' + encodeURIComponent(text)
            + '&lang=' + encodeURIComponent(langParam)
            + '&voice=' + encodeURIComponent(voice);
        const r = await fetch(url);
        const ct = r.headers.get('content-type') || '';
        if (!r.ok) {
            const data = await r.json().catch(() => ({}));
            throw new Error((data && data.detail) || ('HTTP ' + r.status));
        }
        if (addonsAudioUrl) URL.revokeObjectURL(addonsAudioUrl);
        const blob = new Blob([await r.arrayBuffer()], { type: ct });
        // Play on the speaker the page selected ('' = system default).
        const speaker = (document.getElementById('ttsSpeaker') || {}).value || '';
        if (speaker && player && player.setSinkId) {
            try { await player.setSinkId(speaker); } catch (e) { /* default output */ }
        }
        addonsAudioUrl = URL.createObjectURL(blob);
        if (player) { player.src = addonsAudioUrl; player.style.display = ''; player.play().catch(() => {}); }
        if (status) { status.textContent = 'Done - lang: ' + (langParam || 'auto') + ', voice: ' + (voice || 'auto'); status.style.color = 'var(--success, #4caf50)'; }
    } catch (e) {
        if (status) { status.textContent = 'Test failed: ' + e.message; status.style.color = 'var(--danger)'; }
    }
}

/**
 * Fill a device dropdown (ttsSpeaker / sttMic) with the system's audio devices.
 * Browsers hide device labels until an audio permission has been granted, so the
 * first scan requests a throwaway mic stream (the user sees the normal permission
 * prompt), releases it, then re-enumerates. The value '' = "System default" is
 * always kept at the top and re-selected when the saved id no longer exists.
 */
async function addonsListDevices(kind) {
    const selId = kind === 'tts' ? 'ttsSpeaker' : 'sttMic';
    const sel = document.getElementById(selId);
    if (!sel) return;
    const kindLabel = kind === 'tts' ? 'speaker' : 'microphone';
    try {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.getTracks().forEach(t => t.stop());
        } catch (e) {
            // Permission denied: enumeration still works, but labels come back empty.
        }
        const devices = await navigator.mediaDevices.enumerateDevices();
        const wanted = kind === 'tts' ? 'audiooutput' : 'audioinput';
        const saved = sel.value;
        sel.innerHTML = '';
        const def = document.createElement('option');
        def.value = '';
        def.textContent = 'System default ' + kindLabel;
        sel.appendChild(def);
        let found = false;
        for (const d of devices.filter(x => x.kind === wanted)) {
            const o = document.createElement('option');
            o.value = d.deviceId;
            o.textContent = d.label || (kindLabel + ' ' + (d.deviceId.length > 8 ? d.deviceId.slice(0, 8) + '…' : d.deviceId));
            sel.appendChild(o);
            if (d.deviceId === saved) found = true;
        }
        sel.value = found ? saved : '';
    } catch (e) {
        sel.innerHTML = '<option value="">System default ' + kindLabel + '</option>';
        const status = document.getElementById('addonsStatus');
        if (status) { status.textContent = 'Device scan failed: ' + e.message; status.style.color = 'var(--danger)'; }
    }
}

/** STT test: record from the mic (or pick a file), transcribe, show the text. */
let addonsRecorder = null;

async function addonsTestSttFile(file) {
    const status = document.getElementById('sttTestStatus');
    const out = document.getElementById('sttTestText');
    if (!file) return;
    if (status) { status.textContent = 'Transcribing (first run loads the model)...'; status.style.color = 'var(--warning, #f59e0b)'; }
    if (out) out.value = '';
    try {
        const fd = new FormData();
        fd.append('file', file, file.name || 'recording.webm');
        const r = await fetch('/api/addons/transcribe', { method: 'POST', body: fd });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        if (out) out.value = data.text || '(empty transcript)';
        if (status) { status.textContent = 'Done - ' + (data.text || '').length + ' chars.'; status.style.color = 'var(--success, #4caf50)'; }
    } catch (e) {
        if (status) { status.textContent = 'Test failed: ' + e.message; status.style.color = 'var(--danger)'; }
    }
}

/** STT test: record from the microphone until the button is pressed again. */
async function addonsToggleSttRecord() {
    const btn = document.getElementById('sttRecordBtn');
    const status = document.getElementById('sttTestStatus');
    if (addonsRecorder) {
        addonsRecorder.stop();
        return;
    }
    try {
        // Record from the microphone the page selected ('' = system default).
        const mic = (document.getElementById('sttMic') || {}).value || '';
        const audio = mic ? { audio: { deviceId: { exact: mic } } } : { audio: true };
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia(audio);
        } catch (e) {
            if (mic) {
                // The saved device vanished (unplugged) - retry on the default mic.
                stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            } else {
                throw e;
            }
        }
        addonsRecorder = new MediaRecorder(stream);
        const chunks = [];
        addonsRecorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
        addonsRecorder.onstop = () => {
            stream.getTracks().forEach(t => t.stop());
            addonsRecorder = null;
            if (btn) { btn.textContent = '⏺ Record test'; btn.style.color = ''; }
            const blob = new Blob(chunks, { type: 'audio/webm' });
            addonsTestSttFile(new File([blob], 'recording.webm', { type: 'audio/webm' }));
        };
        addonsRecorder.start();
        if (btn) { btn.textContent = '⏹ Stop recording'; btn.style.color = 'var(--danger)'; }
        if (status) { status.textContent = 'Recording... speak now, then press Stop.'; status.style.color = 'var(--warning, #f59e0b)'; }
    } catch (e) {
        if (status) { status.textContent = 'Microphone unavailable: ' + e.message; status.style.color = 'var(--danger)'; }
    }
}

/** Called by index.html when the Addons tab is opened. */
window.addEventListener('DOMContentLoaded', () => {
    // The tab may already be the stored one when the modal opens; load eagerly either way.
    loadAddons();
});
