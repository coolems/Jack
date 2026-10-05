/*
     * tree-actions.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Select / download / delete / open actions + drag-to-prompt start.



function toggleSelect(itemPath, checked) {

    if (checked) {

        selectedItems.add(itemPath);

    } else {

        selectedItems.delete(itemPath);

    }

    updateDownloadSelectedBtn();

}



function updateDownloadSelectedBtn() {

    const btn = document.getElementById('downloadSelectedBtn');

    if (btn) {

        btn.style.display = selectedItems.size > 0 ? 'flex' : 'none';

        btn.textContent = `Download Selected (${selectedItems.size})`;

    }

}



async function downloadSingle(path) {

    // SECURITY fix 2026-10-05: anchor clicks cannot send headers, so the old code put the
    // master key + email in ?api_key=...&email=. Now we mint a short-lived, single-use,
    // path-bound media token via header auth (window.getMediaToken) and pass ?t=<token> -
    // the master key no longer appears in any URL. The 2026-08-23 mandatory-email gap is
    // gone with it: tokens carry no identity, so no ?email= round-trip either.

    try {

        if (typeof window.getMediaToken !== 'function') throw new Error('media token helper unavailable');

        const token = await window.getMediaToken(path);

        const url = new URL('/api/download', window.location.origin);

        url.searchParams.set('path', path);

        url.searchParams.set('t', token);

        const a = document.createElement('a');

        a.href = url.toString();

        a.download = '';

        document.body.appendChild(a);

        a.click();

        document.body.removeChild(a);

    } catch (e) {

        showNotification('Download failed: ' + e.message, 'error');

    }

}



async function downloadSelected() {

    const paths = Array.from(selectedItems);

    if (paths.length === 0) return;



    try {

        const apiKey = localStorage.getItem('coolems_api_key') || '';

        const headers = {'Content-Type': 'application/json'};

        if (apiKey) headers['X-API-Key'] = apiKey;

        const r = await fetch('/api/download-zip', {

            method: 'POST',

            headers: headers,

            body: JSON.stringify({paths: paths})

        });

        if (!r.ok) throw new Error('Download failed');



        const blob = await r.blob();

        const url = URL.createObjectURL(blob);

        const a = document.createElement('a');

        a.href = url;

        a.download = 'download.zip';

        document.body.appendChild(a);

        a.click();

        document.body.removeChild(a);

        URL.revokeObjectURL(url);

        showNotification('Download started', 'success');

    } catch (e) {

        showNotification('Failed to download', 'error');

    }

}



async function deleteItem(path) {

    const name = path.split('/').pop();

    // Immediate delete - no confirmation

    try {

        const r = await fetch(`/api/tree?path=${encodeURIComponent(path)}`, { method: 'DELETE' });

        if (r.ok) {

            showNotification(`Deleted ${name}`, 'success');

            loadTree(currentTreePath);

        } else throw new Error();

    } catch (e) {

        showNotification('Failed to delete', 'error');

    }

}



async function openFileTree(path) {

    // SECURITY (2026-08-25): /files/ is no longer public - open through the
    // authenticated download endpoint with inline=true so the browser renders it.
    // SECURITY fix 2026-10-05: short-lived path-bound media token (?t=) instead of
    // ?api_key=...&email= - the master key no longer appears in any URL.

    try {

        if (typeof window.getMediaToken !== 'function') throw new Error('media token helper unavailable');

        const token = await window.getMediaToken(path);

        const url = new URL('/api/download', window.location.origin);

        url.searchParams.set('path', path);

        url.searchParams.set('inline', 'true');

        url.searchParams.set('t', token);

        window.open(url.toString(), '_blank');

    } catch (e) {

        showNotification('Could not open file: ' + e.message, 'error');

    }

}



function getFileIcon(filename) {

    const ext = filename.split('.').pop().toLowerCase();

    const icons = {

        'png': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️', 'gif': '🖼️', 'svg': '🖼️', 'webp': '🖼️',

        'pdf': '📕',

        'txt': '📄', 'md': '📄', 'json': '📄', 'xml': '📄', 'csv': '📄',

        'html': '🌐', 'htm': '🌐', 'css': '🎨', 'js': '⚡', 'py': '🐍',

        'zip': '📦', 'rar': '📦', '7z': '📦',

        'mp4': '🎬', 'mov': '🎬', 'avi': '🎬',

        'mp3': '🎵', 'wav': '🎵',

    };

    return icons[ext] || '📄';

}



function refreshFiles() {

    const icon = document.getElementById('refreshIcon');

    if (icon) icon.classList.add('loading-spinner');

    loadTree(currentTreePath).then(() => setTimeout(() => {

        if (icon) icon.classList.remove('loading-spinner');

    }, 300));

}



async function openFile(filename) {

    // BUG FIX (2026-10-05): the old URL /api/open/<filename> matched NO server route
    // (only GET /api/open?path=... exists in read_endpoints.py) - every click 404'd.
    // SECURITY fix 2026-10-05: media token (?t=) instead of ?api_key=...

    try {

        if (typeof window.getMediaToken !== 'function') throw new Error('media token helper unavailable');

        const token = await window.getMediaToken(filename);

        const url = new URL('/api/open', window.location.origin);

        url.searchParams.set('path', filename);

        url.searchParams.set('t', token);

        window.open(url.toString(), '_blank');

    } catch (e) {

        showNotification('Could not open file: ' + e.message, 'error');

    }

}



async function downloadFile(filename) {

    // SECURITY fix 2026-10-05: media token (?t=) instead of ?api_key=...&email=
    // (the mandatory-email gap from the 2026-08-23 fix is gone - tokens carry no identity).

    try {

        if (typeof window.getMediaToken !== 'function') throw new Error('media token helper unavailable');

        const token = await window.getMediaToken(filename);

        const url = new URL('/api/download', window.location.origin);

        url.searchParams.set('path', filename);

        url.searchParams.set('t', token);

        const a = document.createElement('a');

        a.href = url.toString();

        a.download = filename;

        document.body.appendChild(a);

        a.click();

        document.body.removeChild(a);

    } catch (e) {

        showNotification('Download failed: ' + e.message, 'error');

    }

}



async function deleteFile(filename) {

    try {

        const r = await fetch(`/api/working-root-files/${encodeURIComponent(filename)}`, { method: 'DELETE' });

        if (r.ok) { 

            showNotification(`Deleted ${filename}`, 'success'); 

            loadTree(currentTreePath); 

        } else throw new Error();

    } catch (e) { 

        showNotification('Failed to delete file', 'error'); 

    }

}

function handleFileDragStart(event, filePath, fileName) {

    event.dataTransfer.setData('text/plain', JSON.stringify({filePath, fileName}));

    event.dataTransfer.effectAllowed = 'copy';

    event.dataTransfer.setData('coolems-file-drag', 'true');

}
