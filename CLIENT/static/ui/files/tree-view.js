// Tree loading, breadcrumbs and folder expand/collapse.

let filesTreeRevision = 0;
let filesTreeRenderedRevision = 0;
let filesTreeWorkspace = null;
const pendingFolderLoads = new WeakMap();

async function fetchFileTreeItems(path, conversation) {
    const query = new URLSearchParams({ path, conversation_id: conversation, shallow: 'true' });
    const response = await fetch('/api/tree?' + query, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Files request failed (${response.status})`);
    const data = await response.json();
    if (!Array.isArray(data.items)) throw new Error('Invalid files listing');
    return data.items;
}

async function loadTree(path = '') {
    path = path || '';
    const sameFolder = path === currentTreePath;
    currentTreePath = path;
    const revision = ++filesTreeRevision;
    const conversation = currentConvId();
    const isCurrent = () => revision === filesTreeRevision && conversation === currentConvId();
    const list = document.getElementById('filesList');
    if (!list) return false;
    if (!conversation) {
        renderBreadcrumbs('');
        list.innerHTML = '<div class="files-empty">Open a workspace to browse its files</div>';
        return false;
    }
    try {
        const items = await fetchFileTreeItems(path, conversation);
        if (!isCurrent()) return false;
        const scrollTop = sameFolder ? list.scrollTop : 0;
        pruneMissingTreeSelections(path, items);
        renderBreadcrumbs(path);
        if (items.length) renderTreeItems(items, path);
        else list.innerHTML = '<div class="files-empty">Empty folder</div>';
        filesTreeRenderedRevision = revision;
        filesTreeWorkspace = conversation;
        // Each response lists one level. Rehydrate every expanded branch, not just
        // the first level (whose rows otherwise claim to be open with no children).
        await restoreExpandedFolders(list);
        if (!isCurrent()) return false;
        list.scrollTop = scrollTop;
        updateDownloadSelectedBtn();
        return true;
    } catch (error) {
        if (isCurrent()) {
            renderBreadcrumbs(path);
            list.innerHTML = '<div class="files-empty"><p style="color:var(--danger);">Error loading files</p></div>';
        }
        return false;
    }
}

function getWorkingFolderName() {
    const root = (typeof currentWorkingRoot !== 'undefined' && currentWorkingRoot) || '';
    return String(root).split(/[/\\]/).filter(Boolean).pop() || 'Files';
}

function renderBreadcrumbs(currentPath) {
    const breadcrumb = document.getElementById('treeBreadcrumbs');
    if (!breadcrumb) return;
    const root = escapeHtml(getWorkingFolderName());
    if (!currentPath) {
        breadcrumb.innerHTML = `<span class="breadcrumb-item active">${root}</span>`;
        return;
    }
    let html = `<span class="breadcrumb-item" onclick="loadTree('')">${root}</span>`;
    const parts = currentPath.split('/');
    let path = '';
    for (const [index, part] of parts.entries()) {
        path = path ? path + '/' + part : part;
        const active = index === parts.length - 1 ? 'active' : '';
        html += ` <span class="breadcrumb-sep">›</span> `;
        html += `<span class="breadcrumb-item ${active}" onclick="loadTree('${escapeJsAttr(path)}')">${escapeHtml(part)}</span>`;
    }
    breadcrumb.innerHTML = html;
}

function fileTreeRow(path, selector = '.tree-item') {
    if (filesTreeRenderedRevision !== filesTreeRevision || filesTreeWorkspace !== currentConvId()) return null;
    return document.getElementById('filesList')?.querySelector(`${selector}[data-path="${CSS.escape(path)}"]`);
}

function pruneMissingTreeSelections(folder, items) {
    const names = new Set(items.map(item => item.name));
    const prefix = folder ? folder + '/' : '';
    for (const path of selectedItems) {
        if (!path.startsWith(prefix) || path === folder) continue;
        const name = path.slice(prefix.length).split('/')[0];
        if (!names.has(name)) selectedItems.delete(path);
    }
    updateDownloadSelectedBtn();
}

function updateFolderArrow(row, expanded) {
    const arrow = row.querySelector(':scope > .tree-item-main .tree-arrow');
    if (arrow) arrow.textContent = expanded ? '▼' : '▶';
}

function restoreExpandedFolders(container, guard) {
    return Promise.all(Array.from(container.children)
        .filter(row => row.classList.contains('tree-folder') && expandedFolders.has(row.dataset.path))
        .map(row => expandFileTreeFolder(row.dataset.path, guard)));
}

async function expandFileTreeFolder(path, guard = () => true) {
    const row = fileTreeRow(path, '.tree-folder');
    if (!row || !guard()) return false;
    if (row.querySelector(':scope > .tree-children')) return true;
    const pending = pendingFolderLoads.get(row);
    if (pending) {
        return (await pending) && guard();
    }
    const conversation = currentConvId();
    const revision = filesTreeRevision;
    expandedFolders.add(path);
    updateFolderArrow(row, true);
    const load = (async () => {
        try {
            const items = await fetchFileTreeItems(path, conversation);
            if (pendingFolderLoads.get(row) !== load || !guard() || !expandedFolders.has(path)
                || fileTreeRow(path, '.tree-folder') !== row) return false;
            pruneMissingTreeSelections(path, items);
            const children = document.createElement('div');
            children.className = 'tree-children';
            children.innerHTML = items.length ? renderChildrenItems(items, path)
                : '<div class="tree-empty-children">Empty folder</div>';
            row.appendChild(children);
            await restoreExpandedFolders(children, guard);
            return true;
        } catch (error) {
            if (pendingFolderLoads.get(row) === load && revision === filesTreeRevision) {
                console.error('Failed to load folder contents:', error);
            }
            return false;
        }
    })();
    pendingFolderLoads.set(row, load);
    try {
        return await load;
    } finally {
        if (pendingFolderLoads.get(row) === load) {
            pendingFolderLoads.delete(row);
            if (!row.querySelector(':scope > .tree-children') && revision === filesTreeRevision) {
                expandedFolders.delete(path);
                updateFolderArrow(row, false);
            }
        }
    }
}

async function toggleFolder(path) {
    const row = fileTreeRow(path, '.tree-folder');
    if (!row) return;
    if (expandedFolders.has(path)) {
        expandedFolders.delete(path);
        pendingFolderLoads.delete(row);
        row.querySelector(':scope > .tree-children')?.remove();
        updateFolderArrow(row, false);
    } else {
        await expandFileTreeFolder(path);
    }
}
