/**
 * message.js - Message rendering, markdown parsing, copy, edit functionality.
 * Uses COOLEMS shared state from app.js.
 */

        async function copyMessage(btn) {
            const raw = btn.closest('.message-content').getAttribute('data-raw-content');
            if (!raw) { showNotification('Nothing to copy', 'warning'); return; }
            try {
                if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(raw); }
                else { const ta = document.createElement('textarea'); ta.value = raw; ta.style.cssText = 'position:fixed;left:-999999px'; document.body.appendChild(ta); ta.select(); if (!document.execCommand('copy')) throw new Error(); document.body.removeChild(ta); }
                const orig = btn.innerHTML; btn.classList.add('copied');
                btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg> Copied!';
                setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = orig; }, 2000);
            } catch (e) { showNotification('Failed to copy', 'error'); }
        }



        function startEditMessage(btn) {
            const messageDiv = btn.closest('.message');
            const messageContent = btn.closest('.message-content');
            const rawContent = messageContent.getAttribute('data-raw-content');
            if (!rawContent) { showNotification('Cannot edit this message', 'warning'); return; }
            if (COOLEMS.isStreaming) { showNotification('Please wait', 'warning'); return; }
            messageDiv.classList.add('editing');
            const textarea = document.createElement('textarea'); textarea.className = 'edit-textarea'; textarea.rows = 3; textarea.value = rawContent;
            const actionsDiv = document.createElement('div'); actionsDiv.className = 'edit-actions';
            actionsDiv.innerHTML = '<button class="edit-save-btn" onclick="saveEditMessage(this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:4px"><polyline points="20 6 9 17 4 12"></polyline></svg> Save & Submit</button><button class="edit-cancel-btn" onclick="cancelEditMessage(this)">Cancel</button>';
            const contentText = messageContent.querySelector('.content-text');
            contentText.parentNode.insertBefore(textarea, contentText);
            contentText.parentNode.insertBefore(actionsDiv, textarea.nextSibling);
            // Auto-grow the edit box to fit content (a few lines up, then scroll), and re-fit on window resize
            const autoGrowEdit = () => { textarea.style.height = 'auto'; textarea.style.height = Math.min(textarea.scrollHeight, 320) + 'px'; };
            requestAnimationFrame(autoGrowEdit); setTimeout(autoGrowEdit, 50);
            const onWinResize = () => autoGrowEdit(); window.addEventListener('resize', onWinResize);
            messageDiv._editCleanup = () => { window.removeEventListener('resize', onWinResize); const t2 = messageContent.querySelector('.edit-textarea'); const a2 = messageContent.querySelector('.edit-actions'); if (t2) t2.remove(); if (a2) a2.remove(); };
textarea.focus(); textarea.setSelectionRange(textarea.value.length, textarea.value.length);
            textarea.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEditMessage(actionsDiv.querySelector('.edit-save-btn')); } if (e.key === 'Escape') cancelEditMessage(actionsDiv.querySelector('.edit-cancel-btn')); });
        }



        function saveEditMessage(btn) {
            const messageDiv = btn.closest('.message'); const messageContent = btn.closest('.message-content');
            const newContent = messageContent.querySelector('.edit-textarea').value.trim();
            if (!newContent) { showNotification('Message cannot be empty', 'warning'); return; }
            messageDiv.classList.remove('editing');
            const ta = messageContent.querySelector('.edit-textarea'); const act = messageContent.querySelector('.edit-actions');
            if (messageDiv._editCleanup) { messageDiv._editCleanup(); delete messageDiv._editCleanup; } else { if (ta) ta.remove(); if (act) act.remove(); }
            editAndResend(parseInt(messageDiv.dataset.messageIndex), newContent);
        }



        function cancelEditMessage(btn) {
            const messageDiv = btn.closest('.message'); const messageContent = btn.closest('.message-content');
            messageDiv.classList.remove('editing');
            const ta = messageContent.querySelector('.edit-textarea'); const act = messageContent.querySelector('.edit-actions');
            if (messageDiv._editCleanup) { messageDiv._editCleanup(); delete messageDiv._editCleanup; } else { if (ta) ta.remove(); if (act) act.remove(); }
        }



        function parseMarkdown(text, isStreaming = false) {
            if (!text) return '';
            
            const placeholders = [];
            function store(html) {
                const idx = placeholders.length;
                placeholders.push(html);
                return '\x00' + idx + '\x00';
            }
            
            text = text.replace(/```[\w]*\n([\s\S]*?)```/g, function(match, code) {
                return store('<pre><code>' + escapeHtml(code) + '</code></pre>');
            });
            
            if (COOLEMS.isStreaming) {
                text = text.replace(/```[\w]*\n([\s\S]*)$/g, function(match, code) {
                    return store('<pre class="streaming-code"><code>' + escapeHtml(code) + '</code></pre>');
                });
            }
            
            text = text.replace(/`([^`]+)`/g, function(match, code) {
                return store('<code>' + escapeHtml(code) + '</code>');
            });
            
            let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            
            html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
            
            html = html.replace(/(?<!["'])((?:https?:\/\/)[^\s<>"']+(?:\.[^\s<>"']+)*)/gi, function(url) {
                let cleanUrl = url, trailing = '';
                const m = url.match(/[.,;:!?\)\]]+$/);
                if (m) { trailing = m[0]; cleanUrl = url.slice(0, -m[0].length); }
                return '<a href="' + cleanUrl + '" target="_blank" rel="noopener noreferrer" class="chat-link">' + cleanUrl + '</a>' + trailing;
            });
            
            html = html.split(/\n\n+/).map(function(p) {
                if (p.trim() === '' || p.indexOf('<pre>') !== -1) return p;
                return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
            }).join('');
            
            for (let i = 0; i < placeholders.length; i++) {
                html = html.split('\x00' + i + '\x00').join(placeholders[i]);
            }
            
            return html;
        }



        function showNotification(message, type = 'info') {
            const existing = document.querySelector('.notification');
            if (existing) existing.remove();
            const div = document.createElement('div');
            div.className = 'notification notification-' + type;
            div.textContent = message;
            document.body.appendChild(div);
            setTimeout(() => { div.style.opacity = '0'; setTimeout(() => div.remove(), 300); }, 5000);
        }



        async function sendMessage() {
            // Clear any error bar on new send
            
            const input = document.getElementById('messageInput');
            const message = input.value.trim();
            if (!message && COOLEMS.selectedFiles.length === 0) return;
            if (COOLEMS.isStreaming) { showNotification('Please wait for current response...', 'warning'); return; }
            if (!COOLEMS.currentConversation) { await newChat(); await new Promise(r => setTimeout(r, 500)); }
            
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            hideScrollIndicator();
            
            const fileContentsForDisplay = COOLEMS.selectedFiles.filter(f => f.content).map(f => ({ filename: f.name, content: f.content, type: 'text' }));
            appendMessage('user', message, false, COOLEMS.selectedFiles.map(f => f.url), fileContentsForDisplay);
            input.value = ''; input.style.height = 'auto'; document.getElementById('filePreview').innerHTML = '';
            // (2026-09-08 multi-chat UI fix) a user send is always LIVE: leave replay mode so
            // the incoming stream of THIS turn renders normally.
            COOLEMS._inReplay = false;
            COOLEMS.isStreaming = true; COOLEMS.streamingBuffer = ''; setButtonState(true);
            const payload = { 
                message, 
                model: document.getElementById('modelSelector').value, 
                agent_mode: COOLEMS.agentMode, 
                media_files: COOLEMS.selectedFiles.map(f => f.url), 
                enable_thinking: COOLEMS.enableThinking
            };
            COOLEMS.selectedFiles = [];
            // (2026-09-08 multi-chat) send on THIS chat's pooled socket; if it is not open
            // yet, wait briefly for the handshake instead of the old fixed 1s timeout.
            const _sendWs = () => ChatSocketPool.isOpen(COOLEMS.currentConversation)
                ? ChatSocketPool.sockets[COOLEMS.currentConversation].ws : null;
            if (_sendWs() && _sendWs().readyState === WebSocket.OPEN) {
                _sendWs().send(JSON.stringify(payload));
            } else {
                ChatSocketPool.ensure(COOLEMS.currentConversation);
                let _tries = 0;
                const _waitOpen = () => {
                    _tries++;
                    const w = _sendWs();
                    if (w && w.readyState === WebSocket.OPEN) { w.send(JSON.stringify(payload)); return; }
                    if (_tries >= 50) { // ~5s budget
                        showNotification('Connection not available. Please retry.', 'error');
                        COOLEMS.isStreaming = false; setButtonState(false);
                        return;
                    }
                    setTimeout(_waitOpen, 100);
                };
                _waitOpen();
            }
        }



        // Build an authenticated inline URL for a saved media file (working_root-relative).
            // Mirrors the pattern used by preview.js: key/email travel as query params because
            // <img> tags cannot send auth headers; server validates them either way.
            function buildMediaUrl(url) {
                const rel = String(url || '').replace(/^\/files\//, '');
                let src = '/api/download?path=' + encodeURIComponent(rel) + '&inline=true';
                const apiKey = localStorage.getItem('coolems_api_key') || '';
                const email = localStorage.getItem('coolems_email') || '';
                if (apiKey) src += '&api_key=' + encodeURIComponent(apiKey);
                if (email) src += '&email=' + encodeURIComponent(email);
                return src;
            }

            // Click handler: open the full-size image in a new tab.
            function openMessageImage(img) { window.open(img.getAttribute('data-full-src'), '_blank'); }

            // Fallback when the file is missing/unauthorized: degrade to a small file tag
            // (path+name visible) instead of a broken-image icon.
            function messageImageError(img) {
                const div = document.createElement('div');
                div.className = 'file-tag';
                div.textContent = '\u{1F5BC}\uFE0F ' + (img.getAttribute('data-name') || 'image');
                if (img.parentNode) img.parentNode.replaceChild(div, img);
            }


            function appendMessage(role, content, streaming = false, mediaUrls = [], fileContents = null) {
            const container = document.getElementById('chatContainer');
            const welcome = document.getElementById('welcome');
            if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message ' + role;
            if (streaming) messageDiv.dataset.streaming = 'true';
            messageDiv.dataset.messageIndex = container.querySelectorAll('.message').length;
            let mediaHtml = '';
            if (mediaUrls && mediaUrls.length > 0) {
                // SECURITY (2026-08-25): /files/ is no longer public - render message images
                // through the authenticated download endpoint (same pattern as preview.js).
                // History stores "/files/<name>" or plain "<name>"; both normalize to a
                // working_root-relative path for ?path=...
                mediaHtml = '<div class="message-images">' + mediaUrls.map(url => {
                    const name = String(url).split('/').pop();
                    if (url.match(/\.(jpg|jpeg|png|webp|gif)$/i)) {
                        const src = buildMediaUrl(url);
                        return '<img src="' + src + '" data-full-src="' + src + '" data-name="' + escapeHtml(name) + '" class="message-image" onclick="openMessageImage(this)" onerror="messageImageError(this)" title="Click to open full size">';
                    }
                    return '<div class="file-tag">' + escapeHtml(name) + '</div>';
                }).join('') + '</div>';
            }
            let fileContentHtml = '';
            if (fileContents && fileContents.length > 0) {
                const textFiles = fileContents.filter(fc => fc.type === 'text');
                if (textFiles.length > 0) {
                    fileContentHtml = '<div class="file-contents">' + textFiles.map(fc => {
                        const displayContent = fc.content.length > 1000 ? fc.content.substring(0, 1000) + '...' : fc.content;
                        return '<div class="file-content-preview"><div class="file-header">📄 ' + escapeHtml(fc.filename) + '</div><pre class="file-content-text">' + escapeHtml(displayContent) + '</pre></div>';
                    }).join('') + '</div>';
                }
            }
            const contentHtml = role === 'assistant' ? parseMarkdown(content) : escapeHtml(content).replace(/\n/g, '<br>');
            const copyIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> Copy';
            const editIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg> Edit';
            messageDiv.innerHTML = '<div class="avatar">' + (role === 'user' ? 'U' : 'AI') + '</div><div class="message-content">' + (role === 'user' ? '<button class="edit-btn copy-top" onclick="startEditMessage(this)">' + editIcon + '</button>' : '') + '<button class="copy-btn copy-top" onclick="copyMessage(this)">' + copyIcon + '</button>' + mediaHtml + fileContentHtml + '<div class="content-text">' + contentHtml + '</div>' + (role === 'user' ? '<button class="edit-btn copy-bottom" onclick="startEditMessage(this)">' + editIcon + '</button>' : '') + '<button class="copy-btn copy-bottom" onclick="copyMessage(this)">' + copyIcon + '</button></div>';
            messageDiv.querySelector('.message-content').setAttribute('data-raw-content', content);
            container.appendChild(messageDiv);
            
            if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) { scrollToBottom(); }
        }

