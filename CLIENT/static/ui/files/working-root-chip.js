/*
     * working-root-chip.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Working root header chip: load/edit/apply + DOMContentLoaded wiring.



// ===== WORKING ROOT MANAGEMENT (header chip - always visible, 2026-08-29) =====

    let currentWorkingRoot = null;

    let _workingRootChipEditing = false;



    function workingRootDisplayText(root) {

        // Show the LAST 20 characters of the working_root path.

        if (!root) return '\u2026';

        const clean = String(root).replace(/[/\\]+$/, '');

        return clean.length > 20 ? '...' + clean.slice(-20) : clean;

    }



    function updateWorkingRootChip() {

        const label = document.getElementById('workingRootChipLabel');

        if (!label) return;

        label.textContent = workingRootDisplayText(currentWorkingRoot);

        label.title = currentWorkingRoot || 'No working folder set';

    }



    async function loadWorkingRoot() {

        try {

            const r = await fetch('/api/working_root');

            const data = await r.json();

            if (data.working_root) {

                currentWorkingRoot = data.working_root;

            }

            updateWorkingRootChip();

        } catch(e) { console.warn('Failed to load working root:', e); }

    }



    function startWorkingRootEdit() {

        const chip = document.getElementById('workingRootChip');

        const input = document.getElementById('workingRootInput');

        if (!chip || !input || _workingRootChipEditing) return;

        _workingRootChipEditing = true;

        input.value = currentWorkingRoot || '';

        chip.classList.add('editing');

        setTimeout(() => { input.focus(); input.select(); }, 0);

    }



    function finishWorkingRootEdit(commit) {

        const chip = document.getElementById('workingRootChip');

        const input = document.getElementById('workingRootInput');

        if (!chip || !input || !_workingRootChipEditing) return;

        _workingRootChipEditing = false;

        chip.classList.remove('editing');

        if (commit) {

            const val = input.value.trim();

            if (val && val !== currentWorkingRoot) {

                applyWorkingRoot(val);

            } else {

                updateWorkingRootChip(); // revert display to committed value

            }

        }

    }



    // 2026-08-29: kept for backwards compatibility - the folder picker modal was removed,

    // changing the working folder is now done by clicking the chip in the header and typing.

    function browseAndApplyWorkingRoot() { startWorkingRootEdit(); }



    async function applyWorkingRoot(value) {

        const path = (value !== undefined ? value : null);

        if (!path || !String(path).trim()) return;

        const clean = String(path).trim();



        try {

            // Per-chat persistence (2026-08-31): tell the server which chat is open so it can

        // save this folder onto that conversation row - switching away and back restores it.

        const body = { working_root: clean };

        if (typeof COOLEMS !== 'undefined' && COOLEMS.currentConversation) {

            body.conversation_id = COOLEMS.currentConversation;

        }



        const r = await fetch('/api/working_root', {

                method: 'POST',

                headers: {'Content-Type': 'application/json'},

                body: JSON.stringify(body)

            });

            const data = await r.json();

            if (data.success) {

                currentWorkingRoot = data.working_root;

                updateWorkingRootChip();

                showNotification('Working folder updated', 'success');

                // Clear ALL state and force full refresh after short delay

                expandedFolders.clear();

                selectedItems.clear();

                currentTreePath = '';

                setTimeout(() => loadTree(''), 100);

            } else {

                updateWorkingRootChip(); // revert display to committed value

                showNotification(data.message || 'Failed to update working folder', 'error');

            }

        } catch(e) {

            updateWorkingRootChip();

            showNotification('Error updating working folder: ' + e, 'error');

        }

    }



    // Load working root on page load and wire up the header chip (click -> inline full-path edit)

    document.addEventListener('DOMContentLoaded', () => {

        setTimeout(loadWorkingRoot, 1000);



        const chip = document.getElementById('workingRootChip');

        if (!chip) return;



        // Click anywhere on the chip opens the inline editor showing the FULL path.

        chip.addEventListener('click', (e) => {

            e.stopPropagation();

            startWorkingRootEdit();

        });



        const input = document.getElementById('workingRootInput');

        if (input) {

            input.addEventListener('keydown', function(e) {

                if (e.key === 'Enter') {

                    e.preventDefault();

                    finishWorkingRootEdit(true);   // commit by typing + Enter

                } else if (e.key === 'Escape') {

                    e.preventDefault();

                    finishWorkingRootEdit(false);  // cancel, keep old value

                }

            });

            input.addEventListener('blur', () => finishWorkingRootEdit(true));

        }

    });
