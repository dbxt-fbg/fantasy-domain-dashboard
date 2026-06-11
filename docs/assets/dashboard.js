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

// Features page — toggle BE/FE work-complete checkboxes. Posts the changed
// flag to /api/feature-work-status; the server flips it in
// config/feature_work_status.yaml and re-renders features.html. Optimistic
// UI: the checkbox shows the new state immediately and reverts on failure.
window.saveFeatureWorkStatus = async function (cb) {
    if (!cb) return;
    const key = cb.dataset.key;
    const kind = cb.dataset.kind;
    if (!key || !(kind === 'be' || kind === 'fe')) return;

    // GitHub Pages has no API — revert and tell the user.
    if (!isLocalEnvironment()) {
        cb.checked = !cb.checked;
        cb.disabled = true;
        cb.title = 'Read-only on GitHub Pages — run the dashboard locally to edit.';
        return;
    }

    const previous = !cb.checked; // optimistic-revert target
    const field = kind === 'be' ? 'be_done' : 'fe_done';
    const cell = cb.closest('td');
    const row = cb.closest('tr.feature-row');
    cb.disabled = true;
    try {
        const resp = await fetch('/api/feature-work-status', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({key: key, [field]: cb.checked}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || ('HTTP ' + resp.status));
        }
        if (cell) cell.dataset.sort = cb.checked ? '1' : '0';
        // Sync the row-level data attr the filter bar reads, so a freshly
        // checked BE Done row hides immediately if "Hide BE Done" is on.
        if (row) {
            row.dataset[kind === 'be' ? 'beDone' : 'feDone'] = cb.checked ? '1' : '0';
            applyFeatureFilters();
        }
    } catch (e) {
        cb.checked = previous;
        if (cell) cell.dataset.sort = previous ? '1' : '0';
        cb.title = 'Save failed: ' + e.message;
    } finally {
        cb.disabled = false;
    }
};

// Features page — top-of-page filter bar (Hide Completed / BE Done / FE Done).
// Reads each .feature-row's data-bucket / data-be-done / data-fe-done and
// toggles a `hidden-by-filter` class. Also rewrites the per-launch summary
// count chip so the user sees how many rows are currently visible.
function applyFeatureFilters() {
    const bar = document.getElementById('feature-filter-bar');
    if (!bar) return;
    const hideCompleted = bar.querySelector('input[data-filter="completed"]')?.checked;
    const hideBe = bar.querySelector('input[data-filter="be"]')?.checked;
    const hideFe = bar.querySelector('input[data-filter="fe"]')?.checked;

    document.querySelectorAll('.launch-group').forEach(group => {
        const rows = group.querySelectorAll('tr.feature-row');
        let visible = 0;
        rows.forEach(row => {
            const isCompleted = row.dataset.completed === '1';
            const isBeDone = row.dataset.beDone === '1';
            const isFeDone = row.dataset.feDone === '1';
            const hide = (hideCompleted && isCompleted)
                || (hideBe && isBeDone)
                || (hideFe && isFeDone);
            row.classList.toggle('hidden-by-filter', hide);
            if (!hide) visible += 1;
        });
        const counts = group.querySelector('.launch-group-counts');
        if (counts && !counts.dataset.totalLabel) {
            // Snapshot the original count chip on first run so we can swap
            // back to it when no filters are active.
            counts.dataset.totalLabel = counts.innerHTML;
        }
        if (counts) {
            const anyFilter = hideCompleted || hideBe || hideFe;
            if (anyFilter) {
                const total = rows.length;
                counts.innerHTML = `<strong>${visible}</strong> of ${total} feature${total === 1 ? '' : 's'} shown`;
            } else {
                counts.innerHTML = counts.dataset.totalLabel;
            }
        }
    });
}

// Auto-save on blur for dependency note textareas. Only fires when the
// value actually changed since the last load/save, so clicking through
// cards without typing doesn't spam the API.
document.addEventListener('DOMContentLoaded', () => {
    // Initialize dependencies page (read-only mode on GitHub Pages)
    initDependenciesPage();

    // GitHub Pages: feature-work-status checkboxes are read-only.
    if (!isLocalEnvironment()) {
        document.querySelectorAll('.feature-toggle').forEach(cb => {
            cb.disabled = true;
            cb.title = 'Read-only on GitHub Pages — run the dashboard locally to edit.';
        });
    }

    // Wire up the Features-page filter bar. Independent of GH Pages: filters
    // only hide rendered rows, no API call.
    const filterBar = document.getElementById('feature-filter-bar');
    if (filterBar) {
        filterBar.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', applyFeatureFilters);
        });
        applyFeatureFilters();
    }

    // Epics page — "Hide Completed Epics" toolbar above the Gantt chart.
    // Persists the user's choice in localStorage so a refresh keeps the
    // filter on. Pure DOM toggle: no server round-trip.
    //
    // The chart above the Gantt is server-rendered SVG, so we can't filter
    // it live in JS. Instead the generator emits two variants — a default
    // and an "exclude completed" one — and the toggle flips display on
    // both at once.
    const ganttHideCompleted = document.getElementById('gantt-hide-completed');
    if (ganttHideCompleted) {
        const STORAGE_KEY = 'gantt.hideCompletedEpics';
        const apply = (hide) => {
            document.querySelectorAll('.gantt-row').forEach(row => {
                const isCompleted = row.dataset.completed === '1';
                row.classList.toggle('hidden-by-filter', hide && isCompleted);
            });
            const fullChart = document.querySelector('.epics-chart-full');
            const filteredChart = document.querySelector('.epics-chart-filtered');
            if (fullChart) fullChart.style.display = hide ? 'none' : '';
            if (filteredChart) filteredChart.style.display = hide ? '' : 'none';
        };
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (saved === '1') ganttHideCompleted.checked = true;
        } catch (_) { /* localStorage may be blocked — start unchecked */ }
        apply(ganttHideCompleted.checked);
        ganttHideCompleted.addEventListener('change', () => {
            apply(ganttHideCompleted.checked);
            try {
                localStorage.setItem(STORAGE_KEY, ganttHideCompleted.checked ? '1' : '0');
            } catch (_) { /* ignore quota / privacy-mode errors */ }
        });
    }

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

// ----- Header action buttons (Refresh Data + Publish to GitHub) ---------
// On local runs (serve_dashboard.py), inject a pair of buttons into every
// page header. Each kicks off its own background job on the server and
// opens a modal that polls progress to completion. The same JS bundle
// ships to GitHub Pages, where these are hidden because the API endpoints
// don't exist there.

(function () {
    // Generic factory: builds a modal+poller bound to a single job kind so
    // Refresh and Publish each get their own DOM, IDs, and polling timer.
    function makeJobUI(opts) {
        const cfg = {
            kind: opts.kind,                  // 'refresh' | 'publish'
            startUrl: opts.startUrl,          // POST endpoint
            statusUrl: opts.statusUrl,        // GET endpoint (takes ?id=)
            runningTitle: opts.runningTitle,
            failedTitle: opts.failedTitle,
            doneLabel: opts.doneLabel,        // text shown at 100%
            onDone: opts.onDone,              // (data) => void after success
        };
        const ids = {
            modal:    cfg.kind + '-modal',
            title:    cfg.kind + '-modal-title',
            status:   cfg.kind + '-modal-status',
            fill:     cfg.kind + '-progress-fill',
            percent:  cfg.kind + '-modal-percent',
            error:    cfg.kind + '-modal-error',
            actions:  cfg.kind + '-modal-actions',
            close:    cfg.kind + '-modal-close',
            retry:    cfg.kind + '-modal-retry',
        };
        let pollTimer = null;

        function buildModal() {
            const modal = document.createElement('div');
            modal.className = 'refresh-modal';
            modal.id = ids.modal;
            modal.hidden = true;
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-modal', 'true');
            modal.setAttribute('aria-labelledby', ids.title);
            modal.innerHTML = `
                <div class="refresh-modal-backdrop"></div>
                <div class="refresh-modal-dialog">
                    <div class="refresh-modal-title" id="${ids.title}">${cfg.runningTitle}</div>
                    <div class="refresh-modal-status" id="${ids.status}">Starting…</div>
                    <div class="refresh-progress"><div class="refresh-progress-fill" id="${ids.fill}"></div></div>
                    <div class="refresh-modal-percent" id="${ids.percent}">0%</div>
                    <div class="refresh-modal-error" id="${ids.error}" hidden></div>
                    <div class="refresh-modal-actions" id="${ids.actions}" hidden>
                        <button type="button" class="refresh-modal-btn" id="${ids.close}">Close</button>
                        <button type="button" class="refresh-modal-btn primary" id="${ids.retry}">Retry</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
            modal.querySelector('#' + ids.close).addEventListener('click', () => { modal.hidden = true; });
            modal.querySelector('#' + ids.retry).addEventListener('click', start);
            return modal;
        }

        function setProgress(percent, label, errOpts) {
            errOpts = errOpts || {};
            const fill = document.getElementById(ids.fill);
            const pct = document.getElementById(ids.percent);
            const status = document.getElementById(ids.status);
            const errBox = document.getElementById(ids.error);
            const actions = document.getElementById(ids.actions);
            const title = document.getElementById(ids.title);
            if (fill) fill.style.width = Math.max(0, Math.min(100, percent)) + '%';
            if (pct) pct.textContent = Math.round(percent) + '%';
            if (status && label) status.textContent = label;
            if (errOpts.error) {
                if (errBox) {
                    errBox.hidden = false;
                    errBox.textContent = errOpts.error + (errOpts.tail ? '\n\n' + errOpts.tail : '');
                }
                if (actions) actions.hidden = false;
                if (title) title.textContent = cfg.failedTitle;
            } else {
                if (errBox) errBox.hidden = true;
                if (actions) actions.hidden = true;
                if (title) title.textContent = cfg.runningTitle;
            }
        }

        function poll(jobId) {
            clearTimeout(pollTimer);
            fetch(cfg.statusUrl + '?id=' + encodeURIComponent(jobId))
                .then(r => r.json().then(d => ({ok: r.ok, data: d})))
                .then(({ok, data}) => {
                    if (!ok) {
                        setProgress(0, '', {error: data.error || 'Status request failed.'});
                        return;
                    }
                    const label = data.step_label || 'Working…';
                    setProgress(data.percent || 0, label);
                    if (data.status === 'done') {
                        setProgress(100, cfg.doneLabel || 'Done.');
                        if (typeof cfg.onDone === 'function') cfg.onDone(data);
                        return;
                    }
                    if (data.status === 'failed') {
                        setProgress(data.percent || 0, label, {
                            error: data.error || (cfg.failedTitle + '.'),
                            tail: data.log_tail || '',
                        });
                        return;
                    }
                    pollTimer = setTimeout(() => poll(jobId), 800);
                })
                .catch(err => {
                    setProgress(0, '', {error: 'Network error: ' + err.message});
                });
        }

        function start() {
            const modal = document.getElementById(ids.modal) || buildModal();
            modal.hidden = false;
            setProgress(0, 'Starting…');
            fetch(cfg.startUrl, {method: 'POST'})
                .then(r => r.json().then(d => ({ok: r.ok, data: d})))
                .then(({ok, data}) => {
                    if (!ok || !data.job_id) {
                        setProgress(0, '', {error: data.error || 'Could not start.'});
                        return;
                    }
                    poll(data.job_id);
                })
                .catch(err => {
                    setProgress(0, '', {error: 'Network error: ' + err.message});
                });
        }

        return {start};
    }

    const refreshUI = makeJobUI({
        kind: 'refresh',
        startUrl: '/api/refresh',
        statusUrl: '/api/refresh/status',
        runningTitle: 'Refreshing dashboard data…',
        failedTitle: 'Refresh failed',
        doneLabel: 'Done — reloading page…',
        onDone: () => setTimeout(() => window.location.reload(), 600),
    });

    const publishUI = makeJobUI({
        kind: 'publish',
        startUrl: '/api/publish',
        statusUrl: '/api/publish/status',
        runningTitle: 'Publishing to GitHub Pages…',
        failedTitle: 'Publish failed',
        doneLabel: 'Published — GitHub Pages will rebuild shortly.',
    });

    function injectButtons() {
        const header = document.querySelector('body > .container > header')
            || document.querySelector('header');
        if (!header) return;
        if (header.querySelector('.header-actions')) return;

        const wrap = document.createElement('div');
        wrap.className = 'header-actions';

        const refreshBtn = document.createElement('button');
        refreshBtn.type = 'button';
        refreshBtn.className = 'header-action-btn refresh-data-btn is-visible';
        refreshBtn.textContent = '↻ Refresh Data';
        refreshBtn.title = 'Pull the latest Jira data and regenerate the dashboards';
        refreshBtn.addEventListener('click', refreshUI.start);
        wrap.appendChild(refreshBtn);

        const publishBtn = document.createElement('button');
        publishBtn.type = 'button';
        publishBtn.className = 'header-action-btn publish-data-btn is-visible';
        publishBtn.textContent = '⤴ Publish to GitHub';
        publishBtn.title = 'Sync the current dashboards to docs/ and push to GitHub Pages';
        publishBtn.addEventListener('click', publishUI.start);
        wrap.appendChild(publishBtn);

        header.appendChild(wrap);
    }

    document.addEventListener('DOMContentLoaded', () => {
        // Buttons only make sense on the local dashboard server — GitHub
        // Pages copies don't have the /api endpoints behind them.
        if (typeof isLocalEnvironment === 'function' && !isLocalEnvironment()) return;
        injectButtons();
    });
})();

// ----- Per-section "Export to PDF" --------------------------------------
// The Jira link is just an <a target="_blank"> — the browser handles it.
// For PDF, we let the user use their browser's native "Save as PDF" via
// window.print(), but we temporarily isolate the chosen <details> so the
// printed page only contains that section (rest of the dashboard is
// hidden via the @media print rules in dashboard.css).
//
// Listener uses CAPTURE phase: the wrapping <span class="feature-export-actions">
// has an inline `stopPropagation` to keep summary clicks from toggling the
// accordion, which would otherwise swallow these clicks in the bubble phase.
document.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-export-pdf]');
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    const groupId = btn.getAttribute('data-export-pdf');
    const target = document.getElementById(groupId);
    if (!target) {
        console.warn('PDF export: no element with id', groupId);
        return;
    }

    // Force the section open (and remember prior state) so the printed
    // version contains the table even if the user collapsed the accordion.
    const wasOpen = target.open;
    if ('open' in target) target.open = true;

    // Also expand any nested <details> so collapsed sub-blocks (e.g.
    // engineers inside a sprint) appear in the printed PDF. Track state
    // so we can restore on afterprint.
    const nestedDetails = Array.from(target.querySelectorAll('details'));
    const nestedPrior = nestedDetails.map(d => d.open);
    nestedDetails.forEach(d => { d.open = true; });

    target.classList.add('feature-print-target');
    document.body.classList.add('feature-print-active');

    // Update the print title temporarily so the browser's PDF filename
    // prompt is helpful instead of the page title.
    const originalTitle = document.title;
    const exportLabel = btn.getAttribute('data-export-label') || 'Features';
    document.title = exportLabel + ' — ' + originalTitle;

    const restore = () => {
        document.body.classList.remove('feature-print-active');
        target.classList.remove('feature-print-target');
        document.title = originalTitle;
        nestedDetails.forEach((d, i) => { d.open = nestedPrior[i]; });
        if (!wasOpen && 'open' in target) target.open = false;
        window.removeEventListener('afterprint', restore);
    };
    window.addEventListener('afterprint', restore);

    // Use rAF to let the layout settle (open the details, apply class)
    // before invoking the print dialog.
    requestAnimationFrame(() => requestAnimationFrame(() => {
        try {
            window.print();
        } finally {
            // Some browsers (Safari) don't fire afterprint reliably —
            // schedule a backup restore on the next tick.
            setTimeout(() => {
                if (document.body.classList.contains('feature-print-active')) {
                    restore();
                }
            }, 1500);
        }
    }));
}, true);

// ----- Per-section "Export to Sheets" -----------------------------------
// Google Sheets has no public URL prefill API, so the canonical workflow
// is: copy table as TSV to the clipboard, open a blank sheet, user pastes.
// We open sheets.new in a new tab and show a small toast confirming the
// clipboard copy. If clipboard access fails (Safari without user gesture,
// HTTP origins, etc.) we fall back to a modal with the TSV pre-selected
// so the user can ⌘C manually.

function sectionToTsv(section) {
    // Prefer a hidden <table class="export-table"> if the generator emitted
    // one — that's how non-tabular sections (sprint reports, etc.) ship
    // structured TSV data. Otherwise fall back to the first visible table.
    // Important: scope to direct-table-not-inside-nested-details so a sprint
    // block's TSV doesn't include all its engineers' tables too.
    const table = scopedTable(section, 'table.export-table')
        || scopedTable(section, 'table');
    if (!table) return '';
    const lines = [];
    // Headers come from <thead><tr><th>. Strip surrounding whitespace and
    // any sort-arrow text injected by the sortable headers.
    const headerCells = table.querySelectorAll('thead tr th');
    if (headerCells.length) {
        lines.push(Array.from(headerCells).map(th => cellText(th)).join('\t'));
    }
    // Body rows. Skip rows that only contain a colspan placeholder.
    const bodyRows = table.querySelectorAll('tbody tr');
    bodyRows.forEach(tr => {
        const cells = tr.querySelectorAll('td');
        if (!cells.length) return;
        lines.push(Array.from(cells).map(td => cellText(td)).join('\t'));
    });
    return lines.join('\n');
}

// Find the first matching descendant that isn't inside a nested <details>
// of `section`. Prevents a parent's TSV from absorbing every child block's
// table when sections are nested (e.g. sprint → engineers).
function scopedTable(section, selector) {
    const candidates = section.querySelectorAll(selector);
    for (const el of candidates) {
        let parent = el.parentElement;
        let inside = false;
        while (parent && parent !== section) {
            if (parent.tagName === 'DETAILS' && parent !== section) {
                inside = true;
                break;
            }
            parent = parent.parentElement;
        }
        if (!inside) return el;
    }
    return null;
}

function cellText(cell) {
    // Use textContent rather than innerHTML so HTML entities don't bleed
    // into Sheets. Collapse runs of whitespace into single spaces and
    // replace embedded tabs/newlines (which would break TSV) with spaces.
    const txt = (cell.textContent || '').replace(/\s+/g, ' ').trim();
    return txt.replace(/[\t\n\r]/g, ' ');
}

function showTsvFallback(tsv, label) {
    let modal = document.getElementById('sheets-fallback-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'sheets-fallback-modal';
        modal.className = 'refresh-modal';
        modal.innerHTML = `
            <div class="refresh-modal-backdrop"></div>
            <div class="refresh-modal-dialog" style="width: min(720px, 92vw);">
                <div class="refresh-modal-title">Copy to Google Sheets</div>
                <div class="refresh-modal-status" id="sheets-fallback-status">
                    Couldn't copy automatically. Select the text below, copy
                    (⌘C / Ctrl+C), then click "Open Google Sheets" and paste.
                </div>
                <textarea id="sheets-fallback-textarea"
                          style="width:100%; height:240px; font-family:ui-monospace,monospace; font-size:12px; background:var(--bg-surface-2); color:var(--text-primary); border:1px solid var(--border); border-radius:var(--radius-md); padding:8px; resize:vertical;"
                          spellcheck="false"></textarea>
                <div class="refresh-modal-actions" style="display:flex;">
                    <button type="button" class="refresh-modal-btn" id="sheets-fallback-close">Close</button>
                    <button type="button" class="refresh-modal-btn" id="sheets-fallback-copy">Copy</button>
                    <a class="refresh-modal-btn primary" href="https://sheets.new" target="_blank" rel="noopener" id="sheets-fallback-open">Open Google Sheets</a>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.querySelector('#sheets-fallback-close').addEventListener('click', () => { modal.hidden = true; });
        modal.querySelector('.refresh-modal-backdrop').addEventListener('click', () => { modal.hidden = true; });
        modal.querySelector('#sheets-fallback-copy').addEventListener('click', () => {
            const ta = modal.querySelector('#sheets-fallback-textarea');
            ta.focus();
            ta.select();
            try {
                document.execCommand('copy');
                modal.querySelector('#sheets-fallback-status').textContent = 'Copied. Click "Open Google Sheets" and paste.';
            } catch (e) {
                modal.querySelector('#sheets-fallback-status').textContent = 'Copy failed — select the text manually and press ⌘C.';
            }
        });
    }
    const ta = modal.querySelector('#sheets-fallback-textarea');
    ta.value = tsv;
    modal.querySelector('#sheets-fallback-status').textContent =
        'Couldn’t copy automatically. Select the text below, copy (⌘C / Ctrl+C), then click "Open Google Sheets" and paste.';
    modal.hidden = false;
    // Auto-select so ⌘A isn't needed.
    setTimeout(() => { ta.focus(); ta.select(); }, 0);
}

function showSheetsToast(message) {
    let toast = document.getElementById('sheets-export-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'sheets-export-toast';
        toast.className = 'sheets-export-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('is-visible');
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => {
        toast.classList.remove('is-visible');
    }, 3500);
}

document.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-export-sheets]');
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    const groupId = btn.getAttribute('data-export-sheets');
    const label = btn.getAttribute('data-export-label') || 'Features';
    const section = document.getElementById(groupId);
    if (!section) return;

    // Open the <details> so sectionToTsv sees the table even if collapsed.
    const wasOpen = section.open;
    if ('open' in section) section.open = true;

    const tsv = sectionToTsv(section);
    if ('open' in section && !wasOpen) section.open = false;

    if (!tsv) {
        showSheetsToast('Nothing to export.');
        return;
    }

    // Open Google Sheets first (must be in the user-gesture stack to avoid
    // popup blockers). Then write to the clipboard.
    const newSheet = window.open('https://sheets.new', '_blank', 'noopener');
    const writePromise = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(tsv)
        : Promise.reject(new Error('clipboard unavailable'));

    writePromise
        .then(() => {
            if (newSheet) {
                showSheetsToast('Copied. Paste (⌘V) into the new Google Sheet.');
            } else {
                // Popup blocker swallowed the new tab — show fallback so
                // the user has a way to get the data out.
                showTsvFallback(tsv, label);
            }
        })
        .catch(() => {
            // Clipboard write failed (Safari without HTTPS, missing user
            // gesture, etc.) — show the fallback modal regardless of
            // whether the tab opened, since clipboard is empty.
            showTsvFallback(tsv, label);
        });
}, true);  // capture-phase: see PDF handler comment above
