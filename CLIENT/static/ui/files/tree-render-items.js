/*
     * tree-render-items.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Renders top-level tree items into HTML.



function renderTreeItems(items, currentPath) {

    const listEl = document.getElementById('filesList');

    if (!listEl) return;



    let html = '';

    for (const item of items) {

        const itemPath = currentPath ? currentPath + '/' + item.name : item.name;

        const isSelected = selectedItems.has(itemPath);



        if (item.type === 'folder') {

            const isExpanded = expandedFolders.has(itemPath);

            const arrow = isExpanded ? '▼' : '▶';

            html += `<div class="tree-item tree-folder" data-path="${escapeHtml(itemPath)}">`;

            html += `<div class="tree-item-main" onclick="toggleFolder('${escapeHtml(itemPath).replace(/'/g, "\\'")}')">`;

            html += `<span class="tree-arrow">${arrow}</span>`;

            html += `<span class="tree-icon">📁</span>`;

            html += `<span class="tree-name">${escapeHtml(item.name)}</span>`;

            html += `<span class="tree-meta">folder</span>`;

            html += `</div>`;

            html += `<div class="tree-item-actions">`;

            html += `<button class="tree-action-btn" onclick="downloadSingle('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Download"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"></path></svg></button>`;

            html += `<button class="tree-action-btn delete" onclick="deleteItem('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Delete"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>`;

            html += `<input type="checkbox" class="tree-checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelect('${escapeHtml(itemPath).replace(/'/g, "\\'")}', this.checked)">`;

            html += `</div></div>`;



            // Render children if expanded and available

            if (isExpanded && item.items && item.items.length > 0) {

                html += `<div class="tree-children" data-parent="${escapeHtml(itemPath)}">`;

                html += renderChildrenItems(item.items, itemPath);

                html += `</div>`;

            } else if (isExpanded && (!item.items || item.items.length === 0)) {

                html += `<div class="tree-children" data-parent="${escapeHtml(itemPath)}">`;

                html += `<div class="tree-empty-children">Empty folder</div>`;

                html += `</div>`;

            }

        } else {

            // File - draggable to prompt

            const isHtml = item.name.toLowerCase().endsWith('.html') || item.name.toLowerCase().endsWith('.htm');

            html += `<div class="tree-item tree-file" data-path="${escapeHtml(itemPath)}">`;

            html += `<div class="tree-item-main tree-file-draggable" draggable="true" `;

            html += `onclick="previewFile('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" `;

            html += `ondragstart="handleFileDragStart(event, '${escapeHtml(itemPath).replace(/'/g, "\\'")}', '${escapeHtml(item.name).replace(/'/g, "\\'")}')" `;

            html += `title="Drag to prompt or click to preview">`;

            html += `<span class="tree-arrow"></span>`;

            html += `<span class="tree-icon">${getFileIcon(item.name)}</span>`;

            html += `<span class="tree-name">${escapeHtml(item.name)}</span>`;

            html += `<span class="tree-meta">${formatFileSize(item.size)}</span>`;

            html += `<span class="tree-drag-hint" title="Drag to prompt">⠿</span>`;

            html += `</div>`;

            html += `<div class="tree-item-actions">`;

            const isPy = item.name.toLowerCase().endsWith('.py');

            if (isHtml) {

                html += `<button class="tree-action-btn open" onclick="openFileTree('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Open"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg></button>`;

            }

            if (isPy) {

                html += `<button class="tree-action-btn run" onclick="runPythonFile('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Run Python"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg></button>`;

            }

            html += `<button class="tree-action-btn" onclick="downloadSingle('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Download"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"></path></svg></button>`;

            html += `<button class="tree-action-btn delete" onclick="deleteItem('${escapeHtml(itemPath).replace(/'/g, "\\'")}')" title="Delete"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>`;

            html += `<input type="checkbox" class="tree-checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelect('${escapeHtml(itemPath).replace(/'/g, "\\'")}', this.checked)">`;

            html += `</div></div>`;

        }

    }

    listEl.innerHTML = html;

}
