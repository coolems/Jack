/*
     * files-state.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Shared state for the file-handler module group.

// Tree view state

let currentTreePath = '';

let expandedFolders = new Set();

let selectedItems = new Set();

// Preview state

let currentPreviewPath = '';

let currentPreviewOriginalText = '';

let previewEditing = false;

// Flag to prevent double processing of drop events

let isProcessingDrop = false;

// ===== DRAG FILE TO PROMPT =====



// Text file extensions that can be previewed as text

const TEXT_EXTENSIONS = ['txt', 'md', 'json', 'xml', 'csv', 'html', 'htm', 'css', 'js', 'py', 'java', 'c', 'cpp', 'h', 'hpp', 'rb', 'go', 'rs', 'ts', 'tsx', 'jsx', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'sh', 'bat', 'ps1', 'log', 'env', 'lock', 'gitignore', 'dockerignore'];



// Image file extensions

const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico'];

const PDF_EXTENSIONS = ['pdf'];



// ===== ESCAPE HTML HELPER =====

function escapeHtml(text) {

    if (!text) return '';

    const div = document.createElement('div');

    div.textContent = text;

    return div.innerHTML;

}
