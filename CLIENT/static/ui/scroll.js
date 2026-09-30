/**
 * scroll.js - Scroll detection, auto-scroll, scroll indicator.
 * Uses COOLEMS shared state from app.js.
 */

        function isNearBottom(container) {
            if (!container) return true;
            const scrollPosition = container.scrollTop + container.clientHeight;
            const scrollHeight = container.scrollHeight;
            return scrollHeight - scrollPosition < 100;
        }

        function scrollToBottom() {
            const container = document.getElementById('chatContainer');
            if (container) { container.scrollTop = container.scrollHeight; }
        }

        function showScrollIndicator() { const indicator = document.getElementById('scrollIndicator'); if (indicator) indicator.style.display = 'flex'; }
        function hideScrollIndicator() { const indicator = document.getElementById('scrollIndicator'); if (indicator) indicator.style.display = 'none'; }

        function scrollToBottomAndResume() {
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            scrollToBottom();
            hideScrollIndicator();
        }

        function initScrollDetection() {
            const container = document.getElementById('chatContainer');
            if (!container) return;
            
            container.addEventListener('scroll', function() {
                if (COOLEMS.scrollTimeout) clearTimeout(COOLEMS.scrollTimeout);
                
                const nearBottom = isNearBottom(container);
                
                if (!nearBottom && COOLEMS.isStreaming) {
                    COOLEMS.userScrolledUp = true;
                    COOLEMS.autoScrollEnabled = false;
                    showScrollIndicator();
                } else if (nearBottom && COOLEMS.userScrolledUp) {
                    COOLEMS.userScrolledUp = false;
                    COOLEMS.autoScrollEnabled = true;
                    hideScrollIndicator();
                }
                
                COOLEMS.scrollTimeout = setTimeout(() => {
                    if (nearBottom) {
                        COOLEMS.userScrolledUp = false;
                        COOLEMS.autoScrollEnabled = true;
                        hideScrollIndicator();
                    }
                }, 150);
            });
        }

        // ===== WEBSOCKET MESSAGE HANDLER =====
