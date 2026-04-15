/*
 * ExpressLane shared client-side utilities
 * Sprint 2 / Item #3 — UI improvements plan, 2026-04-11.
 *
 * This file is loaded from _navbar.html which is included at the top of
 * <body> on every authenticated page, so everything defined here is
 * available to per-template <script> blocks that follow.
 *
 * Contents:
 *   fetchWithRetry(url, opts)  — moved from _navbar.html, OCI 503 retry
 *   debounce(fn, ms)           — used by search inputs
 *   escapeHtml(str)            — used by row-rendering functions
 *   $(sel), $$(sel)            — DOM helpers
 *   formatRelativeTime(ts)     — "Nm ago" for freshness indicators
 *   window.toast                — stub; fleshed out in Sprint 3 item #5
 *   window.confirmAction        — stub; fleshed out in Sprint 3 item #4
 *
 * Note: the CSRF token auto-inject wrapper stays in _navbar.html because
 * it needs to run synchronously before any other fetch on the page. It's
 * a one-time page bootstrap, not a reusable utility.
 */

(function() {
    'use strict';

    // ----- fetchWithRetry: retry on HTTP 503 + retryable:true (OCI key prop) -----
    // Backoff: baseDelay * 2^attempt (default 3s → 6s → 12s → 24s → 48s)
    // Moved verbatim from _navbar.html on 2026-04-11.
    window.fetchWithRetry = async function(url, options, maxRetries, baseDelay) {
        maxRetries = maxRetries || 5;
        baseDelay = baseDelay || 3000;
        let lastResponse;
        for (let attempt = 0; attempt <= maxRetries; attempt++) {
            lastResponse = await fetch(url, options);
            if (lastResponse.status !== 503 || attempt === maxRetries) return lastResponse;
            const clone = lastResponse.clone();
            try {
                const body = await clone.json();
                if (!body.retryable) return lastResponse;
            } catch (e) { return lastResponse; }
            const delay = baseDelay * Math.pow(2, attempt);
            console.log(`[fetchWithRetry] ${url} — retry in ${delay/1000}s (${attempt+1}/${maxRetries})`);
            await new Promise(r => setTimeout(r, delay));
        }
        return lastResponse;
    };

    // ----- debounce: delay fn until ms ms after the last call ---------
    // Used by the inventory search input and (Sprint 2 #2) the migration
    // list search input. Lifted in style from inventory_dashboard.html.
    window.debounce = function(fn, ms) {
        let timer;
        return function(...args) {
            const ctx = this;
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(ctx, args), ms);
        };
    };

    // ----- escapeHtml: safe interpolation for innerHTML templates ------
    // Consolidates 3+ copies currently scattered across templates.
    window.escapeHtml = function(str) {
        const div = document.createElement('div');
        div.textContent = String(str == null ? '' : str);
        return div.innerHTML;
    };

    // ----- $ / $$ : lightweight DOM query helpers ----------------------
    // Only defined if not already in use. `$` is also jQuery's global,
    // which this app doesn't use, but we guard anyway.
    if (typeof window.$ === 'undefined') {
        window.$ = function(sel, ctx) { return (ctx || document).querySelector(sel); };
    }
    if (typeof window.$$ === 'undefined') {
        window.$$ = function(sel, ctx) { return Array.from((ctx || document).querySelectorAll(sel)); };
    }

    // ----- formatRelativeTime: human-readable "Nm ago" strings ---------
    // Accepts Date | number (ms) | ISO string. Returns e.g. "just now",
    // "12s ago", "4m ago", "2h ago", "3d ago". Used by Sprint 1
    // freshness indicators — each page currently has its own copy.
    window.formatRelativeTime = function(ts) {
        if (ts == null) return '';
        let ms;
        if (ts instanceof Date)        ms = ts.getTime();
        else if (typeof ts === 'number') ms = ts;
        else                             ms = Date.parse(ts);
        if (!Number.isFinite(ms)) return '';
        const ageSec = Math.floor((Date.now() - ms) / 1000);
        if (ageSec < 5)         return 'just now';
        if (ageSec < 60)        return ageSec + 's ago';
        if (ageSec < 3600)      return Math.floor(ageSec / 60) + 'm ago';
        if (ageSec < 86400)     return Math.floor(ageSec / 3600) + 'h ago';
        return Math.floor(ageSec / 86400) + 'd ago';
    };

    // ----- window.toast: stacked notification system (Sprint 3 #5) -----
    // Lazy container creation on first call. Auto-dismiss: 5s for
    // success/info, 10s for warn/error. Click × to dismiss early.
    // Uses aria-live="polite" for screen reader announcements.
    const TOAST_ICONS = {
        success: 'fa-check-circle',
        error:   'fa-exclamation-circle',
        warn:    'fa-exclamation-triangle',
        info:    'fa-info-circle',
    };
    const TOAST_DISMISS_MS = {
        success: 5000,
        info:    5000,
        warn:    10000,
        error:   10000,
    };

    function ensureToastContainer() {
        let c = document.getElementById('toast-container');
        if (!c) {
            c = document.createElement('div');
            c.id = 'toast-container';
            c.className = 'toast-container';
            c.setAttribute('aria-live', 'polite');
            c.setAttribute('aria-atomic', 'false');
            document.body.appendChild(c);
        }
        return c;
    }

    function dismissToast(el) {
        if (!el || el.dataset.leaving === '1') return;
        el.dataset.leaving = '1';
        el.classList.add('toast-leaving');
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }

    function resetDismissTimer(el, kind, opts) {
        if (el._dismissTimer) { clearTimeout(el._dismissTimer); el._dismissTimer = null; }
        if (opts && opts.sticky) return;
        const ms = (opts && typeof opts.duration === 'number')
            ? opts.duration
            : (TOAST_DISMISS_MS[kind] || 5000);
        if (ms > 0) {
            el._dismissTimer = setTimeout(() => dismissToast(el), ms);
        }
    }

    function showToast(kind, message, opts) {
        opts = opts || {};
        const container = ensureToastContainer();

        // Key-based de-dup (Sprint 3 #5 follow-up, 2026-04-11).
        // If a previous toast with the same key is still visible, UPDATE it
        // in place instead of stacking a new one. This is how long-running
        // progress updates should feel — one toast whose text/icon changes,
        // not N stacked copies. Callers opt in by passing { key: 'my-id' }.
        if (opts.key) {
            const existing = container.querySelector(
                '.toast[data-toast-key="' + String(opts.key).replace(/"/g, '\\"') + '"]'
            );
            if (existing && existing.dataset.leaving !== '1') {
                // Update kind (color + icon)
                existing.className = 'toast ' + kind;
                existing.setAttribute('data-toast-key', opts.key);
                existing.setAttribute('role', kind === 'error' || kind === 'warn' ? 'alert' : 'status');
                const iconEl = existing.querySelector('.toast-icon');
                if (iconEl) {
                    iconEl.className = 'fas ' + (TOAST_ICONS[kind] || TOAST_ICONS.info) + ' toast-icon';
                }
                existing.querySelector('.toast-body').textContent =
                    String(message == null ? '' : message);
                resetDismissTimer(existing, kind, opts);
                return existing;
            }
        }

        const el = document.createElement('div');
        el.className = 'toast ' + kind;
        if (opts.key) el.setAttribute('data-toast-key', opts.key);
        el.setAttribute('role', kind === 'error' || kind === 'warn' ? 'alert' : 'status');

        const iconClass = TOAST_ICONS[kind] || TOAST_ICONS.info;
        el.innerHTML = `
            <i class="fas ${iconClass} toast-icon" aria-hidden="true"></i>
            <div class="toast-body"></div>
            <button type="button" class="toast-close" aria-label="Dismiss notification">&times;</button>
        `;
        // Use textContent for the message body so any message content is
        // escaped automatically — safer than building the HTML directly.
        el.querySelector('.toast-body').textContent = String(message == null ? '' : message);

        container.appendChild(el);

        // Click body or × to dismiss early.
        el.addEventListener('click', function(e) {
            if (e.target.closest('.toast-close') || e.target === el || e.target.classList.contains('toast-body')) {
                dismissToast(el);
            }
        });

        // Auto-dismiss (unless opts.sticky).
        resetDismissTimer(el, kind, opts);

        return el;
    }

    window.toast = {
        success: function(msg, opts) { return showToast('success', msg, opts); },
        error:   function(msg, opts) { return showToast('error',   msg, opts); },
        warn:    function(msg, opts) { return showToast('warn',    msg, opts); },
        info:    function(msg, opts) { return showToast('info',    msg, opts); },
    };

    // ----- window.confirmAction: styled modal (Sprint 3 #4) -----
    // Usage:
    //   const ok = await confirmAction({
    //       title: 'Cancel migration?',
    //       message: 'This will stop replication for UBTestx04.',
    //       confirmText: 'Cancel Migration',
    //       cancelText: 'Keep Running',
    //       destructive: true,
    //   });
    //   if (!ok) return;
    //
    // Returns Promise<boolean>. Esc and backdrop click both resolve false.
    window.confirmAction = function(opts) {
        opts = opts || {};
        const title        = opts.title        || 'Are you sure?';
        const message      = opts.message      || '';
        const confirmText  = opts.confirmText  || 'Confirm';
        const cancelText   = opts.cancelText   || 'Cancel';
        const destructive  = !!opts.destructive;
        const iconClass    = opts.icon || (destructive ? 'fa-exclamation-triangle' : 'fa-question-circle');

        return new Promise((resolve) => {
            // Build backdrop + dialog
            const backdrop = document.createElement('div');
            backdrop.className = 'confirm-backdrop';
            const dialog = document.createElement('div');
            dialog.className = 'confirm-dialog' + (destructive ? ' destructive' : '');
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');
            dialog.setAttribute('aria-labelledby', 'confirmTitle');
            dialog.innerHTML = `
                <h3 id="confirmTitle" class="confirm-title">
                    <i class="fas ${iconClass} confirm-icon" aria-hidden="true"></i>
                    <span class="confirm-title-text"></span>
                </h3>
                <div class="confirm-message"></div>
                <div class="confirm-actions">
                    <button type="button" class="confirm-btn confirm-btn-cancel"></button>
                    <button type="button" class="confirm-btn confirm-btn-ok"></button>
                </div>
            `;
            backdrop.appendChild(dialog);
            document.body.appendChild(backdrop);

            // textContent avoids any HTML injection from caller opts
            dialog.querySelector('.confirm-title-text').textContent = title;
            dialog.querySelector('.confirm-message').textContent = message;
            dialog.querySelector('.confirm-btn-cancel').textContent = cancelText;
            const okBtn = dialog.querySelector('.confirm-btn-ok');
            okBtn.textContent = confirmText;

            // Remember the element that was focused before the dialog opened,
            // so we can restore focus on close — basic focus management.
            const prevFocus = document.activeElement;

            function cleanup(result) {
                document.removeEventListener('keydown', onKey);
                if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
                try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) {}
                resolve(result);
            }

            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }
                if (e.key === 'Enter')  { e.preventDefault(); cleanup(true); }
            }

            dialog.querySelector('.confirm-btn-cancel').addEventListener('click', () => cleanup(false));
            okBtn.addEventListener('click', () => cleanup(true));
            backdrop.addEventListener('click', (e) => { if (e.target === backdrop) cleanup(false); });
            document.addEventListener('keydown', onKey);

            // Focus the confirm button by default so Enter confirms.
            // For destructive actions, focus Cancel instead — safer default.
            if (destructive) {
                dialog.querySelector('.confirm-btn-cancel').focus();
            } else {
                okBtn.focus();
            }
        });
    };

})();
