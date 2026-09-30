/*
     * run-python.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Run a .py file from the tree and show its output in the preview panel.



// ===== RUN PYTHON FILE =====



let runPythonOutputPanel = null;



async function runPythonFile(path) {

    const filename = path.split('/').pop() || path.split('\\').pop();

    

    // Show loading notification

    showNotification(`Running ${filename}...`, 'info');

    

    try {

        const response = await fetch('/api/run-file', {

            method: 'POST',

            headers: { 'Content-Type': 'application/json' },

            body: JSON.stringify({ path: path })

        });

        

        if (!response.ok) {

            throw new Error(`HTTP ${response.status}`);

        }

        

        const data = await response.json();

        

        // Show output in preview panel

        showPythonOutput(data, filename);

        

    } catch (e) {

        showNotification(`Failed to run ${filename}: ${e.message}`, 'error');

    }

}



function showPythonOutput(data, filename) {

    const panel = document.getElementById('previewPanel');

    if (panel.classList.contains('collapsed')) {

        panel.classList.remove('collapsed');

    }



    const contentEl = document.getElementById('previewContent');

    const filenameEl = document.getElementById('previewFilename');



    if (filenameEl) filenameEl.textContent = `Output: ${filename}`;



    const hasOutput = data.stdout && data.stdout.trim().length > 0;

    const hasErrors = data.stderr && data.stderr.trim().length > 0;



    let statusClass, statusText;



    if (data.status === 'launched') {

        // GUI app was launched as independent process

        statusClass = 'status-success';

        statusText = '\u2705 GUI Launched';

    } else if (data.status === 'timeout') {

        statusClass = 'status-error';

        statusText = '\u23f1 Timed out';

    } else if (data.returncode === 0) {

        statusClass = 'status-success';

        statusText = '\u2713 Completed';

    } else {

        statusClass = 'status-error';

        statusText = `\u2717 Exit code: ${data.returncode}`;

    }



    let html = '<div class="python-output-container">';



    // Status bar

    html += `<div class="python-output-status ${statusClass}">${statusText}</div>`;



    // Output content

    if (hasOutput || hasErrors) {

        html += '<div class="python-output-content">';



        if (hasOutput) {

            html += '<div class="python-output-section">';

            html += '<div class="python-output-label">STDOUT</div>';

            html += `<pre class="python-output-text">${escapeHtml(data.stdout)}</pre>`;

            html += '</div>';

        }



        if (hasErrors) {

            html += '<div class="python-output-section python-output-error">';

            html += '<div class="python-output-label">STDERR</div>';

            html += `<pre class="python-output-text">${escapeHtml(data.stderr)}</pre>`;

            html += '</div>';

        }



        html += '</div>';

    } else if (data.status !== 'launched') {

        html += '<div class="python-output-empty">No output</div>';

    }



    html += '</div>';



    contentEl.innerHTML = html;



    // Store data for reference

    window._lastPythonOutput = data;

}
