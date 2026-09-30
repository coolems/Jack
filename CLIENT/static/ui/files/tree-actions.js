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



function downloadSingle(path) {

    const a = document.createElement('a');

    const apiKey = localStorage.getItem('coolems_api_key') || '';

    const separator = path ? '&' : '?';

    const url = new URL('/api/download', window.location.origin);

    url.searchParams.set('path', path);

    if (apiKey) url.searchParams.set('api_key', apiKey);

    // FIX (2026-08-23): the auth middleware REQUIRES an email on every
    // authenticated request. Anchor clicks cannot send headers, so it must
    // travel as ?email= - without it /api/download answered 401 and Chrome
    // showed a 'download.json File wasn't available' error page instead of
    // saving the file. Same pattern openFileTree() already uses.
    const email = localStorage.getItem('coolems_email') || '';

    if (email) url.searchParams.set('email', email);

    a.href = url.toString();

    a.download = '';

    document.body.appendChild(a);

    a.click();

    document.body.removeChild(a);

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



function openFileTree(path) {

        // SECURITY (2026-08-25): /files/ is no longer public - open through the

        // authenticated download endpoint with inline=true so the browser renders it.

        const apiKey = localStorage.getItem('coolems_api_key') || '';

        const email = localStorage.getItem('coolems_email') || '';

        let fileUrl = '/api/download?path=' + encodeURIComponent(path) + '&inline=true';

        if (apiKey) fileUrl += '&api_key=' + encodeURIComponent(apiKey);

        if (email) fileUrl += '&email=' + encodeURIComponent(email);

        window.open(fileUrl, '_blank');

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



function openFile(filename) { 

    const apiKey = localStorage.getItem('coolems_api_key') || '';

    const url = new URL(`/api/open/${encodeURIComponent(filename)}`, window.location.origin);

    if (apiKey) url.searchParams.set('api_key', apiKey);

    window.open(url.toString(), '_blank'); 

}



function downloadFile(filename) { 

    const a = document.createElement('a'); 

    const apiKey = localStorage.getItem('coolems_api_key') || '';

    const url = new URL('/api/download', window.location.origin);

    url.searchParams.set('path', filename);

    if (apiKey) url.searchParams.set('api_key', apiKey);

    // FIX (2026-08-23): same mandatory-email gap as downloadSingle -
    // without ?email= the middleware rejects with 401.
    const email = localStorage.getItem('coolems_email') || '';

    if (email) url.searchParams.set('email', email);

    a.href = url.toString(); 

    a.download = filename; 

    document.body.appendChild(a); 

    a.click(); 

    document.body.removeChild(a); 

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
