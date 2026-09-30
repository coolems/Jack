/*
     * file-upload.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Drag-and-drop uploads into the prompt + input-area drop handler.

// ===== DRAG AND DROP FILE UPLOADS =====


async function handleDroppedFiles(files) {

    let addedCount = 0;

    for (let file of Array.from(files)) {

        if (file.size > 2 * 1024 * 1024 * 1024) { 

            showNotification(`File too large (max 2GB): ${file.name}`, 'error'); 

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

            addedCount++;

        } catch (e) { 

            showNotification(`Failed to upload ${file.name}`, 'error'); 

        }

    }

    if (addedCount > 0) { 

        updateFilePreview(); 

        showNotification(`Added ${addedCount} file(s)`, 'success'); 

    }

}



// Upload a file to a specific folder in the working root directory

async function uploadFileToFolder(file, folderPath) {

    if (file.size > 2 * 1024 * 1024 * 1024) {

        showNotification(`File too large (max 2GB): ${file.name}`, 'error');

        return false;

    }

    const formData = new FormData();

    formData.append('file', file);

    try {

        const url = folderPath

            ? `/api/upload-to-folder?folder_path=${encodeURIComponent(folderPath)}`

            : '/api/upload-to-folder';

        const r = await fetch(url, { method: 'POST', body: formData });

        if (!r.ok) throw new Error();

        const data = await r.json();

        showNotification(`Copied "${file.name}" to folder`, 'success');

        return true;

    } catch (e) {

        showNotification(`Failed to copy ${file.name}`, 'error');

        return false;

    }

}

// ===== INPUT AREA DROP HANDLER (with fix for double processing) =====



(function initInputAreaDrop() {

    const inputWrapper = document.getElementById('inputWrapper');

    if (!inputWrapper) return;



    inputWrapper.addEventListener('dragenter', (e) => {

        e.preventDefault();

        inputWrapper.classList.add('drag-over');

    });



    inputWrapper.addEventListener('dragleave', (e) => {

        if (!inputWrapper.contains(e.relatedTarget)) {

            inputWrapper.classList.remove('drag-over');

        }

    });



    inputWrapper.addEventListener('dragover', (e) => {

        e.preventDefault();

        e.dataTransfer.dropEffect = 'copy';

        inputWrapper.classList.add('drag-over');

    });



    inputWrapper.addEventListener('drop', async (e) => {

        e.preventDefault();

        inputWrapper.classList.remove('drag-over');

        

        // Prevent double processing

        if (isProcessingDrop) {

            console.log('[FILE-HANDLER] Drop already in progress, skipping duplicate');

            return;

        }

        

        isProcessingDrop = true;

        

        try {

            const data = e.dataTransfer.getData('text/plain');

            

            // Check if this is a file drag from our file tree (internal drag)

            if (data && e.dataTransfer.getData('coolems-file-drag') === 'true') {

                try {

                    const fileData = JSON.parse(data);

                    const textarea = document.getElementById('messageInput');

                    const currentValue = textarea.value;

                    const displayPath = fileData.filePath || fileData.fileName;

                    

                    const insertion = currentValue ? '\n📎 ' + displayPath + ' ' : '📎 ' + displayPath + ' ';

                    textarea.value = currentValue + insertion;

                    if (typeof autoResize === 'function') autoResize(textarea);

                    textarea.focus();

                    showNotification(`Added "${fileData.fileName}" to prompt`, 'success');

                } catch (err) {

                    console.error('Failed to parse dropped file data:', err);

                }

            } 

            // External files dropped from desktop

            else if (e.dataTransfer.files.length > 0) {

                await handleDroppedFiles(e.dataTransfer.files);

            }

        } finally {

            // Reset flag after a short delay to prevent any race conditions

            setTimeout(() => {

                isProcessingDrop = false;

            }, 500);

        }

    });

})();
