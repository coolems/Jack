/*
     * file-chips.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Prompt file chips: select, preview list, remove.



function formatFileSize(bytes) { 

    if (bytes === 0) return '0 B'; 

    const k = 1024; 

    const s = ['B','KB','MB','GB']; 

    const i = Math.floor(Math.log(bytes) / Math.log(k)); 

    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + s[i]; 

}



async function handleFileSelect(event) {

    for (let file of Array.from(event.target.files)) {

        if (file.size > 2 * 1024 * 1024 * 1024) { 

            showNotification('File too large (max 2GB)', 'error'); 

            continue; 

        }

        const formData = new FormData(); 

        formData.append('file', file);

        try {

            const r = await fetch('/api/upload', { method: 'POST', body: formData });

            if (!r.ok) throw new Error();

            const data = await r.json();

            COOLEMS.selectedFiles.push({ 

                name: file.name, 

                url: data.url, 

                content: data.content || null, 

                is_text: data.is_text, 

                is_image: data.is_image 

            });

            updateFilePreview();

        } catch (e) { 

            showNotification('Failed to upload ' + file.name, 'error'); 

        }

    }

    event.target.value = '';

}



function updateFilePreview() {

    const previewEl = document.getElementById('filePreview');

    if (!previewEl) return;

    

    previewEl.innerHTML = COOLEMS.selectedFiles.map(f => {

        let tagClass = '', icon = '';

        if (f.is_image) { 

            tagClass = 'file-tag-image'; 

            icon = '🖼️ '; 

        } else if (f.is_text) { 

            tagClass = 'file-tag-text'; 

            icon = '📄 '; 

        }

        return '<div class="file-tag ' + tagClass + '">' + icon + escapeHtml(f.name) + '<span class="file-tag-remove" onclick="removeFile(\'' + f.url + '\')">×</span></div>';

    }).join('');

}



function removeFile(url) { 

    COOLEMS.selectedFiles = COOLEMS.selectedFiles.filter(f => f.url !== url); 

    updateFilePreview(); 

}
