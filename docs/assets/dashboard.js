// Shared dashboard interactions. Safe to load on every page.
// Version: 2.0 - Environment-aware dependencies (updated 2026-05-22)
(function () {
    'use strict';

    // Make every `.section-title` a clickable toggle that collapses everything
    // else inside its parent `.section`. Sections start expanded.
    function initCollapsibleSections() {
        const titles = document.querySelectorAll('.section > .section-title');
        titles.forEach(function (title) {
            const section = title.parentElement;
            if (!section) return;

            // Add a caret indicator; keep the existing header text intact.
            const caret = document.createElement('span');
            caret.className = 'section-caret';
            caret.setAttribute('aria-hidden', 'true');
            caret.textContent = '▾';
            title.insertBefore(caret, title.firstChild);

            title.classList.add('is-collapsible');
            title.setAttribute('role', 'button');
            title.setAttribute('tabindex', '0');
            title.setAttribute('aria-expanded', 'true');

            const toggle = function () {
                const collapsed = section.classList.toggle('section-collapsed');
                title.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
                caret.textContent = collapsed ? '▸' : '▾';
            };

            title.addEventListener('click', toggle);
            title.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggle();
                }
            });
        });
    }

    // ------- Competency modal ------------------------------------------
    // Reads the JSON blob embedded on the page and wires up the
    // "View Competencies" buttons to open a modal with current + next level.
    function initCompetencyModal() {
        const dataEl = document.getElementById('competency-data');
        const modal = document.getElementById('competency-modal');
        if (!dataEl || !modal) return;

        let data;
        try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

        const titleEl = document.getElementById('competency-modal-title');
        const subtitleEl = document.getElementById('competency-modal-subtitle');
        const bodyEl = document.getElementById('competency-modal-body');

        function renderColumn(level) {
            if (!level) return '';
            const label = data.levelLabels[level] || ('Level ' + level);
            const comp = data.competencies[level];
            if (!comp) {
                return '<div class="competency-col"><h3>' + label + '</h3>' +
                       '<p class="competency-empty">No competency definition for this level.</p></div>';
            }
            const resultsHtml = (comp.results || []).map(function (r) {
                return '<li><strong>' + r.label + ':</strong> ' + r.text + '</li>';
            }).join('');
            const behaviorsHtml = (comp.behaviors || []).map(function (r) {
                return '<li><strong>' + r.label + ':</strong> ' + r.text + '</li>';
            }).join('');
            return '' +
                '<div class="competency-col">' +
                    '<h3>' + label + '</h3>' +
                    '<div class="competency-who">' + (comp.who_you_are || '') + '</div>' +
                    '<div class="competency-subhead">Results (What You Do)</div>' +
                    '<ul class="competency-list">' + resultsHtml + '</ul>' +
                    '<div class="competency-subhead">Behaviors (How You Do It)</div>' +
                    '<ul class="competency-list">' + behaviorsHtml + '</ul>' +
                    '<div class="competency-subhead">Leadership</div>' +
                    '<p class="competency-leadership">' + (comp.leadership || '') + '</p>' +
                '</div>';
        }

        // Equalize the i-th child's height in both .competency-col elements so
        // that "Leadership" on the right lines up with "Leadership" on the left,
        // etc. Skipped on narrow viewports (single-column stack).
        function alignCompetencyRows() {
            if (window.innerWidth < 760) return;
            const cols = bodyEl.querySelectorAll('.competency-col');
            if (cols.length !== 2) return;
            const left = cols[0].children;
            const right = cols[1].children;
            const n = Math.min(left.length, right.length);
            for (let i = 0; i < n; i++) {
                left[i].style.minHeight = '';
                right[i].style.minHeight = '';
            }
            for (let i = 0; i < n; i++) {
                const h = Math.max(left[i].offsetHeight, right[i].offsetHeight);
                left[i].style.minHeight = h + 'px';
                right[i].style.minHeight = h + 'px';
            }
        }

        function onResize() { if (!modal.hidden) alignCompetencyRows(); }

        function open(btn) {
            const title = btn.getAttribute('data-level-title');
            const devName = btn.getAttribute('data-dev-name') || '';
            const currentLevel = data.titleToLevel[title];
            if (!currentLevel) return;
            const nextLevel = data.competencies[currentLevel + 1] ? currentLevel + 1 : null;

            titleEl.textContent = devName || title;
            subtitleEl.textContent = 'Current: ' + (data.levelLabels[currentLevel] || title) +
                (nextLevel ? ' · Next up: ' + data.levelLabels[nextLevel] : ' · Top of ladder');

            bodyEl.innerHTML =
                '<div class="competency-grid ' + (nextLevel ? 'two-col' : 'one-col') + '">' +
                    '<div class="competency-col-wrap current">' +
                        '<div class="competency-col-badge">Current level</div>' +
                        renderColumn(currentLevel) +
                    '</div>' +
                    (nextLevel ?
                        '<div class="competency-col-wrap next">' +
                            '<div class="competency-col-badge next">Path to next level</div>' +
                            renderColumn(nextLevel) +
                        '</div>'
                    : '') +
                '</div>';

            modal.hidden = false;
            // focus the close button for a11y
            const close = modal.querySelector('.competency-close');
            if (close) close.focus();
            document.body.style.overflow = 'hidden';
            // Wait one frame so the browser has laid out the new content.
            requestAnimationFrame(alignCompetencyRows);
            window.addEventListener('resize', onResize);
        }

        function close() {
            modal.hidden = true;
            document.body.style.overflow = '';
            window.removeEventListener('resize', onResize);
        }

        document.querySelectorAll('.competency-btn').forEach(function (btn) {
            btn.addEventListener('click', function () { open(btn); });
        });

        modal.querySelectorAll('[data-close-competency-modal]').forEach(function (el) {
            el.addEventListener('click', close);
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !modal.hidden) close();
        });
    }

    // ------- Member edit modal ------------------------------------------
    // Opens the edit form pre-filled from the clicked card's data-* attrs,
    // POSTs the form to /api/member, then reloads so the regenerated HTML
    // replaces the page. Uses delegated click handling so it still works
    // regardless of DOM order between the button and the modal markup.
    function initMemberEditModal() {
        const modal = document.getElementById('member-edit-modal');
        if (!modal) return;

        const subtitle = document.getElementById('member-edit-modal-subtitle');
        const originalName = document.getElementById('member-edit-original-name');
        const githubInput = document.getElementById('member-edit-github');
        const jiraInput = document.getElementById('member-edit-jira');
        const levelSelect = document.getElementById('member-edit-level');
        const saveBtn = document.getElementById('member-edit-save');
        const errorEl = document.getElementById('member-edit-error');
        const form = document.getElementById('member-edit-form');

        function showError(msg) {
            if (errorEl) { errorEl.textContent = msg; errorEl.hidden = false; }
        }
        function clearError() {
            if (errorEl) { errorEl.textContent = ''; errorEl.hidden = true; }
        }

        function openModal(btn) {
            const devName = btn.getAttribute('data-dev-name') || '';
            if (originalName) originalName.value = devName;
            if (githubInput) githubInput.value = btn.getAttribute('data-github-username') || '';
            if (jiraInput) jiraInput.value = btn.getAttribute('data-jira-account-id') || '';
            if (levelSelect) levelSelect.value = btn.getAttribute('data-level') || '';
            if (subtitle) subtitle.textContent = devName;
            clearError();
            modal.hidden = false;
            document.body.style.overflow = 'hidden';
            if (githubInput) githubInput.focus();
        }

        function closeModal() {
            modal.hidden = true;
            document.body.style.overflow = '';
        }

        async function save() {
            if (!saveBtn) return;
            clearError();
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving…';
            try {
                const payload = {
                    name: originalName ? originalName.value : '',
                    github_username: githubInput ? githubInput.value.trim() : '',
                    jira_account_id: jiraInput ? jiraInput.value.trim() : '',
                    level: levelSelect ? levelSelect.value : '',
                };
                const resp = await fetch('/api/member', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                let data = {};
                try { data = await resp.json(); } catch (_) { /* ignore */ }
                if (!resp.ok) {
                    showError(data.error || ('Server error: ' + resp.status));
                    return;
                }
                window.location.reload();
            } catch (err) {
                showError('Network error: ' + err.message);
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save';
            }
        }

        // Single delegated listener on document — robust against rerenders.
        document.addEventListener('click', function (e) {
            const editBtn = e.target.closest && e.target.closest('.member-edit-btn');
            if (editBtn) {
                e.preventDefault();
                openModal(editBtn);
                return;
            }
            const closeEl = e.target.closest && e.target.closest('[data-close-member-edit-modal]');
            if (closeEl) {
                e.preventDefault();
                closeModal();
                return;
            }
            if (saveBtn && e.target === saveBtn) {
                e.preventDefault();
                save();
            }
        });

        if (form) form.addEventListener('submit', function (e) { e.preventDefault(); save(); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !modal.hidden) closeModal();
        });
    }

    // ------- Fantasy Ops chat widget -----------------------------------
    // Injects a floating "Ask" button + slide-out panel on every page.
    // Conversation history is kept in sessionStorage so it survives tab
    // navigation but clears when you close the tab. Cmd/Ctrl+/ toggles it.
    function initFantasyOpsWidget() {
        if (document.getElementById('fops-widget')) return;

        const root = document.createElement('div');
        root.id = 'fops-widget';
        root.innerHTML = `
            <button type="button" id="fops-toggle" class="fops-toggle"
                    aria-label="Open Fantasy Ops (Cmd+/)">
                <span class="fops-toggle-icon">💬</span>
                <span class="fops-toggle-label">Ask</span>
            </button>
            <aside id="fops-panel" class="fops-panel" hidden aria-hidden="true">
                <div class="fops-header">
                    <div>
                        <div class="fops-title">Fantasy Ops</div>
                        <div class="fops-sub">Ask about the team, sprint, PRs, or agents.</div>
                    </div>
                    <button type="button" class="fops-close" aria-label="Close">×</button>
                </div>
                <div class="fops-transcript" id="fops-transcript"></div>
                <form id="fops-form" class="fops-form" autocomplete="off">
                    <textarea id="fops-input" rows="2"
                              placeholder="e.g. who's behind on sprint?"
                              aria-label="Ask Fantasy Ops"></textarea>
                    <div class="fops-form-row">
                        <button type="button" class="flat-btn" id="fops-clear">Clear</button>
                        <button type="submit" class="flat-btn success" id="fops-send">Send</button>
                    </div>
                </form>
            </aside>
        `;
        document.body.appendChild(root);

        const toggle = document.getElementById('fops-toggle');
        const panel = document.getElementById('fops-panel');
        const closeBtn = root.querySelector('.fops-close');
        const form = document.getElementById('fops-form');
        const input = document.getElementById('fops-input');
        const transcript = document.getElementById('fops-transcript');
        const clearBtn = document.getElementById('fops-clear');
        const sendBtn = document.getElementById('fops-send');

        const STORAGE_KEY = 'fops.history.v1';
        function loadHistory() {
            try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]'); }
            catch (e) { return []; }
        }
        function saveHistory(h) {
            try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(h)); } catch (e) {}
        }

        function escapeHTML(s) {
            return (s || '').replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
        }

        function addBubble(role, text, opts) {
            const row = document.createElement('div');
            row.className = 'fops-row fops-row-' + role;
            const bubble = document.createElement('div');
            bubble.className = 'fops-bubble';
            // Light markdown-ish: preserve line breaks + code spans.
            const safe = escapeHTML(text).replace(/\n/g, '<br>')
                .replace(/`([^`]+)`/g, '<code>$1</code>');
            bubble.innerHTML = safe;
            row.appendChild(bubble);
            if (opts && opts.tools && opts.tools.length) {
                const foot = document.createElement('div');
                foot.className = 'fops-tools';
                foot.textContent = 'Called: ' + opts.tools.map(t => t.name).join(', ');
                row.appendChild(foot);
            }
            transcript.appendChild(row);
            transcript.scrollTop = transcript.scrollHeight;
            return row;
        }

        function renderHistory(h) {
            transcript.innerHTML = '';
            (h || []).forEach(function (turn) {
                // Our stored transcript tracks just the user/assistant text
                // pairs (not the raw Anthropic message blocks).
                if (turn.display_role === 'user') {
                    addBubble('user', turn.text);
                } else if (turn.display_role === 'assistant') {
                    addBubble('assistant', turn.text, { tools: turn.tool_calls });
                }
            });
            if (!transcript.childElementCount) {
                addBubble('assistant',
                    "Hi. I can answer questions about the team, current sprint, PRs, hygiene issues, and recent agent runs. Try `who's behind on sprint?` or `what did the hygiene agent find last run?`");
            }
        }

        let apiHistory = []; // raw history passed to /api/ask each turn
        let displayHistory = []; // what we render to the user

        function resetFromStorage() {
            const stored = loadHistory();
            apiHistory = stored.api || [];
            displayHistory = stored.display || [];
            renderHistory(displayHistory);
        }

        function persist() {
            saveHistory({ api: apiHistory, display: displayHistory });
        }

        function openPanel() {
            panel.hidden = false;
            panel.setAttribute('aria-hidden', 'false');
            document.body.classList.add('fops-open');
            setTimeout(function () { input.focus(); }, 60);
        }
        function closePanel() {
            panel.hidden = true;
            panel.setAttribute('aria-hidden', 'true');
            document.body.classList.remove('fops-open');
        }
        function togglePanel() { panel.hidden ? openPanel() : closePanel(); }

        async function send() {
            const question = input.value.trim();
            if (!question) return;
            addBubble('user', question);
            displayHistory.push({ display_role: 'user', text: question });
            input.value = '';
            input.style.height = 'auto';
            sendBtn.disabled = true;
            sendBtn.textContent = 'Thinking…';
            const pending = addBubble('assistant', '…');

            try {
                const resp = await fetch('/api/ask', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: question, history: apiHistory }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    pending.querySelector('.fops-bubble').textContent =
                        '⚠ Request failed: ' + (data.error || resp.status);
                    return;
                }
                // Replace the placeholder bubble with the real reply
                const bubble = pending.querySelector('.fops-bubble');
                const safe = escapeHTML(data.reply || '').replace(/\n/g, '<br>')
                    .replace(/`([^`]+)`/g, '<code>$1</code>');
                bubble.innerHTML = safe || '(no response)';
                if (data.tool_calls && data.tool_calls.length) {
                    const foot = document.createElement('div');
                    foot.className = 'fops-tools';
                    foot.textContent = 'Called: ' + data.tool_calls.map(t => t.name).join(', ');
                    pending.appendChild(foot);
                }
                // Update histories
                apiHistory = data.history_after || apiHistory;
                displayHistory.push({
                    display_role: 'assistant',
                    text: data.reply || '',
                    tool_calls: data.tool_calls || [],
                });
                persist();
                if (data.missing_api_key) {
                    bubble.classList.add('fops-bubble-warn');
                }
            } catch (err) {
                pending.querySelector('.fops-bubble').textContent = '⚠ Network error: ' + err.message;
            } finally {
                sendBtn.disabled = false;
                sendBtn.textContent = 'Send';
                input.focus();
            }
        }

        function clearConversation() {
            apiHistory = [];
            displayHistory = [];
            persist();
            renderHistory(displayHistory);
        }

        toggle.addEventListener('click', togglePanel);
        closeBtn.addEventListener('click', closePanel);
        form.addEventListener('submit', function (e) { e.preventDefault(); send(); });
        clearBtn.addEventListener('click', clearConversation);
        input.addEventListener('keydown', function (e) {
            // Enter sends; Shift+Enter inserts a newline.
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send();
            }
        });
        input.addEventListener('input', function () {
            input.style.height = 'auto';
            input.style.height = Math.min(160, input.scrollHeight) + 'px';
        });

        document.addEventListener('keydown', function (e) {
            const mod = e.metaKey || e.ctrlKey;
            if (mod && e.key === '/') { e.preventDefault(); togglePanel(); }
            if (e.key === 'Escape' && !panel.hidden) closePanel();
        });

        resetFromStorage();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            initCollapsibleSections();
            initCompetencyModal();
            initMemberEditModal();
            initFantasyOpsWidget();
        });
    } else {
        initCollapsibleSections();
        initCompetencyModal();
        initMemberEditModal();
        initFantasyOpsWidget();
    }
})();


// ---------------------------------------------------------------------------
// Inline-handler shims, exposed on window for legacy onclick="..." attributes.
// Previously these were defined inline in every generated page (3 generators,
// ~150 lines duplicated); now they live here so a fix is one edit, not three.
// ---------------------------------------------------------------------------

window.toggleAccordion = function (id) {
    const panel = document.getElementById(id);
    if (!panel) return;
    let nowOpen = false;

    if (panel.tagName === 'TR') {
        const isVisible = panel.style.display !== 'none';
        document.querySelectorAll('tr.accordion-panel').forEach(p => { p.style.display = 'none'; });
        if (!isVisible) { panel.style.display = 'table-row'; nowOpen = true; }
    } else {
        const isActive = panel.classList.contains('active');
        document.querySelectorAll('div.accordion-panel').forEach(p => p.classList.remove('active'));
        if (!isActive) { panel.classList.add('active'); nowOpen = true; }
    }

    document.querySelectorAll('[aria-controls][aria-expanded]').forEach(btn => {
        if (btn.getAttribute('aria-controls') === id) {
            btn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
        }
    });
};

window.toggleInsights = function (id, btn) {
    const panel = document.getElementById(id);
    if (!panel) return;
    const isOpen = panel.classList.toggle('open');
    const caret = btn.querySelector('.toggle-caret');
    if (caret) caret.textContent = isOpen ? '▾' : '▸';
    const label = btn.querySelector('.toggle-label');
    if (label) label.textContent = isOpen ? 'Hide' : 'Show';
};

window.sortTable = function (table, columnIndex, dataType) {
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr')).filter(row => !row.classList.contains('accordion-panel'));
    const header = table.querySelectorAll('th')[columnIndex];
    const isAsc = header.classList.contains('asc');
    table.querySelectorAll('th').forEach(th => th.classList.remove('asc', 'desc'));
    header.classList.add(isAsc ? 'desc' : 'asc');

    // Cells can opt in to a sort-only value via `data-sort`. Useful when the
    // displayed text doesn't sort cleanly (e.g. "5d ago" should sort numeric).
    function cellValue(row, idx) {
        const cell = row.children[idx];
        return cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent.trim();
    }

    rows.sort((a, b) => {
        let av, bv;
        const rawA = cellValue(a, columnIndex);
        const rawB = cellValue(b, columnIndex);
        if (dataType === 'number') {
            av = parseFloat(rawA);
            bv = parseFloat(rawB);
            if (Number.isNaN(av)) av = 0;
            if (Number.isNaN(bv)) bv = 0;
        } else {
            av = rawA.toLowerCase();
            bv = rawB.toLowerCase();
        }
        if (av < bv) return isAsc ? 1 : -1;
        if (av > bv) return isAsc ? -1 : 1;
        return 0;
    });

    const allRows = Array.from(tbody.querySelectorAll('tr'));
    tbody.innerHTML = '';
    rows.forEach(row => {
        const rowIndex = allRows.indexOf(row);
        tbody.appendChild(row);
        for (let i = rowIndex + 1; i < allRows.length; i++) {
            if (allRows[i].classList.contains('accordion-panel') &&
                allRows[i].id.startsWith('dev-' + row.dataset.devIndex)) {
                tbody.appendChild(allRows[i]);
            } else if (!allRows[i].classList.contains('accordion-panel')) {
                break;
            }
        }
    });
};

// Sort the Epic Timeline (Gantt) rows. The rows are <div>s, not <tr>s, so
// we can't reuse window.sortTable; this walks the rows container and
// re-appends sorted children. Each row carries data-sort-key/status/
// summary/assignee on its first cell (set by the generator).
window.sortGanttRows = function (headerEl, field) {
    const wrapper = headerEl.closest('.gantt-wrapper');
    if (!wrapper) return;
    const headers = wrapper.querySelectorAll('.gantt-col-header');
    const rowsContainer = wrapper.querySelector('.gantt-rows');
    if (!rowsContainer) return;
    const rows = Array.from(rowsContainer.querySelectorAll('.gantt-row'));

    const isAsc = headerEl.classList.contains('asc');
    headers.forEach(h => h.classList.remove('asc', 'desc'));
    headerEl.classList.add(isAsc ? 'desc' : 'asc');
    const dir = isAsc ? -1 : 1;

    function ticketKeyValue(s) {
        // Sort FNTSY-25 before FNTSY-100 by stripping prefix and using the
        // numeric tail when present.
        const m = (s || '').match(/-(\d+)\s*$/);
        return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
    }

    rows.sort((a, b) => {
        const av = (a.dataset['sort' + field.charAt(0).toUpperCase() + field.slice(1)] || '').toLowerCase();
        const bv = (b.dataset['sort' + field.charAt(0).toUpperCase() + field.slice(1)] || '').toLowerCase();
        if (field === 'key') {
            const an = ticketKeyValue(av);
            const bn = ticketKeyValue(bv);
            if (an !== bn) return (an - bn) * dir;
            return av.localeCompare(bv) * dir;
        }
        return av.localeCompare(bv) * dir;
    });
    rows.forEach(row => rowsContainer.appendChild(row));
};

// Hygiene-page section switcher.
window.showSection = function (sectionId) {
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    const target = document.getElementById(sectionId);
    if (target) target.classList.add('active');
    document.querySelectorAll('[aria-controls][aria-expanded]').forEach(btn => {
        btn.setAttribute('aria-expanded',
            btn.getAttribute('aria-controls') === sectionId ? 'true' : 'false');
    });
};

// Logs-page agent-log panels.
window.toggleAgentLogs = function (agentId) {
    const logsDiv = document.getElementById(agentId + '-logs');
    if (!logsDiv) return;
    const btn = (typeof event !== 'undefined' ? event.target : null);
    if (logsDiv.classList.contains('visible')) {
        logsDiv.classList.remove('visible');
        if (btn) btn.textContent = 'Show Logs';
    } else {
        logsDiv.classList.add('visible');
        if (btn) btn.textContent = 'Hide Logs';
    }
};

window.toggleLog = function (id) {
    const container = document.getElementById(id);
    if (!container) return;
    container.classList.toggle('collapsed');
    const btn = (typeof event !== 'undefined' ? event.target : null);
    if (btn) btn.textContent = container.classList.contains('collapsed') ? 'Expand' : 'Collapse';
};

// Detect if we're running locally or on GitHub Pages
function isLocalEnvironment() {
    const hostname = window.location.hostname;
    // Return false (not local) if on GitHub Pages or other hosting
    if (hostname.includes('github.io') ||
        hostname.includes('githubusercontent.com') ||
        hostname.includes('netlify.app') ||
        hostname.includes('vercel.app')) {
        return false;
    }
    // Return true for local development environments
    return hostname === 'localhost' ||
           hostname === '127.0.0.1' ||
           hostname === '' || // file:// protocol
           hostname.startsWith('192.168.') ||
           hostname.startsWith('10.') ||
           hostname.endsWith('.local');
}

// Initialize dependencies page based on environment
function initDependenciesPage() {
    const isLocal = isLocalEnvironment();

    // If on GitHub Pages, make read-only
    if (!isLocal) {
        document.querySelectorAll('.dep-notes').forEach(ta => {
            ta.readOnly = true;
            ta.style.backgroundColor = 'var(--bg-page, #0f0f0f)';
            ta.style.color = 'var(--text-secondary, #999)';
            ta.style.border = '1px solid var(--border, #333)';
            ta.style.cursor = 'default';
            ta.style.opacity = '0.8';
            ta.title = 'Read-only: Dashboard is hosted on GitHub Pages';
        });

        document.querySelectorAll('.dep-save-btn').forEach(btn => {
            btn.disabled = true;
            btn.style.display = 'none';
        });

        // Add read-only notice
        const introBanner = document.querySelector('.intro-banner');
        if (introBanner && window.location.pathname.includes('dependencies')) {
            const notice = document.createElement('p');
            notice.style.cssText = 'margin-top: 10px; padding: 8px 12px; background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; font-size: 13px; color: #000;';
            notice.innerHTML = '<strong>📌 Read-only mode:</strong> This dashboard is hosted on GitHub Pages. To edit dependency notes, run the dashboard locally.';
            introBanner.appendChild(notice);
        }
    }
}

// Dependencies page — save the textarea contents back to dependencies.yaml
// via the /api/dependency-notes endpoint. Optimistic UI: button disables
// while in-flight, the small message line confirms or shows the error.
// Save dependency notes. Callable two ways:
//   - From the Save button (passes the button as `btn` for label feedback).
//   - From the textarea blur handler (no button — shows feedback only in
//     the .dep-save-msg span). The button stays as a manual force-save.
window.saveDependencyNotes = async function (key, btn) {
    // Prevent saves on GitHub Pages
    if (!isLocalEnvironment()) {
        const card = btn ? btn.closest('.dep-card') : document.querySelector(`.dep-card[data-key="${CSS.escape(key)}"]`);
        const msg = card && card.querySelector('.dep-save-msg');
        if (msg) {
            msg.textContent = 'Read-only mode';
            msg.className = 'dep-save-msg error';
        }
        return;
    }

    const card = btn ? btn.closest('.dep-card') : document.querySelector(`.dep-card[data-key="${CSS.escape(key)}"]`);
    if (!card) return;
    const textarea = card.querySelector('.dep-notes');
    const msg = card.querySelector('.dep-save-msg');
    if (!textarea) return;
    const original = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving…';
    }
    if (msg) { msg.textContent = btn ? '' : 'Saving…'; msg.className = 'dep-save-msg'; }
    try {
        const resp = await fetch('/api/dependency-notes', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({key: key, notes: textarea.value}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || ('HTTP ' + resp.status));
        }
        textarea.dataset.baseline = textarea.value;
        if (msg) { msg.textContent = 'Saved.'; msg.className = 'dep-save-msg ok'; }
    } catch (e) {
        if (msg) { msg.textContent = 'Save failed: ' + e.message; msg.className = 'dep-save-msg error'; }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = original;
        }
    }
};

// Auto-save on blur for dependency note textareas. Only fires when the
// value actually changed since the last load/save, so clicking through
// cards without typing doesn't spam the API.
document.addEventListener('DOMContentLoaded', () => {
    // Initialize dependencies page (read-only mode on GitHub Pages)
    initDependenciesPage();

    // Only enable auto-save if running locally
    if (isLocalEnvironment()) {
        document.querySelectorAll('.dep-notes').forEach(ta => {
            ta.dataset.baseline = ta.value;
            ta.addEventListener('blur', () => {
                if (ta.value === ta.dataset.baseline) return;
                const card = ta.closest('.dep-card');
                const key = card && card.dataset.key;
                if (!key) return;
                window.saveDependencyNotes(key);
            });
        });
    }
});

// "Run Now" button — single-user LAN dashboard, so we just print instructions.
window.triggerAgent = function (agentType) {
    const agentNames = {
        'jira-collector': 'Jira Collector',
        'qa': 'QA Agent',
        'team-member': 'Team Member Agent',
    };
    alert('To run the ' + (agentNames[agentType] || agentType) + ':\n\n' +
          'Open Terminal and run:\n' +
          'cd /Users/davidbaxter/sync/claude/em_dashboard\n' +
          'bash scripts/trigger_agent.sh ' + agentType + '\n\n' +
          'The dashboard will refresh automatically when complete.');
};
