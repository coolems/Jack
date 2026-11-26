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

        function showScrollIndicator() { positionScrollIndicator(); const indicator = document.getElementById('scrollIndicator'); if (indicator) indicator.style.display = 'flex'; }
        function hideScrollIndicator() { const indicator = document.getElementById('scrollIndicator'); if (indicator) indicator.style.display = 'none'; }

        function scrollToBottomAndResume() {
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            scrollToBottom();
            hideScrollIndicator();
        }

        /**
         * (2026-11-26 user request) Keep the jump-to-bottom indicator on the LOWER RIGHT of the
         * CONVERSATION frame. The element is a child of .main (see index.html), so its `right`
         * already measures from the frame's own edge - opening the Files or Preview panel shrinks
         * .main instead of sliding the button over the panel. Only `bottom` has to be derived:
         * the prompt frame (.input-container) is the last row of .main and changes height with the
         * textarea and the file chips, so the button sits just above it.
         */
        function positionScrollIndicator() {
            const indicator = document.getElementById('scrollIndicator');
            const input = document.querySelector('.input-container');
            if (!indicator) return;
            if (!input) { indicator.style.bottom = '120px'; return; }   // pre-layout fallback
            const height = Math.round(input.getBoundingClientRect().height);
            indicator.style.bottom = (height + 12) + 'px';
        }

        /** Position once and keep it correct while the chat frame / prompt frame resize. */
        function initScrollIndicatorPosition() {
            positionScrollIndicator();
            if (typeof ResizeObserver === 'undefined') {
                window.addEventListener('resize', positionScrollIndicator);
                return;
            }
            if (COOLEMS.scrollIndicatorObserver) COOLEMS.scrollIndicatorObserver.disconnect();
            const ro = new ResizeObserver(() => positionScrollIndicator());
            const chat = document.getElementById('chatContainer');
            const input = document.querySelector('.input-container');
            if (chat) ro.observe(chat);
            if (input) ro.observe(input);
            COOLEMS.scrollIndicatorObserver = ro;
        }

        /**
         * (2026-11-26 user request) Land the view on the END of the conversation - used whenever
         * a workspace is opened/switched. History that settles right after the swap (turn blocks,
         * restored cards, images still loading) would otherwise leave the frame short of the last
         * message, so the jump is repeated over the next frames and on late image loads. It never
         * fights the user: as soon as they scroll up, the follow-up jumps stop.
         */
        function scrollToConversationEnd() {
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            hideScrollIndicator();
            const container = document.getElementById('chatContainer');
            if (!container) return;
            // A wheel/touch on the list is the user taking over during the settle window.
            let userTookOver = false;
            const takeOver = () => { userTookOver = true; };
            container.addEventListener('wheel', takeOver, { passive: true, once: true });
            container.addEventListener('touchstart', takeOver, { passive: true, once: true });
            const settle = () => { if (!userTookOver && !COOLEMS.userScrolledUp) scrollToBottom(); };
            settle();
            requestAnimationFrame(() => { settle(); requestAnimationFrame(settle); });
            setTimeout(settle, 120);
            setTimeout(settle, 400);
            setTimeout(() => {
                container.removeEventListener('wheel', takeOver);
                container.removeEventListener('touchstart', takeOver);
            }, 600);
            // Images placed after the swap grow the container - re-anchor once each has loaded.
            const pending = container.querySelectorAll('img:not([data-scroll-settled])');
            for (const img of pending) {
                img.dataset.scrollSettled = '1';
                if (!img.complete) img.addEventListener('load', settle, { once: true });
            }
        }

        function initScrollDetection() {
            // (2026-11-26) the jump-to-bottom indicator is pinned inside the chat frame - keep its
            // bottom offset in sync from the start (independent of the scroll listeners below).
            initScrollIndicatorPosition();
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
