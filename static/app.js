/* ═══════════════════════════════════════════════
   Savitar Desktop — App Logic
   Handles: Navigation, Theme, Downloads, Extract
═══════════════════════════════════════════════ */
(function () {
    'use strict';

    // Statuses that mean a download is being worked on. Mirrors
    // ACTIVE_STATUSES in core/downloader.py — "preparing" (waiting on ffmpeg)
    // and "retrying" (transient-failure backoff) used to arrive here as a bare
    // "pending", which is why downloads looked stuck.
    const ACTIVE_STATUSES = ['downloading', 'merging', 'preparing', 'retrying'];

    // ── State ──────────────────────────────────
    const state = {
        currentPage: 'home',
        theme: 'light',       // 'light' | 'dark' | 'system'
        platforms: [],
        currentResult: null,
        selected: null,       // the MediaFormat the user picked on the card
        downloads: [],
        pollTimer: null,
        // Playlist state
        playlistResult: null,
        selectedPlaylistIds: new Set(),
        playlistFormatId: 'bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best',
        playlistFormatLabel: 'Best Available',
        playlistDownloadMode: 'all', // 'all' | 'sequential'
        playlistSearchQuery: '',
        convertedItems: [],
        eventSource: null,
        sseConnected: false,
        // Downloads toolbar & format filter state
        downloadsFilter: 'all', // 'all' | 'active' | 'completed' | 'failed'
        downloadsSearch: '',
        downloadsSort: 'default', // 'default' | 'newest' | 'oldest' | 'size'
        formatTab: 'all', // 'all' | 'video' | 'audio'
    };

    // ── Helpers ────────────────────────────────
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);
    const esc = (s) => (!s ? '' : String(s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#039;'));

    async function api(url, method = 'GET', body = null) {
        try {
            const opts = { method, headers: {} };
            if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
            const r = await fetch(url, opts);
            return await r.json();
        } catch (e) {
            console.error('API', url, e);
            return { ok: false, message: e.message };
        }
    }

    // Reads the clipboard WITHOUT any permission prompt: the backend reads it
    // directly through the Windows Win32 API, which is not sandboxed.
    async function readClipboard() {
        try {
            const res = await api('/api/clipboard');
            if (res && res.ok && typeof res.text === 'string') {
                return res.text;
            }
        } catch (_) {}
        return '';
    }

    function formatBytes(b) {
        if (!b || isNaN(b) || b <= 0) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), 4);
        return (b / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
    }

    function formatSpeed(bps) {
        if (!bps || isNaN(bps) || bps <= 0) return '0 KB/s';
        return formatBytes(bps) + '/s';
    }

    function formatDuration(s) {
        if (!s) return '0:00';
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = s % 60;
        return h > 0
            ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
            : `${m}:${String(sec).padStart(2,'0')}`;
    }

    // Open-folder glyph, reused by the recent cards and the download rows.
    const FOLDER_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

    let toastTimer = null;
    function toast(message) {
        if (!message) return;
        let el = $('#sc-toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'sc-toast';
            el.className = 'sc-toast';
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.classList.add('show');
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
    }

    // ── Theme ──────────────────────────────────
    function applyTheme(theme) {
        state.theme = theme;
        const html = document.documentElement;

        let resolved = theme;
        if (theme === 'system') {
            resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        html.setAttribute('data-theme', resolved);

        // Sync sidebar select
        const sel = $('#theme-select');
        if (sel) sel.value = theme;

        // Sync settings buttons
        $$('.theme-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.themeVal === theme);
        });

        // Update theme icon
        updateThemeIcon(resolved);

        // Persist
        try { localStorage.setItem('sc_theme', theme); } catch (_) {}
    }

    function updateThemeIcon(resolved) {
        const icon = $('#theme-icon');
        if (!icon) return;
        if (resolved === 'dark') {
            icon.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
        } else {
            icon.innerHTML = `<circle cx="12" cy="12" r="5"/>
                <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
                <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
                <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
                <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>`;
        }
    }

    function loadTheme() {
        let saved = 'light';
        try { saved = localStorage.getItem('sc_theme') || 'light'; } catch (_) {}
        applyTheme(saved);

        // Watch system changes
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
            if (state.theme === 'system') applyTheme('system');
        });
    }

    // ── Navigation ─────────────────────────────
    function navigate(page) {
        state.currentPage = page;

        // Update nav items
        $$('.nav-item').forEach(el => {
            el.classList.toggle('active', el.dataset.page === page);
        });

        // Show/hide pages
        $$('.page').forEach(el => {
            el.classList.toggle('active', el.id === `page-${page}`);
        });
    }

    function setupNav() {
        $$('.nav-item').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const page = link.dataset.page;
                if (page) navigate(page);
            });
        });

        // View All button on Home → go to Downloads page
        const viewAllBtn = $('#btn-view-all');
        if (viewAllBtn) {
            viewAllBtn.addEventListener('click', () => navigate('downloads'));
        }
    }

    // ── Platforms ──────────────────────────────
    // Official brand marks, single-path where possible. Colour comes from CSS
    // (`.platform-chip[data-platform=…]`) so dark mode can invert TikTok/X.
    const PLATFORM_ICONS = {
        youtube: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M23.5 6.19a3.02 3.02 0 0 0-2.12-2.14C19.5 3.55 12 3.55 12 3.55s-7.5 0-9.38.5A3.02 3.02 0 0 0 .5 6.19C0 8.08 0 12 0 12s0 3.92.5 5.81a3.02 3.02 0 0 0 2.12 2.14c1.88.5 9.38.5 9.38.5s7.5 0 9.38-.5a3.02 3.02 0 0 0 2.12-2.14C24 15.92 24 12 24 12s0-3.92-.5-5.81zM9.55 15.57V8.43L15.82 12l-6.27 3.57z"/></svg>',
        instagram: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.16c3.2 0 3.58.01 4.85.07 1.17.05 1.96.24 2.65.51.72.28 1.33.66 1.94 1.27a5.2 5.2 0 0 1 1.26 1.93c.27.7.46 1.49.52 2.66.06 1.27.07 1.65.07 4.85s-.01 3.58-.07 4.85c-.06 1.17-.25 1.96-.52 2.65a5.5 5.5 0 0 1-1.26 1.94 5.2 5.2 0 0 1-1.94 1.26c-.69.27-1.48.46-2.65.52-1.27.06-1.65.07-4.85.07s-3.58-.01-4.85-.07c-1.17-.06-1.96-.25-2.66-.52a5.2 5.2 0 0 1-1.93-1.26 5.2 5.2 0 0 1-1.27-1.94c-.27-.69-.46-1.48-.51-2.65C2.17 15.58 2.16 15.2 2.16 12s.01-3.58.07-4.85c.05-1.17.24-1.96.51-2.66.28-.72.66-1.32 1.27-1.93A5.2 5.2 0 0 1 5.94 2.3c.7-.27 1.49-.46 2.66-.51C9.87 2.17 10.25 2.16 12 2.16zm0 1.98c-3.15 0-3.5.01-4.74.07-.95.04-1.47.2-1.81.34-.46.17-.78.39-1.12.73-.34.34-.55.66-.73 1.12-.13.34-.3.86-.34 1.81-.06 1.24-.07 1.6-.07 4.74s.01 3.5.07 4.74c.04.95.21 1.47.34 1.81.18.46.39.78.73 1.12.34.34.66.56 1.12.73.34.14.86.3 1.81.34 1.24.06 1.59.07 4.74.07s3.5-.01 4.74-.07c.95-.04 1.47-.2 1.81-.34.46-.17.78-.39 1.12-.73.34-.34.56-.66.73-1.12.14-.34.3-.86.34-1.81.06-1.24.07-1.6.07-4.74s-.01-3.5-.07-4.74c-.04-.95-.2-1.47-.34-1.81a3.02 3.02 0 0 0-.73-1.12 3.02 3.02 0 0 0-1.12-.73c-.34-.13-.86-.3-1.81-.34-1.24-.06-1.59-.07-4.74-.07zm0 3.38a5.48 5.48 0 1 1 0 10.96 5.48 5.48 0 0 1 0-10.96zm0 1.98a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zm6.98-2.2a1.28 1.28 0 1 1-2.56 0 1.28 1.28 0 0 1 2.56 0z"/></svg>',
        tiktok: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-5.2 1.74 2.89 2.89 0 0 1 2.31-4.64c.3 0 .59.05.86.13V9.4a6.33 6.33 0 0 0-.86-.05A6.34 6.34 0 0 0 9.44 22a6.34 6.34 0 0 0 6.34-6.34V8.87a8.16 8.16 0 0 0 4.77 1.52V6.94a4.85 4.85 0 0 1-.96-.25z"/></svg>',
        twitter: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.9 1.15h3.68l-8.04 9.19L24 22.85h-7.4l-5.8-7.58-6.63 7.58H.49l8.6-9.83L0 1.15h7.59l5.44 7.2 5.87-7.2zm-1.29 19.5h2.04L6.48 3.24H4.3l13.31 17.41z"/></svg>',
        x: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.9 1.15h3.68l-8.04 9.19L24 22.85h-7.4l-5.8-7.58-6.63 7.58H.49l8.6-9.83L0 1.15h7.59l5.44 7.2 5.87-7.2zm-1.29 19.5h2.04L6.48 3.24H4.3l13.31 17.41z"/></svg>',
        facebook: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M24 12.07C24 5.4 18.63 0 12 0S0 5.4 0 12.07C0 18.1 4.39 23.1 10.13 24v-8.44H7.08v-3.49h3.05V9.41c0-3.02 1.79-4.69 4.53-4.69 1.31 0 2.68.24 2.68.24v2.97h-1.5c-1.5 0-1.96.93-1.96 1.89v2.25h3.32l-.53 3.49h-2.8V24C19.62 23.1 24 18.1 24 12.07z"/></svg>',
        pinterest: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.08 3.16 9.42 7.62 11.17-.1-.95-.2-2.4.04-3.44l1.4-5.96s-.36-.72-.36-1.78c0-1.67.97-2.92 2.17-2.92 1.02 0 1.51.77 1.51 1.69 0 1.03-.65 2.57-1 4-.28 1.2.6 2.17 1.78 2.17 2.14 0 3.79-2.26 3.79-5.52 0-2.88-2.07-4.9-5.03-4.9-3.42 0-5.44 2.57-5.44 5.22 0 1.03.4 2.14.9 2.74.1.12.11.23.08.35l-.34 1.36c-.05.22-.18.28-.4.17-1.5-.7-2.42-2.9-2.42-4.66 0-3.8 2.76-7.29 7.95-7.29 4.18 0 7.42 2.98 7.42 6.95 0 4.15-2.61 7.5-6.24 7.5-1.22 0-2.36-.64-2.75-1.39l-.75 2.85c-.27 1.04-1 2.35-1.5 3.15A12 12 0 0 0 12 24c6.63 0 12-5.37 12-12S18.63 0 12 0z"/></svg>',
        globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
    };

    async function loadPlatforms() {
        const data = await api('/api/platforms');
        if (data.ok && data.platforms) {
            state.platforms = data.platforms;
            renderPlatformChips();
        }
    }

    function platformIcon(id) {
        return PLATFORM_ICONS[String(id || '').toLowerCase()] || PLATFORM_ICONS.globe;
    }

    function renderPlatformChips() {
        const wrap = $('#platform-chips');
        if (!wrap) return;
        wrap.innerHTML = '';
        const enabled = state.platforms.filter(p => p.enabled);
        enabled.slice(0, 10).forEach(p => {
            const id = String(p.id || '').toLowerCase();
            const chip = document.createElement('div');
            chip.className = 'platform-chip';
            chip.title = p.name;
            chip.dataset.platform = id;
            chip.innerHTML = platformIcon(id);
            wrap.appendChild(chip);
        });
        if (enabled.length > 10) {
            const more = document.createElement('div');
            more.className = 'platform-chip';
            more.textContent = '···';
            more.style.fontSize = '11px';
            more.style.fontWeight = '700';
            wrap.appendChild(more);
        }
    }

    // ── Extract ────────────────────────────────
    function setupExtract() {
        const input = $('#url-input');
        const btn = $('#btn-extract');
        const pasteBtn = $('#btn-paste');
        const clearBtn = $('#btn-clear-url');
        if (!btn || !input) return;

        // Paste from clipboard button
        if (pasteBtn) {
            pasteBtn.addEventListener('click', async () => {
                const text = await readClipboard();
                const trimmed = text.trim();
                if (trimmed) {
                    input.value = trimmed;
                    updateClearBtn();
                    input.focus();
                    if (/^(https?:\/\/|www\.)/i.test(trimmed)) {
                        doExtract();
                    }
                } else {
                    // Clipboard empty or blocked — focus input for manual paste
                    input.focus();
                }
            });
        }

        // Clear (X) button
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                input.value = '';
                updateClearBtn();
                hideAllResults();
                input.focus();
            });
        }

        // Show/hide clear button based on input content
        function updateClearBtn() {
            if (!clearBtn) return;
            if (input.value.trim()) {
                clearBtn.classList.remove('hidden');
            } else {
                clearBtn.classList.add('hidden');
            }
        }

        let pasteDebounce = null;
        input.addEventListener('input', updateClearBtn);
        input.addEventListener('paste', () => {
            setTimeout(() => {
                updateClearBtn();
                const val = input.value.trim();
                if (/^(https?:\/\/|www\.)/i.test(val)) {
                    if (pasteDebounce) clearTimeout(pasteDebounce);
                    pasteDebounce = setTimeout(() => doExtract(), 120);
                }
            }, 30);
        });

        btn.addEventListener('click', () => doExtract());
        input.addEventListener('keydown', e => { if (e.key === 'Enter') doExtract(); });

        // Speculative clipboard pre-warm: when window gains focus, if clipboard has a video URL,
        // pre-fetch it silently in background so when user pastes, result appears in 0ms!
        let lastPrewarmedUrl = '';
        const KNOWN_HOSTS = ['youtube.com', 'youtu.be', 'tiktok.com', 'instagram.com', 'twitter.com', 'x.com', 'facebook.com', 'fb.watch', 'pinterest.com', 'pin.it', 'reddit.com'];
        async function prewarmClipboard() {
            if (state.currentPage !== 'home' && state.currentPage !== 'playlist') return;
            try {
                const text = (await readClipboard()) || '';
                const u = text.trim();
                if (!u || u === lastPrewarmedUrl || u.length > 500) return;
                const low = u.toLowerCase();
                if (/^(https?:\/\/|www\.)/i.test(u) && KNOWN_HOSTS.some(h => low.includes(h))) {
                    lastPrewarmedUrl = u;
                    api('/api/extract', 'POST', { url: u }).catch(() => {});
                }
            } catch (_) {}
        }
        window.addEventListener('focus', () => {
            setTimeout(prewarmClipboard, 250);
        });
    }

    async function doExtract() {
        const input = $('#url-input');
        const url = input?.value.trim();
        if (!url) return;

        // Smart route to Playlist & Channel Downloader if user pasted a playlist or channel URL
        if (isPlaylistUrl(url)) {
            navigate('playlist');
            const plInput = $('#playlist-url-input');
            if (plInput) {
                plInput.value = url;
                const clearBtn = $('#btn-playlist-clear-url');
                if (clearBtn) clearBtn.classList.remove('hidden');
            }
            doPlaylistExtract(url);
            return;
        }

        // Validate YouTube URL
        const ytErr = validateYoutube(url);
        if (ytErr) { showError(ytErr, 'INVALID_LINK'); return; }

        // Reset UI
        hideAllResults();
        $('#loading-skeleton')?.classList.remove('hidden');

        const data = await api('/api/extract', 'POST', { url });

        $('#loading-skeleton')?.classList.add('hidden');

        if (data.ok) {
            state.currentResult = { ...data, url };
            renderResultCard(data);
        } else {
            showError(data.message || 'Could not process this URL.', data.code || 'ERROR');
        }
    }

    function hideAllResults() {
        ['#loading-skeleton', '#error-state', '#result-card'].forEach(sel => {
            $(sel)?.classList.add('hidden');
        });
    }

    function showError(msg, code) {
        const title = $('#error-title');
        const msgEl = $('#error-message');
        const settBtn = $('#btn-error-settings');
        const svBtn = $('#btn-savevid');

        const url = $('#url-input')?.value?.trim() || '';
        const isInstagram = /instagram\.com/i.test(url);
        const isLoginRequired = code === 'PRIVATE_OR_LOGIN_REQUIRED';

        if (title) title.textContent = isLoginRequired
            ? 'Login Required' : 'Extraction Failed';
        if (msgEl) msgEl.textContent = msg;
        if (settBtn) settBtn.classList.toggle('hidden', !isLoginRequired);
        if (svBtn) {
            const show = isLoginRequired && isInstagram;
            svBtn.classList.toggle('hidden', !show);
            if (show) {
                svBtn.onclick = () => {
                    window.open('https://savevid.net/en#' + encodeURIComponent(url), '_blank');
                };
            }
        }
        $('#error-state')?.classList.remove('hidden');
    }

    function validateYoutube(url) {
        try {
            const u = new URL(url);
            const host = u.hostname.replace(/^www\./, '');
            let vid = '';
            if (host === 'youtu.be') vid = u.pathname.split('/').filter(Boolean)[0] || '';
            else if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
                if (u.pathname === '/watch') vid = u.searchParams.get('v') || '';
                else if (/^\/(shorts|embed)\//.test(u.pathname)) vid = u.pathname.split('/').filter(Boolean)[1] || '';
            }
            if (vid && !/^[A-Za-z0-9_-]{11}$/.test(vid)) {
                return 'This YouTube link looks incomplete. Please paste the full video URL.';
            }
        } catch (_) {}
        return '';
    }

    function renderResultCard(data) {
        const thumb = $('#result-thumb');
        const wrap = thumb?.closest('.media-thumb-wrap');
        if (wrap) wrap.classList.toggle('no-thumb', !data.thumbnail);
        if (thumb) {
            thumb.src = '';
            if (data.thumbnail) thumb.src = data.thumbnail;
        }

        const durVal = data.duration || data.duration_seconds || 0;
        const dur = $('#result-duration');
        if (dur) {
            dur.textContent = formatDuration(durVal);
            dur.classList.toggle('hidden', !durVal);
        }

        const kind = $('#result-kind');
        if (kind) kind.textContent = (data.media_kind || 'video').toUpperCase();

        const titleEl = $('#result-title');
        if (titleEl) { titleEl.textContent = data.title || 'Unknown Title'; titleEl.title = data.title || ''; }

        // Dot-separated meta row: author · duration · platform
        const metaEl = $('#result-meta');
        if (metaEl) {
            const platform = data.platform || data.extractor || data.source_platform || '';
            const bits = [];
            if (data.author) bits.push(`<span>${esc(data.author)}</span>`);
            if (durVal) bits.push(`<span>${formatDuration(durVal)}</span>`);
            if (platform) bits.push(`<span class="meta-platform">${esc(platform)}</span>`);
            metaEl.innerHTML = bits.join('<span class="meta-dot">•</span>');
        }

        const formats = data.formats || [];
        const presets = formats.filter(f => f.kind === 'preset');
        const rawFormats = formats.filter(f => f.kind !== 'preset');

        state.currentPresets = presets.length ? presets : formats;
        state.currentRawFormats = rawFormats;
        state.selected = null;

        renderQualityPills(state.currentPresets, state.currentRawFormats);

        $('#result-card')?.classList.remove('hidden');
    }

    function renderQualityPills(presets, rawFormats) {
        const wrap = $('#quality-pills');
        if (!wrap) return;
        wrap.innerHTML = '';

        let displayedPresets = presets;
        if (state.formatTab === 'video') {
            displayedPresets = presets.filter(f => !f.audio_only && f.tier !== 'audio' && f.ext !== 'mp3' && f.ext !== 'm4a');
        } else if (state.formatTab === 'audio') {
            displayedPresets = presets.filter(f => f.audio_only || f.tier === 'audio' || f.ext === 'mp3' || f.ext === 'm4a' || (f.label && f.label.toLowerCase().includes('audio')));
        }

        if (!displayedPresets.length) {
            wrap.innerHTML = `<span style="font-size:12px;color:var(--text-3)">No ${state.formatTab} formats available</span>`;
            return;
        }

        displayedPresets.forEach((fmt, idx) => {
            const pill = document.createElement('button');
            pill.type = 'button';
            pill.className = 'q-pill';
            const isMax = fmt.label && fmt.label.toLowerCase().includes('max quality');
            if (isMax) {
                pill.classList.add('max-enhanced');
            }
            pill.dataset.fid = fmt.format_id;
            pill.dataset.pidx = `preset-${idx}`;
            fmt._pidx = `preset-${idx}`;
            const iconPrefix = isMax ? '⚡ ' : '';
            const approxPrefix = fmt.needs_merge ? '~' : '';
            const size = fmt.filesize_bytes ? `<span class="q-size">${approxPrefix}${formatBytes(fmt.filesize_bytes)}</span>` : '';
            pill.innerHTML = `${iconPrefix}${esc(fmt.label)}${size}`;
            pill.title = isMax
                ? 'Maximum Quality Fully Enhanced — Highest resolution, 60fps/HDR & highest audio bitrate'
                : (fmt.needs_merge ? `${fmt.label} — video and audio merged in high quality` : (fmt.label || ''));
            pill.addEventListener('click', () => selectFormat(fmt));
            wrap.appendChild(pill);
        });

        // Advanced raw formats toggle button & container
        const toggleBtn = $('#btn-toggle-all-qualities');
        const toggleText = $('#toggle-qualities-text');
        const advSection = $('#all-qualities-section');
        const rawWrap = $('#raw-format-pills');

        if (toggleBtn && rawWrap && advSection) {
            if (rawFormats && rawFormats.length > 0) {
                toggleBtn.classList.remove('hidden');
                toggleBtn.classList.remove('expanded');
                if (toggleText) toggleText.textContent = `All Formats (${rawFormats.length})`;
                advSection.classList.add('hidden');
                rawWrap.innerHTML = '';

                rawFormats.forEach(fmt => {
                    const pill = document.createElement('button');
                    pill.type = 'button';
                    pill.className = 'q-pill';
                    pill.dataset.fid = fmt.format_id;
                    const size = fmt.filesize_bytes ? `<span class="q-size">${formatBytes(fmt.filesize_bytes)}</span>` : '';
                    const label = fmt.label || fmt.format_id || 'stream';
                    pill.innerHTML = `${esc(label)}${size}`;
                    pill.title = `${label} (${fmt.ext || 'media'}) - format_id: ${fmt.format_id}`;
                    pill.addEventListener('click', () => selectFormat(fmt));
                    rawWrap.appendChild(pill);
                });
            } else {
                toggleBtn.classList.add('hidden');
                advSection.classList.add('hidden');
            }
        }

        // Auto-select preferred quality: for video links (TikTok, Instagram, YouTube, etc.),
        // default to Maximum Quality (Enhanced) / highest resolution video, not MP3 audio!
        const pq = (state.settings?.preferred_quality || 'max').toLowerCase();
        const videoPresets = presets.filter(f => !f.audio_only && f.tier !== 'audio');
        let preferred = null;

        if (videoPresets.length > 0) {
            // Video streams exist: ALWAYS prioritize highest/max quality video
            if (pq === 'max' || pq === 'best' || pq === 'audio') {
                preferred = videoPresets.find(f => (f.label && f.label.toLowerCase().includes('max quality')) || f.recommended) || videoPresets[0];
            } else {
                const targetH = parseInt(pq.replace('p', ''), 10);
                if (targetH) {
                    preferred = videoPresets.find(f => (f.sublabel && f.sublabel.toLowerCase() === pq) || f.height === targetH);
                    if (!preferred) {
                        const below = videoPresets.filter(f => f.height && f.height <= targetH);
                        if (below.length) {
                            preferred = below.reduce((prev, curr) => (curr.height > prev.height ? curr : prev));
                        }
                    }
                }
                if (!preferred) {
                    preferred = videoPresets.find(f => (f.label && f.label.toLowerCase().includes('max quality')) || f.recommended) || videoPresets[0];
                }
            }
        } else {
            // Audio-only media (e.g. music track)
            preferred = presets.find(f => f.audio_only || f.tier === 'audio') || presets[0];
        }

        if (!preferred) {
            preferred = presets.find(f => f.recommended) || presets[0];
        }
        selectFormat(preferred);
    }

    function selectFormat(fmt) {
        if (!fmt) return;
        state.selected = fmt;

        $$('.q-pill').forEach(p => {
            const isActive = (fmt._pidx && p.dataset.pidx)
                ? p.dataset.pidx === fmt._pidx
                : p.dataset.fid === fmt.format_id;
            p.classList.toggle('active', isActive);
        });

        const label = $('#download-now-text');
        if (label) label.textContent = `Download ${fmt.sublabel || fmt.label || ''}`.trim();
    }

    function setupResultCard() {
        const dlBtn = $('#btn-download-now');
        if (dlBtn) {
            dlBtn.addEventListener('click', () => {
                if (state.selected) startDownload(state.selected.format_id);
            });
        }

        const toggleBtn = $('#btn-toggle-all-qualities');
        const toggleText = $('#toggle-qualities-text');
        const advSection = $('#all-qualities-section');
        if (toggleBtn && advSection) {
            toggleBtn.addEventListener('click', () => {
                const isExpanded = !advSection.classList.contains('hidden');
                if (isExpanded) {
                    advSection.classList.add('hidden');
                    toggleBtn.classList.remove('expanded');
                    const count = (state.currentRawFormats || []).length;
                    if (toggleText) toggleText.textContent = `All Formats (${count})`;
                } else {
                    advSection.classList.remove('hidden');
                    toggleBtn.classList.add('expanded');
                    if (toggleText) toggleText.textContent = 'Hide All Formats';
                }
            });
        }
    }

    // thumbnail error
    function setupThumbError() {
        const thumb = $('#result-thumb');
        if (thumb) {
            thumb.addEventListener('error', () => {
                thumb.closest('.media-thumb-wrap')?.classList.add('no-thumb');
            });
        }
    }

    // ── Download ───────────────────────────────
    async function startDownload(formatId) {
        if (!state.currentResult) return;
        const r = state.currentResult;
        // Prefer the object the user actually clicked: a preset selector and a
        // raw format can share an id (e.g. "best"), and the pill's own label,
        // size and sublabel are what should reach the download card.
        const fmt = (state.selected && state.selected.format_id === formatId)
            ? state.selected
            : ((r.formats || []).find(f => f.format_id === formatId) || {});
        const durVal = r.duration || r.duration_seconds || 0;

        const dlBtn = $('#btn-download-now');
        const dlText = $('#download-now-text');
        const prevText = dlText ? dlText.textContent : '';
        if (dlBtn) dlBtn.disabled = true;
        if (dlText) dlText.textContent = 'Starting…';

        // A merged (video+audio) selection has no single URL containing both
        // tracks — fmt.url points at the video-only stream, so handing it to
        // the backend as a fallback would produce a muted download.
        const selfContained = Boolean(fmt.url) && !fmt.needs_merge && fmt.has_audio !== false;

        const res = await api('/api/download', 'POST', {
            video_url: r.url,
            format_id: formatId,
            direct_url: selfContained ? fmt.url : '',
            needs_merge: !!fmt.needs_merge,
            has_audio: fmt.has_audio !== false,
            title: r.title || '',
            format_label: fmt.label || '',
            thumbnail: r.thumbnail || '',
            platform: r.platform || r.extractor || r.source_platform || '',
            quality: fmt.sublabel || (fmt.label ? fmt.label.split(' ')[0] : ''),
            duration: durVal,
            total_bytes: fmt.filesize_bytes || fmt.size || 0,
        });

        if (dlBtn) dlBtn.disabled = false;
        if (dlText) dlText.textContent = prevText || 'Download';

        if (res.ok) {
            if (res.download_id) {
                const optimistic = {
                    id: res.download_id,
                    url: r.url,
                    title: r.title || 'Starting download...',
                    thumbnail: r.thumbnail || '',
                    platform: r.platform || r.extractor || r.source_platform || '',
                    format_label: fmt.label || fmt.sublabel || '',
                    status: 'downloading',
                    status_detail: 'Connecting to stream...',
                    progress: 0,
                    downloaded_bytes: 0,
                    total_bytes: fmt.filesize_bytes || fmt.size || 0,
                    speed: 0,
                    eta: null,
                    created_at: Date.now() / 1000,
                };
                if (!state.downloads.some(d => d.id === res.download_id)) {
                    state.downloads = [optimistic, ...state.downloads];
                    applyDownloadUpdates(state.downloads);
                }
            }
            // Navigate to downloads page to show progress
            navigate('downloads');
            pollDownloads();
            updateNavBadge();
        } else {
            showError(res.message || 'Failed to start download', 'DOWNLOAD_ERROR');
        }
    }

    function applyDownloadUpdates(downloads) {
        if (!Array.isArray(downloads)) return;
        state.downloads = downloads;
        renderDownloadsPage(downloads);
        renderRecentDownloads(downloads);
        updateStatusBar(downloads);
        updateNavBadge(downloads);
    }

    function setupEventSource() {
        if (!window.EventSource) {
            startPolling();
            return;
        }

        try {
            if (state.eventSource) {
                try { state.eventSource.close(); } catch (_) {}
                state.eventSource = null;
            }

            const es = new EventSource('/api/downloads/stream');
            state.eventSource = es;

            es.onopen = () => {
                state.sseConnected = true;
                if (state.pollTimer) {
                    clearInterval(state.pollTimer);
                    state.pollTimer = null;
                }
            };

            es.onmessage = (e) => {
                try {
                    const data = JSON.parse(e.data);
                    if (data && data.downloads) {
                        applyDownloadUpdates(data.downloads);
                    }
                } catch (err) {
                    console.debug('SSE parse error:', err);
                }
            };

            es.onerror = () => {
                state.sseConnected = false;
                try { es.close(); } catch (_) {}
                state.eventSource = null;
                // Seamless fallback to polling
                startPolling();
                // Attempt to reconnect SSE after 5 seconds
                setTimeout(setupEventSource, 5000);
            };
        } catch (e) {
            console.debug('SSE initialization error:', e);
            startPolling();
        }
    }

    function startPolling() {
        if (state.pollTimer) clearInterval(state.pollTimer);
        state.pollTimer = setInterval(pollDownloads, 800);
    }

    async function pollDownloads() {
        const data = await api('/api/downloads');
        if (!data || !data.downloads) return;
        applyDownloadUpdates(data.downloads);
    }

    window._cancelDl = async function(id) {
        await api(`/api/download/cancel/${id}`, 'POST');
        pollDownloads();
    };

    window._pauseDl = async function(id) {
        await api(`/api/download/pause/${id}`, 'POST');
        pollDownloads();
    };

    window._resumeDl = async function(id) {
        await api(`/api/download/resume/${id}`, 'POST');
        pollDownloads();
    };

    window._retryDl = async function(id) {
        await api(`/api/download/retry/${encodeURIComponent(id)}`, 'POST');
        pollDownloads();
    };

    window._copyDlError = function(id) {
        const dl = (state.downloads || []).find(d => d.id === id);
        if (dl && dl.error) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(dl.error).then(() => {
                    toast('Error details copied to clipboard');
                }).catch(() => {
                    _fallbackCopy(dl.error);
                });
            } else {
                _fallbackCopy(dl.error);
            }
        } else {
            toast('No error details to copy');
        }
    };

    function _fallbackCopy(text) {
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            toast('Error details copied to clipboard');
        } catch (e) {
            toast('Failed to copy error details');
        }
    }

    window._moveQueueDl = async function(id, direction) {
        try {
            await api('/api/queue/move', 'POST', { download_id: id, direction: direction });
            pollDownloads();
        } catch (err) {
            console.error('Failed to move task in queue:', err);
        }
    };

    window._removeDl = async function(id) {
        await api(`/api/download/remove/${encodeURIComponent(id)}`, 'POST');
        pollDownloads();
    };

    window._removeRecentDl = async function(id) {
        try {
            const raw = localStorage.getItem('savitar_recent_downloads');
            if (raw) {
                const list = JSON.parse(raw);
                if (Array.isArray(list)) {
                    const filtered = list.filter(item => item.id !== id);
                    localStorage.setItem('savitar_recent_downloads', JSON.stringify(filtered));
                }
            }
        } catch (_) {}

        if (id) {
            await api(`/api/download/remove/${encodeURIComponent(id)}`, 'POST');
        }

        state.downloads = (state.downloads || []).filter(d => d.id !== id);
        recentSig = null;
        renderRecentDownloads(state.downloads);
        renderDownloadsPage(state.downloads);
        updateStatusBar(state.downloads);
        updateNavBadge(state.downloads);
    };

    window._openExternal = async function(url) {
        if (!url) return;
        try {
            await api('/api/app/open-url', 'POST', { url });
        } catch (_) {
            window.open(url, '_blank');
        }
    };

    window._playDl = async function(id) {
        const res = await api(`/api/play/${encodeURIComponent(id)}`, 'POST');
        if (!res || !res.ok) {
            toast((res && res.message) || 'Could not play media file.');
        }
    };

    let _lastRevealTime = 0;

    window._revealDl = async function(id, filePath) {
        const now = Date.now();
        if (now - _lastRevealTime < 1000) {
            return;
        }
        _lastRevealTime = now;
        if (!filePath && id && typeof state !== 'undefined' && state.downloads) {
            const item = state.downloads.find(d => d.id === id);
            if (item && item.file_path) {
                filePath = item.file_path;
            }
        }
        if (!id && filePath) {
            return window._revealPath(filePath);
        }
        const res = await api(`/api/reveal/${encodeURIComponent(id || 'current')}`, 'POST', { file_path: filePath || '' });
        if (!res || !res.ok) {
            if (filePath) {
                await window._revealPath(filePath);
                return;
            }
            toast((res && res.message) || 'Could not open the download folder.');
        }
    };

    window._revealPath = async function(filePath) {
        const now = Date.now();
        if (now - _lastRevealTime < 1000) {
            return;
        }
        _lastRevealTime = now;
        const res = await api('/api/reveal/path', 'POST', { file_path: filePath || '' });
        if (!res || !res.ok) {
            toast((res && res.message) || 'Could not open the containing folder.');
        }
    };

    function setupDownloadActions() {
        const clearBtn = $('#btn-clear-downloads');
        if (clearBtn) {
            clearBtn.addEventListener('click', async () => {
                await api('/api/downloads/clear?only_completed=true', 'POST');
                localStorage.removeItem('savitar_recent_downloads');
                recentSig = null;
                pollDownloads();
            });
        }

        const clearIncompleteBtn = $('#btn-clear-incomplete');
        if (clearIncompleteBtn) {
            clearIncompleteBtn.addEventListener('click', async () => {
                await api('/api/downloads/clear-incomplete', 'POST');
                pollDownloads();
            });
        }

        const pauseResumeAllBtn = $('#btn-pause-resume-all');
        if (pauseResumeAllBtn) {
            pauseResumeAllBtn.addEventListener('click', async () => {
                const mode = pauseResumeAllBtn.dataset.mode || 'pause';
                if (mode === 'pause') {
                    await api('/api/downloads/pause-all', 'POST');
                } else {
                    await api('/api/downloads/resume-all', 'POST');
                }
                pollDownloads();
            });
        }

        document.addEventListener('click', (e) => {
            const btn = e.target.closest('.btn-open-folder');
            if (!btn) return;
            const card = btn.closest('.dl-card');
            const id = card ? card.getAttribute('data-dlid') : null;
            if (id) {
                window._revealDl(id);
            }
        });
    }

    function setupDownloadsToolbar() {
        const searchInput = $('#dl-search-input');
        const clearBtn = $('#btn-dl-search-clear');
        const filterChips = $$('.dl-filter-chip');
        const sortSelect = $('#dl-sort-select');
        const speedSelect = $('#dl-speed-select');

        if (searchInput) {
            searchInput.addEventListener('input', () => {
                state.downloadsSearch = searchInput.value;
                if (clearBtn) {
                    clearBtn.classList.toggle('hidden', !searchInput.value);
                }
                renderDownloadsPage(state.downloads);
            });
        }

        if (clearBtn && searchInput) {
            clearBtn.addEventListener('click', () => {
                searchInput.value = '';
                state.downloadsSearch = '';
                clearBtn.classList.add('hidden');
                renderDownloadsPage(state.downloads);
            });
        }

        filterChips.forEach(chip => {
            chip.addEventListener('click', () => {
                filterChips.forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                state.downloadsFilter = chip.dataset.filter || 'all';
                renderDownloadsPage(state.downloads);
            });
        });

        if (sortSelect) {
            sortSelect.addEventListener('change', () => {
                state.downloadsSort = sortSelect.value || 'default';
                renderDownloadsPage(state.downloads);
            });
        }

        if (speedSelect) {
            if (state.settings && state.settings.speed_limit_kbps !== undefined) {
                speedSelect.value = String(state.settings.speed_limit_kbps);
            }
            speedSelect.addEventListener('change', async () => {
                const limit = parseInt(speedSelect.value, 10) || 0;
                await api('/api/settings', 'POST', { speed_limit_kbps: limit });
                const slSelect = $('#setting-speed-limit');
                if (slSelect) slSelect.value = String(limit);
                toast(`Speed limit set to: ${speedSelect.options[speedSelect.selectedIndex].text}`);
            });
        }
    }

    function setupFormatFilterTabs() {
        const tabs = $$('#format-filter-tabs .fft-btn');
        tabs.forEach(btn => {
            btn.addEventListener('click', () => {
                tabs.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.formatTab = btn.dataset.fmtTab || 'all';
                if (state.currentPresets) {
                    renderQualityPills(state.currentPresets, state.currentRawFormats);
                }
            });
        });
    }

    // ── Render Downloads (full page) ───────────
    function renderDownloadsPage(downloads) {
        const container = $('#downloads-container');
        const empty = $('#empty-downloads');
        if (!container) return;

        const pauseResumeAllBtn = $('#btn-pause-resume-all');
        const iconPauseResumeAll = $('#icon-pause-resume-all');
        const textPauseResumeAll = $('#text-pause-resume-all');

        if (pauseResumeAllBtn) {
            const hasActive = (downloads || []).some(d => ['downloading', 'preparing', 'retrying', 'pending'].includes(d.status));
            const hasPaused = (downloads || []).some(d => d.status === 'paused');

            if (hasActive) {
                pauseResumeAllBtn.disabled = false;
                pauseResumeAllBtn.dataset.mode = 'pause';
                if (iconPauseResumeAll) iconPauseResumeAll.textContent = '⏸';
                if (textPauseResumeAll) textPauseResumeAll.textContent = 'Pause All';
                pauseResumeAllBtn.title = 'Pause all active and pending downloads';
            } else if (hasPaused) {
                pauseResumeAllBtn.disabled = false;
                pauseResumeAllBtn.dataset.mode = 'resume';
                if (iconPauseResumeAll) iconPauseResumeAll.textContent = '▶';
                if (textPauseResumeAll) textPauseResumeAll.textContent = 'Resume All';
                pauseResumeAllBtn.title = 'Resume all paused downloads';
            } else {
                pauseResumeAllBtn.disabled = true;
                pauseResumeAllBtn.dataset.mode = 'pause';
                if (iconPauseResumeAll) iconPauseResumeAll.textContent = '⏸';
                if (textPauseResumeAll) textPauseResumeAll.textContent = 'Pause All';
                pauseResumeAllBtn.title = 'No active downloads to pause';
            }
        }

        const clearIncompleteBtn = $('#btn-clear-incomplete');
        if (clearIncompleteBtn) {
            const hasIncomplete = (downloads || []).some(d => ['failed', 'cancelled', 'paused'].includes(d.status));
            clearIncompleteBtn.disabled = !hasIncomplete;
            clearIncompleteBtn.style.opacity = hasIncomplete ? '1' : '0.45';
            clearIncompleteBtn.style.cursor = hasIncomplete ? 'pointer' : 'not-allowed';
        }

        const clearCompletedBtn = $('#btn-clear-downloads');
        if (clearCompletedBtn) {
            const hasCompleted = (downloads || []).some(d => d.status === 'completed');
            clearCompletedBtn.disabled = !hasCompleted;
            clearCompletedBtn.style.opacity = hasCompleted ? '1' : '0.45';
            clearCompletedBtn.style.cursor = hasCompleted ? 'pointer' : 'not-allowed';
        }

        // Update toolbar filter counts
        const countAll = (downloads || []).length;
        const countActive = (downloads || []).filter(d => ACTIVE_STATUSES.includes(d.status) || d.status === 'pending' || d.status === 'paused').length;
        const countCompleted = (downloads || []).filter(d => d.status === 'completed').length;
        const countFailed = (downloads || []).filter(d => d.status === 'failed' || d.status === 'cancelled').length;

        const cAll = $('#dl-count-all'); if (cAll) cAll.textContent = countAll;
        const cAct = $('#dl-count-active'); if (cAct) cAct.textContent = countActive;
        const cComp = $('#dl-count-completed'); if (cComp) cComp.textContent = countCompleted;
        const cFail = $('#dl-count-failed'); if (cFail) cFail.textContent = countFailed;

        if (!downloads || downloads.length === 0) {
            container.innerHTML = '';
            if (empty) container.appendChild(empty);
            if (empty) empty.style.display = '';
            return;
        }

        // Apply status filter
        let filtered = downloads || [];
        if (state.downloadsFilter === 'active') {
            filtered = filtered.filter(d => ACTIVE_STATUSES.includes(d.status) || d.status === 'pending' || d.status === 'paused');
        } else if (state.downloadsFilter === 'completed') {
            filtered = filtered.filter(d => d.status === 'completed');
        } else if (state.downloadsFilter === 'failed') {
            filtered = filtered.filter(d => d.status === 'failed' || d.status === 'cancelled');
        }

        // Apply search query
        const query = (state.downloadsSearch || '').trim().toLowerCase();
        if (query) {
            filtered = filtered.filter(d => {
                const title = (d.title || '').toLowerCase();
                const platform = (d.platform || '').toLowerCase();
                const fmt = (d.format_label || '').toLowerCase();
                const err = (d.error || '').toLowerCase();
                return title.includes(query) || platform.includes(query) || fmt.includes(query) || err.includes(query);
            });
        }

        if (filtered.length === 0) {
            container.innerHTML = `<div class="empty-downloads" style="padding:48px 0;text-align:center;">
                <p style="color:var(--text-2);font-weight:600;">No downloads found</p>
                <span style="color:var(--text-3);font-size:12px;">Try changing your search query or status filter.</span>
            </div>`;
            return;
        }

        if (empty) empty.style.display = 'none';

        const existing = new Map();
        container.querySelectorAll('[data-dlid]').forEach(el => existing.set(el.dataset.dlid, el));

        const newIds = new Set(filtered.map(d => d.id));
        existing.forEach((el, id) => { if (!newIds.has(id)) el.remove(); });

        // Apply sorting
        let sortedDownloads = [];
        if (state.downloadsSort === 'newest') {
            sortedDownloads = [...filtered].reverse();
        } else if (state.downloadsSort === 'oldest') {
            sortedDownloads = [...filtered];
        } else if (state.downloadsSort === 'size') {
            sortedDownloads = [...filtered].sort((a, b) => {
                const sA = a.total_bytes || a.downloaded_bytes || 0;
                const sB = b.total_bytes || b.downloaded_bytes || 0;
                return sB - sA;
            });
        } else {
            // Default logical ordering: Active -> Pending (queue order) -> Paused -> Finished (newest first)
            const activeList = [];
            const pendingList = [];
            const pausedList = [];
            const finishedList = [];

            filtered.forEach(d => {
                if (ACTIVE_STATUSES.includes(d.status)) {
                    activeList.push(d);
                } else if (d.status === 'pending') {
                    pendingList.push(d);
                } else if (d.status === 'paused') {
                    pausedList.push(d);
                } else {
                    finishedList.push(d);
                }
            });

            pendingList.sort((a, b) => {
                const posA = a.queue_position || 999999;
                const posB = b.queue_position || 999999;
                return posA - posB;
            });

            finishedList.reverse();
            sortedDownloads = [...activeList, ...pendingList, ...pausedList, ...finishedList];
        }

        sortedDownloads.forEach((dl, index) => {
            let el = existing.get(dl.id);
            if (!el) {
                el = document.createElement('div');
                el.className = 'dl-card';
                el.setAttribute('data-dlid', dl.id);
            }
            const currentAtIdx = container.children[index];
            if (currentAtIdx !== el) {
                container.insertBefore(el, currentAtIdx || null);
            }
            updateDlCard(el, dl);
        });
    }

    function updateDlCard(el, dl) {
        const pct = dl.progress || 0;
        // "preparing" (waiting on ffmpeg) and "retrying" (backoff) are working
        // states: they get the pause/cancel controls, and they are labelled for
        // what they are instead of the misleading catch-all "Pending".
        const isActive = ACTIVE_STATUSES.includes(dl.status);
        const isPaused = dl.status === 'paused';
        const isDone = dl.status === 'completed';
        const isFail = dl.status === 'failed';
        const isCancelled = dl.status === 'cancelled';

        // Polling runs every 800ms. Rewriting innerHTML when nothing changed
        // restarts CSS transitions and drops whatever the pointer was hovering,
        // which reads as the card flickering under the cursor.
        const sig = [dl.status, pct, dl.speed_bps, dl.downloaded_bytes, dl.total_bytes,
                     dl.error, dl.engine, dl.format_label, dl.title, dl.thumbnail, dl.queue_position].join('|');
        if (el.dataset.sig === sig) return;
        el.dataset.sig = sig;

        let statusText = 'Queued';
        let statusClass = 'pending';
        if (dl.status === 'pending') {
            statusText = dl.batch_id ? '⏳ In Sequence' : 'Queued';
            statusClass = 'pending';
        }
        else if (dl.status === 'merging') { statusText = '⚙ Merging…'; statusClass = 'active'; }
        else if (dl.status === 'downloading') {
            const displayPct = typeof pct === 'number' ? (pct % 1 === 0 ? pct : pct.toFixed(1)) : pct;
            statusText = `↓ ${displayPct}%`;
            statusClass = 'active';
        }
        else if (dl.status === 'preparing') { statusText = '⚡ Connecting…'; statusClass = 'active'; }
        else if (dl.status === 'retrying') { statusText = '↻ Retrying…'; statusClass = 'retrying'; }
        else if (isPaused)   { statusText = '⏸ Paused';    statusClass = 'paused'; }
        else if (isDone)     { statusText = '✓ Completed'; statusClass = 'completed'; }
        else if (isFail)     { statusText = '✕ Failed';    statusClass = 'failed'; }
        else if (isCancelled){ statusText = 'Cancelled';   statusClass = 'cancelled'; }

        const barW = isDone ? 100 : pct;
        const shimmer = (dl.status === 'downloading' || dl.status === 'merging' || dl.status === 'preparing') ? 'shimmer' : '';

        let actionsHTML = '';
        if (isActive) {
            actionsHTML = `<button class="btn-pause-dl" title="Pause download" onclick="window._pauseDl('${esc(dl.id)}')">⏸</button>`
                        + `<button class="btn-cancel-dl" title="Cancel download" onclick="window._cancelDl('${esc(dl.id)}')">×</button>`;
        } else if (isPaused) {
            actionsHTML = `<button class="btn-resume-dl" title="Resume download" onclick="window._resumeDl('${esc(dl.id)}')">▶</button>`
                        + `<button class="btn-cancel-dl" title="Cancel download" onclick="window._cancelDl('${esc(dl.id)}')">×</button>`;
        } else if (dl.status === 'pending') {
            actionsHTML = `<button class="btn-queue-up" title="Move up in queue" onclick="window._moveQueueDl('${esc(dl.id)}', 'up')">▲</button>`
                        + `<button class="btn-queue-down" title="Move down in queue" onclick="window._moveQueueDl('${esc(dl.id)}', 'down')">▼</button>`
                        + `<button class="btn-cancel-dl" title="Cancel download" onclick="window._cancelDl('${esc(dl.id)}')">×</button>`;
        } else if (isDone) {
            actionsHTML = `<button class="btn-open-folder" title="Show in folder">${FOLDER_ICON}</button>`;
        } else if (isFail || isCancelled) {
            const copyBtn = (isFail && dl.error) ? `<button class="btn-copy-error" title="Copy error details" onclick="window._copyDlError('${esc(dl.id)}')">📋 Copy</button>` : '';
            actionsHTML = copyBtn + `<button class="btn-retry-dl" title="Retry download" onclick="window._retryDl('${esc(dl.id)}')">↻ Retry</button>`;
        }

        let thumbHTML = `<div class="dl-thumb-fallback">▶</div>`;
        if (dl.thumbnail) {
            thumbHTML = `<img src="${esc(dl.thumbnail)}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='grid'">
                         <div class="dl-thumb-fallback" style="display:none">▶</div>`;
        }

        // Download mode badge
        const engineBadge = dl.engine === 'aria2c'
            ? `<span class="dl-meta-badge" title="Turbo Multi-Threaded Download">⚡ Turbo</span>`
            : (dl.engine === 'direct'
                ? `<span class="dl-meta-badge" title="Direct Fast Download">⚡ Direct</span>`
                : ``);
        const qPos = dl.queue_position ? `#${dl.queue_position}` : '';
        const seqBadge = (dl.batch_id && dl.status === 'pending')
            ? `<span class="dl-meta-badge" title="Waiting for previous download in sequence to complete">⏳ In Sequence ${qPos}</span>`
            : (dl.status === 'pending' && dl.queue_position
                ? `<span class="dl-meta-badge" title="Queue Position">#${dl.queue_position} in Queue</span>`
                : '');

        let detailsHTML = '';
        if (dl.status === 'merging') {
            const tot = formatBytes(dl.total_bytes || dl.downloaded_bytes);
            detailsHTML = `<span>Processing & Merging…</span><span>${tot !== '—' ? tot : ''}</span>`;
        } else if (dl.status === 'preparing') {
            detailsHTML = `<span>Establishing high-speed stream connection…</span><span></span>`;
        } else if (dl.status === 'retrying') {
            detailsHTML = `<span>${esc(dl.error || 'Retrying…')}</span><span></span>`;
        } else if (dl.status === 'pending') {
            const posLabel = dl.queue_position ? ` #${dl.queue_position}` : '';
            detailsHTML = dl.batch_id
                ? `<span>Waiting for turn in sequence${posLabel}…</span><span>Queued</span>`
                : `<span>Waiting in queue${posLabel}…</span><span>Queued</span>`;
        } else if (isActive) {
            let etaText = '';
            if (dl.eta_seconds && dl.eta_seconds > 0) {
                const etaM = Math.floor(dl.eta_seconds / 60);
                const etaS = dl.eta_seconds % 60;
                etaText = etaM > 0 ? ` • ETA ${etaM}m ${String(etaS).padStart(2, '0')}s` : ` • ETA ${etaS}s`;
            }
            const dlFormatted = formatBytes(dl.downloaded_bytes);
            const totFormatted = formatBytes(dl.total_bytes);
            const sizeDisplay = (dl.total_bytes > 0)
                ? `${formatBytes(Math.min(dl.downloaded_bytes, dl.total_bytes))} / ${totFormatted}`
                : (dl.downloaded_bytes > 0 ? `${dlFormatted}` : '—');

            detailsHTML = `<span>${formatSpeed(dl.speed_bps)}${etaText}</span>
                           <span>${sizeDisplay}</span>`;
        } else if (isPaused) {
            const dlFormatted = formatBytes(dl.downloaded_bytes);
            const totFormatted = formatBytes(dl.total_bytes);
            const sizeDisplay = (dl.total_bytes > 0) ? `${dlFormatted} / ${totFormatted}` : `${dlFormatted}`;
            detailsHTML = `<span>Paused</span><span>${sizeDisplay}</span>`;
        } else if (isFail) {
            detailsHTML = `<span style="color:var(--error);font-size:11px">${esc(dl.error || 'Download failed')}</span><span></span>`;
        } else {
            detailsHTML = `<span>${statusText}</span><span>${formatBytes(dl.total_bytes || dl.downloaded_bytes)}</span>`;
        }

        // If card structure is missing, build it once
        if (!el.querySelector('.dl-info')) {
            el.innerHTML = `
                <button class="btn-card-close" title="Remove from list" onclick="window._removeDl('${esc(dl.id)}')">×</button>
                <div class="dl-thumb-mini">${thumbHTML}</div>
                <div class="dl-info">
                    <div class="dl-title-row">
                        <span class="dl-name" title="${esc(dl.title)}">${esc(dl.title)}</span>
                        <span class="dl-status-pill ${statusClass}">${statusText}</span>
                    </div>
                    <div class="dl-meta-row">
                        ${dl.format_label ? `<span class="dl-meta-badge">${esc(dl.format_label)}</span>` : ''}
                        ${engineBadge}
                        ${seqBadge}
                    </div>
                    <div class="dl-progress-wrap">
                        <div class="dl-prog-bar"><div class="dl-prog-fill ${shimmer}" style="width:${barW}%"></div></div>
                        <div class="dl-details-row">${detailsHTML}</div>
                    </div>
                </div>
                <div class="dl-actions">${actionsHTML}</div>
            `;
            return;
        }

        // Fast in-place DOM updates to keep CSS transitions continuous and eliminate flickering
        const fillEl = el.querySelector('.dl-prog-fill');
        if (fillEl) {
            fillEl.style.width = `${barW}%`;
            fillEl.className = `dl-prog-fill ${shimmer}`;
        }

        const statusPill = el.querySelector('.dl-status-pill');
        if (statusPill && (statusPill.textContent !== statusText || !statusPill.classList.contains(statusClass))) {
            statusPill.textContent = statusText;
            statusPill.className = `dl-status-pill ${statusClass}`;
        }

        const detailsRow = el.querySelector('.dl-details-row');
        if (detailsRow) {
            detailsRow.innerHTML = detailsHTML;
        }

        const actionsDiv = el.querySelector('.dl-actions');
        if (actionsDiv && actionsDiv.dataset.status !== dl.status) {
            actionsDiv.innerHTML = actionsHTML;
            actionsDiv.dataset.status = dl.status;
        }

        const nameEl = el.querySelector('.dl-name');
        if (nameEl && dl.title && nameEl.textContent !== dl.title) {
            nameEl.textContent = dl.title;
            nameEl.title = dl.title;
        }
    }

    // ── Render Recent Grid (Home page) ─────────
    // The empty-state node is cached: rebuilding the grid detaches it, and a
    // naive innerHTML='' would delete the only reference.
    let noRecentEl = null;
    let recentSig = null;

    function renderRecentDownloads(downloads) {
        const grid = $('#recent-grid');
        if (!grid) return;
        if (!noRecentEl) noRecentEl = $('#no-recent');

        let completed = (downloads || []).filter(d => d.status === 'completed');
        let recentItems = [];

        if (completed.length) {
            // Newest completed downloads come first!
            recentItems = [...completed].reverse();
            try {
                localStorage.setItem('savitar_recent_downloads', JSON.stringify(recentItems.slice(0, 16)));
            } catch (_) {}
        } else {
            try {
                const raw = localStorage.getItem('savitar_recent_downloads');
                if (raw) {
                    const saved = JSON.parse(raw);
                    if (Array.isArray(saved) && saved.length) {
                        recentItems = saved;
                    }
                }
            } catch (_) {}
        }

        const visible = recentItems.slice(0, 4);
        const sig = visible.map(d => `${d.id}:${d.title}:${d.thumbnail}`).join('|');
        if (sig === recentSig && grid.children.length > 0) return;
        recentSig = sig;

        grid.innerHTML = '';

        if (!visible.length) {
            if (noRecentEl) {
                noRecentEl.style.display = '';
                grid.appendChild(noRecentEl);
            }
            return;
        }

        visible.forEach(dl => {
            const card = document.createElement('div');
            card.className = 'recent-card';
            card.title = 'Show in folder';

            const thumbWrapClass = dl.thumbnail ? '' : 'no-img';

            card.innerHTML = `
                <button class="btn-recent-close" title="Remove from history" type="button" aria-label="Remove">×</button>
                <div class="recent-thumb-wrap ${thumbWrapClass}">
                    ${dl.thumbnail ? `<img src="${esc(dl.thumbnail)}" alt="">` : ''}
                    <div class="recent-thumb-fallback">▶</div>
                    <div class="recent-play-overlay">
                        <div class="recent-play-btn">${FOLDER_ICON}</div>
                    </div>
                    ${dl.duration ? `<span class="recent-dur">${formatDuration(dl.duration)}</span>` : ''}
                </div>
                <div class="recent-card-body">
                    <div class="recent-card-title" title="${esc(dl.title)}">${esc(dl.title)}</div>
                    <div class="recent-card-meta">${esc(dl.platform || 'Video')} ${dl.quality ? '• ' + esc(dl.quality) : ''}</div>
                    <div class="recent-card-badges">
                        <span class="rc-badge">${esc(dl.format_label || 'MP4')}</span>
                        ${dl.quality ? `<span class="rc-badge">${esc(dl.quality)}</span>` : ''}
                        <span class="rc-badge success">✓ Completed</span>
                    </div>
                </div>
            `;

            const closeBtn = card.querySelector('.btn-recent-close');
            if (closeBtn) {
                closeBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window._removeRecentDl(dl.id);
                });
            }

            card.addEventListener('click', () => {
                window._revealDl(dl.id, dl.file_path);
            });
            grid.appendChild(card);
        });
    }

    // ── Status Bar ─────────────────────────────
    function updateStatusBar(downloads) {
        const total = downloads.length;
        const active = downloads.filter(d => ACTIVE_STATUSES.includes(d.status));

        $('#stat-count').textContent = total;
        $('#stat-active').textContent = active.length;

        if (active.length > 0) {
            const totalSpeed = active.reduce((acc, d) => acc + (d.speed_bps || 0), 0);
            const totalDownloaded = active.reduce((acc, d) => acc + (d.downloaded_bytes || 0), 0);
            const totalTarget = active.reduce((acc, d) => acc + (d.total_bytes || 0), 0);

            let avgPct = 0;
            if (active.length === 1) {
                const single = active[0];
                if (single.progress > 0) {
                    avgPct = Math.round(single.progress);
                } else if (totalTarget > 0 && totalDownloaded > 0) {
                    avgPct = Math.min(99, Math.round((totalDownloaded / totalTarget) * 100));
                }
            } else {
                avgPct = Math.round(active.reduce((acc, d) => acc + (d.progress || 0), 0) / active.length);
            }
            avgPct = Math.min(100, Math.max(0, avgPct));

            const isAllMerging = active.every(d => d.status === 'merging');
            const isAllPreparing = active.every(d => d.status === 'preparing');

            $('#stat-speed').textContent = isAllMerging ? 'Merging…' : formatSpeed(totalSpeed);
            $('#stat-progress-wrap')?.classList.remove('hidden');

            if (isAllMerging) {
                $('#stat-progress-label').textContent = '⚙ Processing & Merging…';
            } else if (isAllPreparing) {
                $('#stat-progress-label').textContent = '⚡ Connecting to stream…';
            } else if (active.length > 1) {
                $('#stat-progress-label').textContent = `Downloading (${active.length} active)… ${avgPct}%`;
            } else {
                $('#stat-progress-label').textContent = `Downloading… ${avgPct}%`;
            }

            $('#stat-bar-fill').style.width = avgPct + '%';

            if (totalTarget > 0) {
                const displayDl = Math.min(totalDownloaded, totalTarget);
                $('#stat-size').textContent = `${formatBytes(displayDl)} / ${formatBytes(totalTarget)}`;
            } else if (totalDownloaded > 0) {
                $('#stat-size').textContent = `${formatBytes(totalDownloaded)}`;
            } else {
                $('#stat-size').textContent = '—';
            }
        } else {
            $('#stat-speed').textContent = '0 KB/s';
            $('#stat-progress-wrap')?.classList.add('hidden');
        }
    }

    // ── Nav Badge ──────────────────────────────
    function updateNavBadge(downloads) {
        const badge = $('#nav-badge');
        if (!badge) return;
        const arr = downloads || state.downloads;
        const active = arr.filter(d => ACTIVE_STATUSES.includes(d.status) || d.status === 'pending');
        if (active.length > 0) {
            badge.textContent = active.length;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    // ── Settings ───────────────────────────────
    // Files are organized as <download path>\Savitar\<Platform>, so the hint
    // under the input shows the exact folder a finished download lands in.
    function updateFolderHint() {
        const hint = $('#folder-hint');
        const input = $('#setting-folder');
        if (!hint || !input) return;
        const base = (input.value || '').trim().replace(/[\\/]+$/, '');
        hint.textContent = base
            ? `Files are saved to: ${base}\\Savitar\\<Platform>`
            : 'Using your system Downloads folder. Files are saved to: Downloads\\Savitar\\<Platform>';
    }

    async function loadBrowsers(selectedBrowser = '') {
        const data = await api('/api/browsers');
        const sel = $('#setting-browser');
        if (!sel) return;
        sel.innerHTML = '<option value="">None</option>';
        if (data && Array.isArray(data.browsers)) {
            data.browsers.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b.id;
                opt.textContent = `${b.name}${b.available ? '' : ' (Not Found)'}`;
                if (!b.available) opt.disabled = true;
                sel.appendChild(opt);
            });
        }
        if (selectedBrowser) {
            sel.value = selectedBrowser;
        }
    }

    async function loadSettings() {
        const data = await api('/api/settings');
        if (!data) return;
        state.settings = data;
        const folderEl = $('#setting-folder');
        const cookiesEl = $('#setting-cookies-file');
        const quickEl = $('#setting-quick-shortcut');
        const autostartEl = $('#setting-autostart');
        const taskmgrWarningEl = $('#autostart-taskmgr-warning');
        if (folderEl) folderEl.value = data.download_folder || '';
        if (cookiesEl) cookiesEl.value = data.cookies_file || '';
        if (quickEl) quickEl.checked = data.quick_download_shortcut !== false;
        if (autostartEl) autostartEl.checked = data.start_with_windows !== false;
        if (taskmgrWarningEl) {
            taskmgrWarningEl.classList.toggle('hidden', !data.autostart_task_manager_disabled);
        }
        updateFolderHint();
        await loadBrowsers(data.cookies_browser || '');

        // Highlight preferred quality dropdown & pills
        const pq = (data.preferred_quality || 'best').toLowerCase();
        const pqSelect = $('#setting-preferred-quality');
        if (pqSelect) pqSelect.value = pq;
        $$('#quality-pref-pills .qp-pill').forEach(p => {
            p.classList.toggle('active', p.dataset.qval === pq);
        });

        // Highlight speed limit dropdown
        const slSelect = $('#setting-speed-limit');
        if (slSelect && data.speed_limit_kbps !== undefined) {
            slSelect.value = String(data.speed_limit_kbps);
        }

        // Concurrency slider
        const concSlider = $('#setting-concurrency');
        const concVal = $('#concurrency-val');
        if (concSlider && data.max_concurrent_downloads !== undefined) {
            concSlider.value = data.max_concurrent_downloads;
            if (concVal) concVal.textContent = data.max_concurrent_downloads;
        }

        // Disk space badge
        const diskBadge = $('#setting-disk-badge');
        if (diskBadge) {
            api('/api/system/disk-space').then(disk => {
                if (disk && disk.ok) {
                    diskBadge.textContent = `💾 Free Space: ${disk.free_gb} GB of ${disk.total_gb} GB`;
                    diskBadge.style.color = (disk.free_gb < 2) ? 'var(--error)' : 'var(--accent)';
                }
            }).catch(() => {});
        }
    }

    function setupSettings() {
        const saveBtn = $('#btn-save-settings');
        if (saveBtn) {
            saveBtn.addEventListener('click', saveSettings);
        }

        // Choose folder button in Settings
        const chooseFolderBtn = $('#btn-choose-folder');
        if (chooseFolderBtn) {
            chooseFolderBtn.addEventListener('click', async () => {
                const res = await api('/api/dialog/folder');
                if (res && res.ok && res.path) {
                    const input = $('#setting-folder');
                    if (input) {
                        input.value = res.path;
                        updateFolderHint();
                        await saveSettings();
                    }
                }
            });
        }

        // Choose cookies file button in Settings
        const chooseCookiesBtn = $('#btn-choose-cookies-file');
        if (chooseCookiesBtn) {
            chooseCookiesBtn.addEventListener('click', async () => {
                const res = await api('/api/dialog/file?kind=cookies');
                if (res && res.ok && res.path) {
                    const input = $('#setting-cookies-file');
                    if (input) {
                        input.value = res.path;
                        await saveSettings();
                    }
                }
            });
        }

        // Clear cookies file button
        const clearCookiesBtn = $('#btn-clear-cookies-file');
        if (clearCookiesBtn) {
            clearCookiesBtn.addEventListener('click', async () => {
                const input = $('#setting-cookies-file');
                if (input) {
                    input.value = '';
                    await saveSettings();
                }
            });
        }

        // Browser select auto-save
        const browserSel = $('#setting-browser');
        if (browserSel) {
            browserSel.addEventListener('change', () => saveSettings());
        }

        // Global shortcut toggle auto-save
        const quickToggle = $('#setting-quick-shortcut');
        if (quickToggle) {
            quickToggle.addEventListener('change', async () => {
                await saveSettings();
                toast(quickToggle.checked ? 'Global shortcut (Ctrl + Shift + D) enabled.' : 'Global shortcut disabled.');
            });
        }

        // Start with Windows toggle auto-save
        const autostartToggle = $('#setting-autostart');
        if (autostartToggle) {
            autostartToggle.addEventListener('change', async () => {
                await saveSettings();
                toast(autostartToggle.checked ? 'Savitar will start with Windows in the background.' : 'Start with Windows disabled.');
            });
        }

        // Open the current download folder straight from Settings
        const openFolderBtn = $('#btn-open-folder');
        if (openFolderBtn) {
            openFolderBtn.addEventListener('click', async () => {
                const raw = ($('#setting-folder')?.value || '').trim();
                const res = await api('/api/reveal/path', 'POST', { file_path: raw });
                if (!res || !res.ok) {
                    toast((res && res.message) || 'Could not open the download folder.');
                }
            });
        }

        // Keep the save-location hint in sync while typing
        const folderInput = $('#setting-folder');
        if (folderInput) {
            folderInput.addEventListener('input', updateFolderHint);
            folderInput.addEventListener('change', updateFolderHint);
        }

        // Theme buttons in settings page
        $$('.theme-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                applyTheme(btn.dataset.themeVal);
            });
        });

        // Theme select in sidebar
        const themeSelect = $('#theme-select');
        if (themeSelect) {
            themeSelect.addEventListener('change', () => {
                applyTheme(themeSelect.value);
            });
        }

        // Settings nav click opens Settings page
        const btnErrorSettings = $('#btn-error-settings');
        if (btnErrorSettings) {
            btnErrorSettings.addEventListener('click', () => navigate('settings'));
        }

        // Quality preference dropdown — auto-save on change
        const pqSelect = $('#setting-preferred-quality');
        if (pqSelect) {
            pqSelect.addEventListener('change', async () => {
                await saveSettings();
                const selOpt = pqSelect.options[pqSelect.selectedIndex];
                toast(`Download quality set to: ${selOpt ? selOpt.text : pqSelect.value}`);
            });
        }

        // Quality preference pills (fallback) — auto-save on click
        $$('#quality-pref-pills .qp-pill').forEach(pill => {
            pill.addEventListener('click', async () => {
                $$('#quality-pref-pills .qp-pill').forEach(p => p.classList.remove('active'));
                pill.classList.add('active');
                if (pqSelect) pqSelect.value = pill.dataset.qval;
                await saveSettings();
                const labels = {
                    max: '⚡ Maximum Quality (Enhanced)',
                    best: 'Best available',
                    '1080p': '1080p Full HD',
                    '720p': '720p HD',
                    '480p': '480p SD',
                    '360p': '360p Low',
                    audio: 'Audio Only (MP3)',
                };
                toast(`Download quality set to: ${labels[pill.dataset.qval] || pill.dataset.qval}`);
            });
        });

        // Speed limit dropdown — auto-save on change
        const slSelect = $('#setting-speed-limit');
        if (slSelect) {
            slSelect.addEventListener('change', async () => {
                await saveSettings();
                const selOpt = slSelect.options[slSelect.selectedIndex];
                toast(`Speed limit set to: ${selOpt ? selOpt.text : slSelect.value}`);
            });
        }

        // Concurrency slider — auto-save on change
        const concSlider = $('#setting-concurrency');
        const concVal = $('#concurrency-val');
        if (concSlider) {
            concSlider.addEventListener('input', () => {
                if (concVal) concVal.textContent = concSlider.value;
            });
            concSlider.addEventListener('change', async () => {
                await saveSettings();
                toast(`Concurrent downloads set to: ${concSlider.value}`);
            });
        }
    }

    async function saveSettings() {
        const btn = $('#btn-save-settings');
        const quickToggle = $('#setting-quick-shortcut');
        const autostartToggle = $('#setting-autostart');
        const pqSelect = $('#setting-preferred-quality');
        const activePill = $('#quality-pref-pills .qp-pill.active');
        const preferredQuality = pqSelect ? pqSelect.value : (activePill ? activePill.dataset.qval : 'best');
        const slSelect = $('#setting-speed-limit');
        const speedLimitKbps = slSelect ? parseInt(slSelect.value, 10) || 0 : 0;
        const concSlider = $('#setting-concurrency');
        const maxConcurrent = concSlider ? parseInt(concSlider.value, 10) || 5 : 5;
        const payload = {
            download_folder: $('#setting-folder')?.value.trim() || '',
            cookies_browser: $('#setting-browser')?.value || '',
            cookies_file: $('#setting-cookies-file')?.value.trim() || '',
            quick_download_shortcut: quickToggle ? quickToggle.checked : true,
            start_with_windows: autostartToggle ? autostartToggle.checked : false,
            preferred_quality: preferredQuality,
            speed_limit_kbps: speedLimitKbps,
            max_concurrent_downloads: maxConcurrent,
        };

        if (btn) btn.textContent = 'Saving…';
        const res = await api('/api/settings', 'POST', payload);
        if (res && res.ok) {
            if (btn) {
                btn.textContent = 'Saved!';
                setTimeout(() => { btn.innerHTML = '✓ Save Settings'; }, 1500);
            }
        } else {
            if (btn) btn.textContent = 'Error';
            toast('Could not save settings.');
        }
    }

    // ── Convert Page ───────────────────────────
    function setupConvert() {
        // Format pills toggle
        $$('.format-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                $$('.format-pill').forEach(p => p.classList.remove('active'));
                pill.classList.add('active');
                checkConvertReady();
            });
        });

        // Browse file button
        const browseBtn = $('#btn-browse-file');
        if (browseBtn) {
            browseBtn.addEventListener('click', async () => {
                const res = await api('/api/dialog/file');
                if (res && res.ok && res.path) {
                    const src = $('#convert-source');
                    if (src) {
                        src.value = res.path;
                    }
                    checkConvertReady();
                }
            });
        }

        const sourceInput = $('#convert-source');
        if (sourceInput) {
            sourceInput.addEventListener('input', checkConvertReady);
        }

        const startBtn = $('#btn-start-convert');
        if (startBtn) {
            startBtn.addEventListener('click', async () => {
                const src = $('#convert-source')?.value.trim();
                const fmt = document.querySelector('.format-pill.active')?.dataset.format || 'mp3';
                if (!src) {
                    toast('Please select a media file to convert.');
                    return;
                }

                const prevHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg> Start Convert`;
                startBtn.innerHTML = `
                    <svg class="spin-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                        <line x1="12" y1="2" x2="12" y2="6"/>
                        <line x1="12" y1="18" x2="12" y2="22"/>
                        <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/>
                        <line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
                        <line x1="2" y1="12" x2="6" y2="12"/>
                        <line x1="18" y1="12" x2="22" y2="12"/>
                        <line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/>
                        <line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/>
                    </svg> Converting to ${fmt.toUpperCase()}…`;
                startBtn.disabled = true;

                try {
                    const res = await api('/api/convert', 'POST', {
                        source_path: src,
                        target_format: fmt,
                    });

                    if (res && res.ok) {
                        toast(`✓ Converted to ${fmt.toUpperCase()} successfully!`);
                        addConvertedItem(res);
                        const sizeStr = res.size_bytes ? ` (${formatBytes(res.size_bytes)})` : '';
                        startBtn.innerHTML = `✓ Done! Converted to ${fmt.toUpperCase()}${sizeStr}`;

                        setTimeout(() => {
                            startBtn.innerHTML = prevHTML;
                            startBtn.disabled = !$('#convert-source')?.value.trim();
                        }, 2200);
                    } else {
                        toast((res && res.message) || 'Conversion failed. Please try again.');
                        startBtn.innerHTML = prevHTML;
                        startBtn.disabled = false;
                    }
                } catch (err) {
                    toast(`Conversion error: ${err.message || err}`);
                    startBtn.innerHTML = prevHTML;
                    startBtn.disabled = false;
                }
            });
        }
    }

    function checkConvertReady() {
        const src = $('#convert-source')?.value.trim();
        const btn = $('#btn-start-convert');
        if (btn) btn.disabled = !src;
    }

    // ── Converted Files History ─────────────────
    function loadConvertedItems() {
        try {
            const raw = localStorage.getItem('savitar_converted');
            if (raw) state.convertedItems = JSON.parse(raw);
        } catch (_) {}
        renderConvertedList();
    }

    function saveConvertedItems() {
        try {
            localStorage.setItem('savitar_converted', JSON.stringify(state.convertedItems));
        } catch (_) {}
    }

    function addConvertedItem(item) {
        state.convertedItems.unshift({
            filename: item.filename || '',
            output_path: item.output_path || '',
            size_bytes: item.size_bytes || 0,
            target_format: item.target_format || '',
            timestamp: Date.now(),
        });
        // Keep max 50 items
        if (state.convertedItems.length > 50) state.convertedItems.length = 50;
        saveConvertedItems();
        renderConvertedList();
    }

    function renderConvertedList() {
        const container = $('#converted-list');
        const empty = $('#empty-converted');
        const history = $('#converted-history');
        if (!container) return;

        // Remove old cards
        container.querySelectorAll('.cvt-card').forEach(el => el.remove());

        if (!state.convertedItems.length) {
            if (empty) empty.style.display = '';
            if (history) history.classList.remove('has-items');
            return;
        }

        if (empty) empty.style.display = 'none';
        if (history) history.classList.add('has-items');

        state.convertedItems.forEach((item, idx) => {
            const card = document.createElement('div');
            card.className = 'cvt-card';
            card.title = 'Click to open in folder';

            const ext = (item.target_format || '').toUpperCase();
            const sizeStr = item.size_bytes ? formatBytes(item.size_bytes) : '';
            const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleString() : '';

            card.innerHTML = `
                <div class="cvt-card-icon">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                    </svg>
                </div>
                <div class="cvt-card-info">
                    <span class="cvt-card-name" title="${esc(item.output_path || item.filename)}">${esc(item.filename)}</span>
                    <span class="cvt-card-meta">
                        <span class="cvt-format-badge">${esc(ext)}</span>
                        ${sizeStr ? `<span class="cvt-size">${esc(sizeStr)}</span>` : ''}
                        ${timeStr ? `<span class="cvt-time">${esc(timeStr)}</span>` : ''}
                    </span>
                </div>
                <div class="cvt-card-actions">
                    <button class="cvt-action-btn cvt-reveal-btn" title="Open folder">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                        </svg>
                    </button>
                    <button class="cvt-action-btn cvt-remove-btn" title="Remove">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
                            <line x1="18" y1="6" x2="6" y2="18"/>
                            <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                    </button>
                </div>
            `;

            // Open folder on card or reveal button click
            card.addEventListener('click', (e) => {
                if (e.target.closest('.cvt-remove-btn')) return;
                if (item && item.output_path) {
                    window._revealPath(item.output_path);
                }
            });

            // Remove button
            const removeBtn = card.querySelector('.cvt-remove-btn');
            if (removeBtn) {
                removeBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const curIdx = state.convertedItems.indexOf(item);
                    if (curIdx !== -1) {
                        state.convertedItems.splice(curIdx, 1);
                        saveConvertedItems();
                        renderConvertedList();
                    }
                });
            }

            container.appendChild(card);
        });
    }

    function setupConvertedHistory() {
        const clearBtn = $('#btn-clear-converted');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                state.convertedItems = [];
                saveConvertedItems();
                renderConvertedList();
            });
        }
        loadConvertedItems();
    }

    // ── Engines (yt-dlp / aria2c / ffmpeg) ─────
    const ENGINE_KEYS = ['ytdlp', 'aria2c', 'ffmpeg'];

    async function loadEngines() {
        const data = await api('/api/engines');
        if (!data) return;
        renderEngines(data);
    }

    function renderEngines(data) {
        const missing = ENGINE_KEYS.filter(k => !(data[k] || {}).installed);

        // Update Settings page engine status
        const engineTitle = $('#engine-status-title');
        const engineDetail = $('#engine-status-detail');
        if (engineTitle) {
            if (missing.length) {
                engineTitle.textContent = 'Engines updating or missing';
                if (engineDetail) engineDetail.textContent = 'Click "Update all" to install or update all required components.';
            } else {
                engineTitle.textContent = 'All engines are up to date';
                if (engineDetail) engineDetail.textContent = 'All core download and conversion components are up to date.';
            }
        }

        // Update about page engine status (no individual branding)
        const aboutStatus = $('#about-engine-status');
        if (aboutStatus) {
            if (missing.length) {
                aboutStatus.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> Some engines need attention`;
                aboutStatus.classList.add('status-warning');
                aboutStatus.classList.remove('status-ok');
            } else {
                aboutStatus.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> All engines are up to date`;
                aboutStatus.classList.add('status-ok');
                aboutStatus.classList.remove('status-warning');
            }
        }
    }

    function setupEngines() {
        const allBtn = $('#btn-update-engines');
        if (allBtn) allBtn.addEventListener('click', () => runEngineUpdate(allBtn, ''));
    }

    async function runEngineUpdate(btn, tool) {
        const prev = btn.textContent;
        const buttons = $$('.engine-btn');
        buttons.forEach(b => { b.disabled = true; });
        btn.textContent = 'Working…';

        const url = tool ? `/api/engines/update?tool=${encodeURIComponent(tool)}` : '/api/engines/update';
        const res = await api(url, 'POST');

        buttons.forEach(b => { b.disabled = false; });
        btn.textContent = res && res.ok ? 'Done' : (res ? 'Failed' : prev);
        setTimeout(() => { btn.textContent = prev; }, 2200);

        if (res && res.status) renderEngines(res.status);
        else loadEngines();

        const status = $('#ytdlp-status');
        if (status && res && res.message) status.textContent = res.message;
    }

    // ── Playlist & Channel ─────────────────────
    function isPlaylistUrl(url) {
        try {
            const clean = (url || '').trim();
            const u = new URL(clean.startsWith('http') ? clean : 'https://' + clean);
            const host = u.hostname.toLowerCase();
            if (host.includes('youtube.com') || host.includes('youtu.be')) {
                if (u.searchParams.has('list') && u.searchParams.get('list')) return true;
                const p = u.pathname.toLowerCase();
                if (p.startsWith('/@') || p.startsWith('/channel/') || p.startsWith('/c/') || p.startsWith('/user/') || p.includes('/playlist')) return true;
            }
        } catch (_) {}
        return false;
    }

    function setupPlaylist() {
        const input = $('#playlist-url-input');
        const btn = $('#btn-playlist-extract');
        const pasteBtn = $('#btn-playlist-paste');
        const clearBtn = $('#btn-playlist-clear-url');
        const dlBtn = $('#btn-download-playlist');
        const selectAllCb = $('#pl-select-all');
        const searchInput = $('#pl-search-input');
        const errSettingsBtn = $('#btn-playlist-error-settings');

        // Paste button
        if (pasteBtn && input) {
            pasteBtn.addEventListener('click', async () => {
                const text = await readClipboard();
                const trimmed = text.trim();
                if (trimmed) {
                    input.value = trimmed;
                    updatePlaylistClearBtn();
                    input.focus();
                    if (/^(https?:\/\/|www\.)/i.test(trimmed)) {
                        doPlaylistExtract(trimmed);
                    }
                } else {
                    input.focus();
                }
            });
        }

        // Clear button
        if (clearBtn && input) {
            clearBtn.addEventListener('click', () => {
                input.value = '';
                updatePlaylistClearBtn();
                hideAllPlaylistViews();
                input.focus();
            });
        }

        function updatePlaylistClearBtn() {
            if (!clearBtn || !input) return;
            clearBtn.classList.toggle('hidden', !input.value.trim());
        }

        let plPasteDebounce = null;
        if (input) {
            input.addEventListener('input', updatePlaylistClearBtn);
            input.addEventListener('paste', () => {
                setTimeout(() => {
                    updatePlaylistClearBtn();
                    const val = input.value.trim();
                    if (/^(https?:\/\/|www\.)/i.test(val)) {
                        if (plPasteDebounce) clearTimeout(plPasteDebounce);
                        plPasteDebounce = setTimeout(() => doPlaylistExtract(val), 120);
                    }
                }, 30);
            });
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doPlaylistExtract();
            });
        }

        if (btn) {
            btn.addEventListener('click', () => doPlaylistExtract());
        }

        // Hint chips
        $$('.playlist-hint-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const sample = chip.dataset.sample;
                if (input && sample) {
                    input.value = sample;
                    updatePlaylistClearBtn();
                    doPlaylistExtract(sample);
                }
            });
        });

        // Quality Preset Pills
        $$('#playlist-quality-pills .q-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                $$('#playlist-quality-pills .q-pill').forEach(p => p.classList.remove('active'));
                pill.classList.add('active');
                state.playlistFormatId = pill.dataset.fid || 'bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best';
                state.playlistFormatLabel = pill.dataset.label || pill.textContent.trim();
            });
        });

        // Download Mode Switcher (All at once vs One by one)
        $$('#dl-mode-switcher .dl-mode-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                $$('#dl-mode-switcher .dl-mode-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.playlistDownloadMode = btn.dataset.mode || 'all';
            });
        });

        // Main Download Selected Button
        if (dlBtn) {
            dlBtn.addEventListener('click', startPlaylistDownload);
        }

        // Select All / None / Range Quick Buttons
        if (selectAllCb) {
            selectAllCb.addEventListener('change', () => {
                const checked = selectAllCb.checked;
                const visibleEntries = getFilteredPlaylistEntries();
                visibleEntries.forEach(entry => {
                    if (checked) {
                        state.selectedPlaylistIds.add(entry.id);
                    } else {
                        state.selectedPlaylistIds.delete(entry.id);
                    }
                });
                updatePlaylistSelectionUI();
            });
        }

        const btnAll = $('#btn-pl-select-all');
        if (btnAll) {
            btnAll.addEventListener('click', () => {
                if (!state.playlistResult) return;
                state.playlistResult.entries.forEach(e => state.selectedPlaylistIds.add(e.id));
                updatePlaylistSelectionUI();
            });
        }

        const btnNone = $('#btn-pl-select-none');
        if (btnNone) {
            btnNone.addEventListener('click', () => {
                state.selectedPlaylistIds.clear();
                updatePlaylistSelectionUI();
            });
        }

        const btn10 = $('#btn-pl-select-10');
        if (btn10) {
            btn10.addEventListener('click', () => {
                if (!state.playlistResult) return;
                state.selectedPlaylistIds.clear();
                state.playlistResult.entries.slice(0, 10).forEach(e => state.selectedPlaylistIds.add(e.id));
                updatePlaylistSelectionUI();
            });
        }

        const btn25 = $('#btn-pl-select-25');
        if (btn25) {
            btn25.addEventListener('click', () => {
                if (!state.playlistResult) return;
                state.selectedPlaylistIds.clear();
                state.playlistResult.entries.slice(0, 25).forEach(e => state.selectedPlaylistIds.add(e.id));
                updatePlaylistSelectionUI();
            });
        }

        // Search filter
        if (searchInput) {
            searchInput.addEventListener('input', () => {
                state.playlistSearchQuery = searchInput.value.trim().toLowerCase();
                renderPlaylistItems();
            });
        }

        // Settings error button
        if (errSettingsBtn) {
            errSettingsBtn.addEventListener('click', () => navigate('settings'));
        }
    }

    function hideAllPlaylistViews() {
        ['#playlist-skeleton', '#playlist-error-state', '#playlist-result-view'].forEach(sel => {
            $(sel)?.classList.add('hidden');
        });
    }

    function showPlaylistError(msg, code) {
        hideAllPlaylistViews();
        const title = $('#playlist-error-title');
        const msgEl = $('#playlist-error-message');
        const settBtn = $('#btn-playlist-error-settings');
        const isLoginRequired = code === 'PRIVATE_OR_LOGIN_REQUIRED';

        if (title) title.textContent = isLoginRequired ? 'Login Required' : 'Playlist Extraction Failed';
        if (msgEl) msgEl.textContent = msg || 'Could not fetch playlist or channel videos.';
        if (settBtn) settBtn.classList.toggle('hidden', !isLoginRequired);
        $('#playlist-error-state')?.classList.remove('hidden');
    }

    async function doPlaylistExtract(urlOverride) {
        const input = $('#playlist-url-input');
        const url = (urlOverride || input?.value || '').trim();
        if (!url) return;

        hideAllPlaylistViews();
        $('#playlist-skeleton')?.classList.remove('hidden');

        const btn = $('#btn-playlist-extract');
        if (btn) btn.disabled = true;

        const scanLimitEl = $('#playlist-scan-limit');
        const maxItems = scanLimitEl ? (parseInt(scanLimitEl.value, 10)) : 100;
        const data = await api('/api/playlist/extract', 'POST', { url, max_items: isNaN(maxItems) ? 100 : maxItems });

        if (btn) btn.disabled = false;
        $('#playlist-skeleton')?.classList.add('hidden');

        if (data.ok && data.entries && data.entries.length) {
            state.playlistResult = data;
            state.selectedPlaylistIds = new Set(data.entries.map(e => e.id));
            state.playlistSearchQuery = '';
            const searchInput = $('#pl-search-input');
            if (searchInput) searchInput.value = '';
            renderPlaylistResult(data);
        } else {
            showPlaylistError(data.message || 'Could not load playlist.', data.code || 'ERROR');
        }
    }

    function getFilteredPlaylistEntries() {
        if (!state.playlistResult || !state.playlistResult.entries) return [];
        const q = state.playlistSearchQuery;
        if (!q) return state.playlistResult.entries;
        return state.playlistResult.entries.filter(e => {
            const title = (e.title || '').toLowerCase();
            const author = (e.author || '').toLowerCase();
            return title.includes(q) || author.includes(q);
        });
    }

    function renderPlaylistResult(data) {
        const thumb = $('#pl-thumb');
        const fallback = $('#pl-thumb-fallback');
        if (thumb) {
            thumb.src = data.thumbnail || '';
            thumb.onerror = () => {
                thumb.style.display = 'none';
                if (fallback) fallback.style.display = 'grid';
            };
            thumb.onload = () => {
                thumb.style.display = 'block';
                if (fallback) fallback.style.display = 'none';
            };
        }

        const typeBadge = $('#pl-type-badge');
        if (typeBadge) {
            typeBadge.textContent = (data.type || 'playlist').toUpperCase();
        }

        const titleEl = $('#pl-title');
        if (titleEl) {
            titleEl.textContent = data.title || 'YouTube Playlist';
            titleEl.title = data.title || '';
        }

        const authorEl = $('#pl-author');
        if (authorEl) authorEl.textContent = data.author || 'YouTube';

        const countEl = $('#pl-count');
        if (countEl) countEl.textContent = `${data.item_count || data.entries.length} videos`;

        renderPlaylistItems();
        updatePlaylistSelectionUI();

        $('#playlist-result-view')?.classList.remove('hidden');
    }

    function renderPlaylistItems() {
        const list = $('#playlist-items-list');
        if (!list) return;
        list.innerHTML = '';

        const entries = getFilteredPlaylistEntries();
        if (!entries.length) {
            list.innerHTML = `
                <div class="playlist-empty-search">
                    <p>No videos matching "<strong>${esc(state.playlistSearchQuery)}</strong>"</p>
                </div>
            `;
            return;
        }

        entries.forEach(entry => {
            const isSelected = state.selectedPlaylistIds.has(entry.id);
            const row = document.createElement('div');
            row.className = `playlist-item-row ${isSelected ? 'selected' : ''}`;
            row.dataset.id = entry.id;

            const durStr = formatDuration(entry.duration);

            row.innerHTML = `
                <label class="pl-item-cb-wrap" onclick="event.stopPropagation()">
                    <input type="checkbox" class="pl-item-cb" data-id="${esc(entry.id)}" ${isSelected ? 'checked' : ''}>
                </label>
                <div class="pl-item-index">${entry.index}</div>
                <div class="pl-item-thumb-wrap">
                    ${entry.thumbnail ? `<img src="${esc(entry.thumbnail)}" alt="" onerror="this.style.display='none'">` : ''}
                    <div class="pl-item-thumb-fallback">▶</div>
                    ${entry.duration ? `<span class="pl-item-duration">${durStr}</span>` : ''}
                </div>
                <div class="pl-item-details">
                    <div class="pl-item-title" title="${esc(entry.title)}">${esc(entry.title)}</div>
                    <div class="pl-item-meta">
                        ${entry.author ? `<span>${esc(entry.author)}</span>` : ''}
                        ${entry.duration ? `<span class="meta-dot">•</span><span>${durStr}</span>` : ''}
                    </div>
                </div>
                <div class="pl-item-actions">
                    <button class="pl-single-dl-btn" title="Download this video now" type="button">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                            <polyline points="7 10 12 15 17 10"/>
                            <line x1="12" y1="15" x2="12" y2="3"/>
                        </svg>
                        Download
                    </button>
                </div>
            `;

            // Row click toggles selection checkbox
            row.addEventListener('click', (e) => {
                if (e.target.closest('.pl-single-dl-btn') || e.target.closest('.pl-item-cb')) return;
                togglePlaylistEntry(entry.id);
            });

            const cb = row.querySelector('.pl-item-cb');
            if (cb) {
                cb.addEventListener('change', () => togglePlaylistEntry(entry.id));
            }

            // Single video download button
            const singleBtn = row.querySelector('.pl-single-dl-btn');
            if (singleBtn) {
                singleBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    downloadSinglePlaylistItem(entry);
                });
            }

            list.appendChild(row);
        });

        updatePlaylistSelectionUI();
    }

    function togglePlaylistEntry(id) {
        if (state.selectedPlaylistIds.has(id)) {
            state.selectedPlaylistIds.delete(id);
        } else {
            state.selectedPlaylistIds.add(id);
        }
        updatePlaylistSelectionUI();
    }

    function updatePlaylistSelectionUI() {
        const total = state.playlistResult?.entries?.length || 0;
        const selectedCount = state.selectedPlaylistIds.size;

        const countLabel = $('#pl-selected-count-label');
        if (countLabel) {
            countLabel.textContent = `${selectedCount} of ${total} selected`;
        }

        const selectAllCb = $('#pl-select-all');
        if (selectAllCb) {
            selectAllCb.checked = total > 0 && selectedCount === total;
            selectAllCb.indeterminate = selectedCount > 0 && selectedCount < total;
        }

        const dlBtnText = $('#pl-download-btn-text');
        if (dlBtnText) {
            dlBtnText.textContent = `Download Selected (${selectedCount})`;
        }

        const dlBtn = $('#btn-download-playlist');
        if (dlBtn) {
            dlBtn.disabled = selectedCount === 0;
        }

        // Update row visual active states
        $$('.playlist-item-row').forEach(row => {
            const id = row.dataset.id;
            const isChecked = state.selectedPlaylistIds.has(id);
            row.classList.toggle('selected', isChecked);
            const cb = row.querySelector('.pl-item-cb');
            if (cb) cb.checked = isChecked;
        });
    }

    async function downloadSinglePlaylistItem(entry) {
        if (!entry) return;
        toast(`Adding "${entry.title}" to downloads...`);

        const res = await api('/api/download', 'POST', {
            video_url: entry.url,
            format_id: state.playlistFormatId || 'bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best',
            title: entry.title,
            format_label: state.playlistFormatLabel || 'Best Available',
            thumbnail: entry.thumbnail || '',
            platform: 'youtube',
            quality: '',
            duration: entry.duration || 0,
        });

        if (res.ok) {
            if (res.download_id) {
                const optimistic = {
                    id: res.download_id,
                    url: entry.url,
                    title: entry.title || 'Starting download...',
                    thumbnail: entry.thumbnail || '',
                    platform: 'youtube',
                    format_label: state.playlistFormatLabel || 'Best Available',
                    status: 'downloading',
                    status_detail: 'Connecting to stream...',
                    progress: 0,
                    downloaded_bytes: 0,
                    total_bytes: 0,
                    speed: 0,
                    eta: null,
                    created_at: Date.now() / 1000,
                };
                if (!state.downloads.some(d => d.id === res.download_id)) {
                    state.downloads = [optimistic, ...state.downloads];
                    applyDownloadUpdates(state.downloads);
                }
            }
            navigate('downloads');
            pollDownloads();
            updateNavBadge();
        } else {
            toast(res.message || 'Failed to start download');
        }
    }

    async function startPlaylistDownload() {
        if (!state.playlistResult) return;
        const selectedEntries = state.playlistResult.entries.filter(e => state.selectedPlaylistIds.has(e.id));
        if (!selectedEntries.length) {
            toast('Please select at least one video to download.');
            return;
        }

        const btn = $('#btn-download-playlist');
        const btnText = $('#pl-download-btn-text');
        const prevText = btnText ? btnText.textContent : '';
        if (btn) btn.disabled = true;
        if (btnText) btnText.textContent = 'Enqueuing...';

        const subfolder = $('#pl-opt-subfolder')?.checked ?? true;
        const numbered = $('#pl-opt-numbering')?.checked ?? true;
        const sequential = state.playlistDownloadMode === 'sequential';

        const payload = {
            playlist_title: state.playlistResult.title || 'YouTube Playlist',
            playlist_id: state.playlistResult.id || '',
            format_id: state.playlistFormatId || 'bestvideo*+bestaudio[ext=m4a]/bestvideo*+bestaudio/best',
            format_label: state.playlistFormatLabel || 'Best Available',
            subfolder: subfolder,
            numbered: numbered,
            sequential: sequential,
            items: selectedEntries.map((e, idx) => ({
                video_url: e.url,
                title: e.title,
                thumbnail: e.thumbnail || '',
                duration: e.duration || 0,
                index: numbered ? (idx + 1) : 0,
            })),
        };

        const res = await api('/api/playlist/download', 'POST', payload);

        if (btn) btn.disabled = false;
        if (btnText) btnText.textContent = prevText;

        if (res.ok) {
            toast(`Enqueued ${res.count || selectedEntries.length} videos from "${state.playlistResult.title}"`);
            navigate('downloads');
            pollDownloads();
            updateNavBadge();
        } else {
            toast(res.message || 'Failed to start batch download.');
        }
    }

    // ── Input Context Menu (Right Click Support) ─
    // ── Input Context Menu (Right Click Support) ─
    function setupContextMenu() {
        let activeMenu = null;

        function closeMenu() {
            if (activeMenu) {
                activeMenu.remove();
                activeMenu = null;
            }
        }

        document.addEventListener('click', (e) => {
            if (activeMenu && !activeMenu.contains(e.target)) {
                closeMenu();
            }
        });

        window.addEventListener('blur', closeMenu);
        window.addEventListener('resize', closeMenu);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeMenu();
        });

        async function triggerMenu(e) {
            // Check button
            if (e.type === 'mouseup' && e.button !== 2) return;
            if (e.type === 'auxclick' && e.button !== 2) return;

            let target = e.target;
            if (!target) return;

            // Find closest input or textarea
            if (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA') {
                target = target.closest('.url-box, .file-input-wrap, .folder-input-wrap, .convert-form, .settings-card, .playlist-hero, .playlist-toolbar')?.querySelector('input, textarea')
                    || target.querySelector('input, textarea')
                    || (state.currentPage === 'home' ? $('#url-input') : (state.currentPage === 'playlist' ? $('#playlist-url-input') : null));
            }

            if (!target || (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA')) {
                return;
            }

            if (e.preventDefault) e.preventDefault();
            if (e.stopPropagation) e.stopPropagation();
            if (e.stopImmediatePropagation) e.stopImmediatePropagation();
            closeMenu();

            const isReadonly = target.readOnly || target.disabled;
            const selStart = target.selectionStart ?? 0;
            const selEnd = target.selectionEnd ?? 0;
            const hasSelection = selStart !== selEnd;
            const selectedText = hasSelection ? target.value.substring(selStart, selEnd) : '';
            const hasText = Boolean(target.value && target.value.length > 0);

            const menu = document.createElement('div');
            menu.className = 'app-context-menu';

            // Paste item
            const pasteBtn = document.createElement('button');
            pasteBtn.type = 'button';
            pasteBtn.className = 'ctx-menu-item';
            pasteBtn.disabled = isReadonly;
            pasteBtn.innerHTML = `
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>
                    <rect x="8" y="2" width="8" height="4" rx="1" ry="1"/>
                </svg>
                <span>Paste</span>
            `;
            pasteBtn.addEventListener('click', async () => {
                closeMenu();
                // readClipboard() goes through the backend's Windows API first,
                // so no clipboard permission prompt ever appears.
                const clipboardText = await readClipboard();

                if (clipboardText) {
                    target.focus();
                    if (hasSelection) {
                        const before = target.value.substring(0, selStart);
                        const after = target.value.substring(selEnd);
                        target.value = before + clipboardText + after;
                        target.selectionStart = target.selectionEnd = selStart + clipboardText.length;
                    } else {
                        const pos = (target.selectionStart !== null && target.selectionStart !== undefined) ? target.selectionStart : target.value.length;
                        const before = target.value.substring(0, pos);
                        const after = target.value.substring(pos);
                        target.value = before + clipboardText + after;
                        target.selectionStart = target.selectionEnd = pos + clipboardText.length;
                    }
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    target.focus();
                    toast('Clipboard is empty');
                }
            });
            menu.appendChild(pasteBtn);

            // Copy item
            const copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'ctx-menu-item';
            copyBtn.disabled = !hasSelection;
            copyBtn.innerHTML = `
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>
                <span>Copy</span>
            `;
            copyBtn.addEventListener('click', async () => {
                closeMenu();
                if (selectedText) {
                    try {
                        await navigator.clipboard.writeText(selectedText);
                        toast('Copied to clipboard');
                    } catch (_) {
                        document.execCommand('copy');
                    }
                }
            });
            menu.appendChild(copyBtn);

            // Cut item
            const cutBtn = document.createElement('button');
            cutBtn.type = 'button';
            cutBtn.className = 'ctx-menu-item';
            cutBtn.disabled = isReadonly || !hasSelection;
            cutBtn.innerHTML = `
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="6" cy="6" r="3"/>
                    <circle cx="6" cy="18" r="3"/>
                    <line x1="20" y1="4" x2="8.12" y2="15.88"/>
                    <line x1="14.47" y1="14.48" x2="20" y2="20"/>
                    <line x1="8.12" y1="8.12" x2="12" y2="12"/>
                </svg>
                <span>Cut</span>
            `;
            cutBtn.addEventListener('click', async () => {
                closeMenu();
                if (selectedText) {
                    try {
                        await navigator.clipboard.writeText(selectedText);
                    } catch (_) {}
                    const before = target.value.substring(0, selStart);
                    const after = target.value.substring(selEnd);
                    target.value = before + after;
                    target.selectionStart = target.selectionEnd = selStart;
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
            menu.appendChild(cutBtn);

            // Divider
            const div = document.createElement('div');
            div.className = 'ctx-menu-divider';
            menu.appendChild(div);

            // Select All item
            const selAllBtn = document.createElement('button');
            selAllBtn.type = 'button';
            selAllBtn.className = 'ctx-menu-item';
            selAllBtn.disabled = !hasText;
            selAllBtn.innerHTML = `
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M3 3h18v18H3zM9 9h6v6H9z"/>
                </svg>
                <span>Select All</span>
            `;
            selAllBtn.addEventListener('click', () => {
                closeMenu();
                target.focus();
                target.select();
            });
            menu.appendChild(selAllBtn);

            // Clear item
            const clearBtn = document.createElement('button');
            clearBtn.type = 'button';
            clearBtn.className = 'ctx-menu-item';
            clearBtn.disabled = isReadonly || !hasText;
            clearBtn.innerHTML = `
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
                <span>Clear</span>
            `;
            clearBtn.addEventListener('click', () => {
                closeMenu();
                target.value = '';
                target.focus();
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
            });
            menu.appendChild(clearBtn);

            document.body.appendChild(menu);
            activeMenu = menu;

            // Position menu within viewport
            const menuW = 180;
            const menuH = 190;
            let posX = e.clientX || (e.touches && e.touches[0]?.clientX) || 100;
            let posY = e.clientY || (e.touches && e.touches[0]?.clientY) || 100;

            if (posX + menuW > window.innerWidth) {
                posX = window.innerWidth - menuW - 10;
            }
            if (posY + menuH > window.innerHeight) {
                posY = window.innerHeight - menuH - 10;
            }

            menu.style.left = `${Math.max(10, posX)}px`;
            menu.style.top = `${Math.max(10, posY)}px`;
        }

        document.addEventListener('contextmenu', triggerMenu, true);
        document.addEventListener('auxclick', triggerMenu, true);
    }

    // ── App Update Checker (GitHub Releases) ────
    function setupAppUpdater() {
        const overlay = $('#update-modal-overlay');
        const backdrop = $('#update-modal-backdrop');
        const closeBtn = $('#btn-update-close');
        const laterBtn = $('#btn-update-later');
        const updateNowBtn = $('#btn-update-now');
        const checkAppUpdateBtn = $('#btn-check-app-update');
        const appVersionEl = $('#about-app-version');
        const heroVersionEl = $('#about-version');
        const versionBadge = $('#update-version-badge');
        const changelogEl = $('#update-changelog-content');
        const subtitleEl = $('#update-subtitle-text');

        let currentUpdateInfo = null;

        function closeModal() {
            if (overlay) overlay.classList.add('hidden');
        }

        function dismissUpdate() {
            closeModal();
            if (currentUpdateInfo && currentUpdateInfo.latest_version) {
                try {
                    localStorage.setItem('savitar_dismissed_update', JSON.stringify({
                        version: currentUpdateInfo.latest_version,
                        time: Date.now()
                    }));
                } catch (_) {}
            }
        }

        function formatChangelog(text) {
            if (!text || !text.trim()) {
                return '<ul><li>New performance improvements and download engine optimizations.</li><li>Bug fixes and overall stability improvements.</li></ul>';
            }
            const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
            const listItems = [];
            for (const line of lines) {
                if (line.startsWith('#')) continue; // skip markdown headers
                const clean = line.replace(/^[\*\-\•]\s*/, '').trim();
                if (clean) {
                    listItems.push(`<li>${esc(clean)}</li>`);
                }
            }
            if (listItems.length) {
                return `<ul>${listItems.slice(0, 8).join('')}</ul>`;
            }
            return `<p>${esc(text.slice(0, 300))}</p>`;
        }

        async function checkAppUpdate(isManual = false) {
            if (isManual && checkAppUpdateBtn) {
                checkAppUpdateBtn.disabled = true;
                checkAppUpdateBtn.innerHTML = `<span>Checking...</span>`;
            }

            try {
                const res = await api('/api/app/check-update');
                if (res && res.current_version) {
                    if (appVersionEl) appVersionEl.textContent = `v${res.current_version}`;
                    if (heroVersionEl) heroVersionEl.textContent = `v${res.current_version}`;
                }

                if (res && res.ok && res.update_available) {
                    currentUpdateInfo = res;

                    // Cooldown check for automatic check
                    if (!isManual) {
                        try {
                            const dismissed = JSON.parse(localStorage.getItem('savitar_dismissed_update') || '{}');
                            const ONE_DAY = 24 * 60 * 60 * 1000;
                            if (dismissed.version === res.latest_version && (Date.now() - dismissed.time < ONE_DAY)) {
                                return; // User opted for "Remind Me Later" recently
                            }
                        } catch (_) {}
                    }

                    if (versionBadge) versionBadge.textContent = `v${res.latest_version || res.tag_name}`;
                    if (subtitleEl) {
                        subtitleEl.textContent = res.release_name && res.release_name !== res.tag_name
                            ? res.release_name
                            : `A new version (v${res.latest_version}) of Savitar is available!`;
                    }
                    if (changelogEl) changelogEl.innerHTML = formatChangelog(res.release_notes);

                    if (overlay) overlay.classList.remove('hidden');
                } else if (isManual) {
                    if (res && res.error) {
                        toast(res.error);
                    } else {
                        toast('Savitar is already up to date!');
                    }
                }
            } catch (err) {
                if (isManual) toast('Could not connect to update server.');
            } finally {
                if (isManual && checkAppUpdateBtn) {
                    checkAppUpdateBtn.disabled = false;
                    checkAppUpdateBtn.innerHTML = `
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
                        <span>Check for Updates</span>
                    `;
                }
            }
        }

        if (closeBtn) closeBtn.addEventListener('click', dismissUpdate);
        if (backdrop) backdrop.addEventListener('click', dismissUpdate);
        if (laterBtn) laterBtn.addEventListener('click', dismissUpdate);

        if (updateNowBtn) {
            updateNowBtn.addEventListener('click', async () => {
                const url = (currentUpdateInfo && (currentUpdateInfo.download_url || currentUpdateInfo.release_url))
                    || 'https://github.com/kamstackbuild/Savitar/releases/latest';

                await api('/api/app/open-url', 'POST', { url });
                closeModal();
            });
        }

        if (checkAppUpdateBtn) {
            checkAppUpdateBtn.addEventListener('click', () => checkAppUpdate(true));
        }

        // Automatic background check 2.5 seconds after app startup
        setTimeout(() => checkAppUpdate(false), 2500);
    }

    // ── Init ───────────────────────────────────
    async function init() {
        loadTheme();
        setupNav();
        setupExtract();
        setupPlaylist();
        setupThumbError();
        setupResultCard();
        setupFormatFilterTabs();
        setupDownloadActions();
        setupDownloadsToolbar();
        setupSettings();
        setupEngines();
        setupConvert();
        setupConvertedHistory();
        setupContextMenu();
        setupAppUpdater();

        // Load data
        Promise.all([
            loadPlatforms(),
            loadSettings(),
            loadBrowsers(),
            loadEngines(),
            pollDownloads(),
        ]).catch(() => {});

        setupEventSource();
    }

    init();
})();

