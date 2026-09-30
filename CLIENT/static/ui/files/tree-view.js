/*
     * tree-view.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Tree loading, breadcrumbs and folder expand/collapse.

// ===== TREE VIEW FUNCTIONS =====



async function loadTree(path) {

    if (path === undefined || path === null) path = '';

    currentTreePath = path;

    const listEl = document.getElementById('filesList');

    try {

        const ts = Date.now();

            const url = path 

                ? `/api/tree?path=${encodeURIComponent(path)}&t=${ts}` 

                : `/api/tree?t=${ts}`;

        const r = await fetch(url);

        const data = await r.json();

        if (data.items && data.items.length > 0) {

            renderBreadcrumbs(data.path);

            renderTreeItems(data.items, data.path);

            updateDownloadSelectedBtn();

        } else {

            renderBreadcrumbs(data.path);

            listEl.innerHTML = '<div class="files-empty"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg><p>Empty folder</p></div>';

            updateDownloadSelectedBtn();

        }

    } catch (e) {

        listEl.innerHTML = '<div class="files-empty"><p style="color:var(--danger);">Error loading files</p></div>';

    }

}



// FIX (2026-08-29): getWorkingFolderName was DELETED with the old Files-tab working-root
    // bar during the header-chip rework, but renderBreadcrumbs() still calls it. The resulting
    // ReferenceError landed in loadTree()'s catch block and showed "Error loading files" even
    // though /api/tree returned 200 with items. Restored here: shows the last path segment of
    // the current working root (the folder name) for the breadcrumb root label.
    function getWorkingFolderName() {
        const root = (typeof currentWorkingRoot !== 'undefined' && currentWorkingRoot) ? String(currentWorkingRoot) : '';
        if (!root) return 'Files';
        const clean = root.replace(/[/\\]+$/, '');
        const parts = clean.split(/[/\\]/);
        return parts[parts.length - 1] || 'Files';
    }

    function renderBreadcrumbs(currentPath) {

    const breadcrumbEl = document.getElementById('treeBreadcrumbs');

    if (!breadcrumbEl) return;



    const rootName = getWorkingFolderName();



    if (!currentPath) {

        breadcrumbEl.innerHTML = `<span class="breadcrumb-item active">${escapeHtml(rootName)}</span>`;

        return;

    }



    let html = `<span class="breadcrumb-item" onclick="loadTree('', this)">${escapeHtml(rootName)}</span>`;

    const parts = currentPath.split('/');

    let buildPath = '';

    for (let i = 0; i < parts.length; i++) {

        buildPath += (buildPath ? '/' : '') + parts[i];

        const isLast = i === parts.length - 1;

        html += ` <span class="breadcrumb-sep">›</span> `;

        html += `<span class="breadcrumb-item ${isLast ? 'active' : ''}" onclick="loadTree('${buildPath.replace(/'/g, "\\'")}', this)">${escapeHtml(parts[i])}</span>`;

    }

    breadcrumbEl.innerHTML = html;

}



async function toggleFolder(folderPath) {

    const itemEl = document.querySelector(`.tree-item[data-path="${CSS.escape(folderPath)}"]`);

    if (!itemEl) return;



    if (expandedFolders.has(folderPath)) {

        expandedFolders.delete(folderPath);

        // Remove children container

        const children = itemEl.querySelector('.tree-children');

        if (children) children.remove();

    } else {

        expandedFolders.add(folderPath);

        // Remove existing children if any

        const existing = itemEl.querySelector('.tree-children');

        if (existing) existing.remove();



        // Fetch children from server

        try {

            const ts = Date.now();

                const url = `/api/tree?path=${encodeURIComponent(folderPath)}&t=${ts}`;

            const r = await fetch(url);

            const data = await r.json();

            

            const childrenDiv = document.createElement('div');

            childrenDiv.className = 'tree-children';

            childrenDiv.setAttribute('data-parent', folderPath);

            

            if (data.items && data.items.length > 0) {

                childrenDiv.innerHTML = renderChildrenItems(data.items, folderPath);

            } else {

                childrenDiv.innerHTML = '<div class="tree-empty-children">Empty folder</div>';

            }

            itemEl.appendChild(childrenDiv);

        } catch (e) {

            console.error('Failed to load folder contents:', e);

        }

    }



    // Update arrow

    const arrowEl = itemEl.querySelector('.tree-arrow');

    if (arrowEl) {

        arrowEl.textContent = expandedFolders.has(folderPath) ? '▼' : '▶';

    }

}
