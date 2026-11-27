/**
 * workspace-note.js - per-workspace "info" note.
 *
 * A workspace row carries one free-text note a human keeps for that workspace (setup
 * commands, conventions, "where the logs are"...). It is opened from the sidebar row menu
 * ("Edit workspace info") and edited in a small modal that also NAMES the workspace being
 * edited, so text can never be saved onto the wrong row.
 *
 * Where the note lives: conversations.workspace_note, a column of the workspace's OWN row.
 * Saving therefore behaves like a normal save - it just targets the workspace record - and
 * deleting the workspace deletes the note with it (no orphan note records anywhere).
 * Server side: app/routers/conversations.py -> GET/POST /api/conversations/{id}/note.
 *
 * Loaded before conversation-manager.js in index.html (that file only calls into it from a
 * menu click, never at load time), and before message.js - showNotification is used on
 * click/save only.
 */

/** Same limit as NOTE_MAX_CHARS in app/routers/conversations.py (the server refuses more). */
const WORKSPACE_NOTE_MAX = 20000;

const WorkspaceNote = {
    /** Workspace the open editor belongs to (null while the modal is closed). */
    id: null,
    /** Title shown in the editor header - what the note is being saved FOR. */
    title: '',
    /** Last value the SERVER has, i.e. what the textarea is compared against. */
    saved: '',
    saving: false,
    /** Guards against a slow GET landing after the user moved to another workspace. */
    requestSeq: 0,
    bound: false,

    /**
     * Open the note editor for a workspace id.
     * Fetches title + note first: the modal only appears once it can show real data, and a
     * workspace deleted in the meantime reports its error instead of opening empty.
     */
    async open(id) {
        if (!id) return;
        const seq = ++this.requestSeq;
        try {
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}/note`);
            if (!response.ok) throw new Error(await _workspaceNoteError(response));
            const data = await response.json();
            if (seq !== this.requestSeq) return;    // superseded by a newer open()/close()
            this._mount(data.id || id, data.title || '', data.note || '');
        } catch (e) {
            showNotification(`Could not open workspace info: ${e.message}`, 'error');
        }
    },

    /** Show the modal with one workspace's note. */
    _mount(id, title, note) {
        const modal = _workspaceNoteModal();
        if (!modal) return;
        this._bind(modal);
        this.id = id;
        this.title = title || 'New Workspace';
        this.saved = note;
        this.saving = false;

        const name = document.getElementById('workspaceNoteTarget');
        const targetId = document.getElementById('workspaceNoteTargetId');
        const input = document.getElementById('workspaceNoteInput');
        const state = document.getElementById('workspaceNoteState');
        if (name) {
            name.textContent = this.title;
            name.title = this.title;
        }
        if (targetId) targetId.textContent = `#${id}`;
        if (input) input.value = note;
        if (state) state.textContent = note ? 'Saved with this workspace' : 'No info yet';
        this._setBusy(false);
        this._syncCounter();

        modal.classList.add('active');
        if (input) {
            input.focus();
            // Read from the top: a caret parked at the end would scroll a long note down.
            input.setSelectionRange(0, 0);
            input.scrollTop = 0;
        }
    },

    /**
     * Save the note to the workspace record, then close.
     * The textarea is read as-is (no trim): indentation and blank lines are part of the note.
     */
    async save() {
        const input = document.getElementById('workspaceNoteInput');
        if (!this.id || this.saving || !input) return;
        const note = input.value;
        if (note.length > WORKSPACE_NOTE_MAX) {
            showNotification(
                `Workspace info is limited to ${WORKSPACE_NOTE_MAX} characters`, 'warning');
            return;
        }

        this.saving = true;
        this._setBusy(true);
        try {
            const response = await fetch(
                `/api/conversations/${encodeURIComponent(this.id)}/note`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ note }),
                });
            if (!response.ok) throw new Error(await _workspaceNoteError(response));
            showNotification(`Workspace info saved for "${this.title}"`, 'success');
            this.close();
        } catch (e) {
            showNotification(`Failed to save workspace info: ${e.message}`, 'error');
            this._setBusy(false);
            this.saving = false;
        }
    },

    /** Close without saving (Cancel / Esc / backdrop / after a successful save). */
    close() {
        this.requestSeq++;
        const modal = _workspaceNoteModal();
        if (modal) modal.classList.remove('active');
        this.id = null;
        this.title = '';
        this.saved = '';
        this.saving = false;
        // A successful save closes while the buttons are still in their busy state; reset it
        // here so the dialog never holds a disabled "Saving..." button between opens.
        this._setBusy(false);
    },

    /** One-time listeners: live counter, Ctrl/Cmd+Enter to save, Esc and backdrop to close. */
    _bind(modal) {
        if (this.bound) return;
        this.bound = true;
        const input = document.getElementById('workspaceNoteInput');

        if (input) {
            input.addEventListener('input', () => {
                this._syncCounter();
                const state = document.getElementById('workspaceNoteState');
                if (state) {
                    state.textContent = input.value === this.saved ? 'Saved with this workspace' : 'Unsaved changes';
                }
            });
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    this.save();
                }
            });
        }

        // Esc is caught on DOCUMENT in the CAPTURE phase, not on the box: a listener on the
        // box only sees keys pressed while one of its children has focus, so Esc did nothing
        // after a click on the header/backdrop. Capture also means the app-wide Esc handlers
        // (settings, sidebar menus - all registered on document in the bubble phase) never
        // run behind an open dialog. Ignored while a save is in flight: the Cancel button is
        // disabled then too, and closing would strand the "saved for ..." notification.
        document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape' || !this.id || this.saving) return;
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }, true);
        // Backdrop click closes; a click INSIDE the box must never dismiss the editor.
        modal.addEventListener('mousedown', (event) => {
            if (event.target === modal) this.close();
        });
    },

    /** "123 / 20000" under the textarea. */
    _syncCounter() {
        const input = document.getElementById('workspaceNoteInput');
        const count = document.getElementById('workspaceNoteCount');
        if (!input || !count) return;
        count.textContent = `${input.value.length} / ${WORKSPACE_NOTE_MAX}`;
        count.classList.toggle('over', input.value.length > WORKSPACE_NOTE_MAX);
    },

    /** Disable the buttons (and label the save) while a write is in flight. */
    _setBusy(busy) {
        const save = document.getElementById('workspaceNoteSave');
        const cancel = document.getElementById('workspaceNoteCancel');
        if (save) {
            save.disabled = !!busy;
            save.textContent = busy ? 'Saving...' : 'Save';
        }
        if (cancel) cancel.disabled = !!busy;
    },
};

/** The modal element (markup lives in index.html next to the settings dialog). */
function _workspaceNoteModal() {
    return document.getElementById('workspaceNoteModal');
}

/** Detail text of a failed response, whatever shape the error came back in. */
async function _workspaceNoteError(response) {
    try {
        const body = await response.json();
        if (body && body.detail) return body.detail;
    } catch (e) { /* not JSON - fall through to the status code */ }
    return `HTTP ${response.status}`;
}

window.WorkspaceNote = WorkspaceNote;
