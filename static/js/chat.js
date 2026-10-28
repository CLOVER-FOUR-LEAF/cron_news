(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const messagesEl = $('chatMessages');
    if (!messagesEl) return;  // 当前页面没有嵌入对话框

    // 首页嵌入模式：没有会话列表、状态栏与标题，相关逻辑需跳过
    const embedded = !$('convList');
    const convListEl = $('convList');
    const statusEl = $('chatStatus');
    const inputEl = $('chatInput');
    const sendBtn = $('sendBtn');
    const stopBtn = $('stopBtn');
    const formEl = $('chatForm');
    const titleEl = $('chatTitle');
    const sideEl = $('chatSide');

    if (embedded) document.body.classList.add('chat-embedded');

    const emptyHTML = $('chatEmpty') ? $('chatEmpty').outerHTML : '';
    const state = {
        conversationId: null,
        streaming: false,
        controller: null,
        bubble: null,
        citations: [],
        trace: [],
        answer: ''
    };

    function formatTime(iso) {
        if (!iso) return '';
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return '';
        const now = new Date();
        const sameDay = date.toDateString() === now.toDateString();
        const pad = (n) => String(n).padStart(2, '0');
        return sameDay
            ? `${pad(date.getHours())}:${pad(date.getMinutes())}`
            : `${date.getMonth() + 1}-${pad(date.getDate())}`;
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function bindSuggestions() {
        messagesEl.querySelectorAll('[data-q]').forEach((button) => {
            button.addEventListener('click', () => {
                inputEl.value = button.dataset.q || '';
                autoGrow();
                formEl.requestSubmit();
            });
        });
    }

    function showEmpty() {
        if (!emptyHTML) return;
        messagesEl.innerHTML = emptyHTML;
        bindSuggestions();
    }

    function removeEmpty() {
        const empty = $('chatEmpty');
        if (empty) empty.remove();
    }

    function autoGrow() {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + 'px';
    }

    function userBubble(text) {
        const row = document.createElement('div');
        row.className = 'chat-row chat-row-user';
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble chat-bubble-user';
        bubble.textContent = text;
        row.appendChild(bubble);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    function assistantBubble() {
        const row = document.createElement('div');
        row.className = 'chat-row chat-row-assistant';

        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble chat-bubble-assistant';

        const status = document.createElement('div');
        status.className = 'chat-bubble-status';
        status.textContent = '正在检索你的新闻库…';

        const content = document.createElement('div');
        content.className = 'markdown-body chat-markdown';

        const trace = document.createElement('details');
        trace.className = 'chat-trace';
        trace.hidden = true;
        const traceSummary = document.createElement('summary');
        trace.appendChild(traceSummary);
        const traceBody = document.createElement('div');
        traceBody.className = 'chat-trace-body';
        trace.appendChild(traceBody);

        const sources = document.createElement('div');
        sources.className = 'chat-sources';

        const meta = document.createElement('div');
        meta.className = 'chat-bubble-meta';

        bubble.append(status, content, trace, sources, meta);
        row.appendChild(bubble);
        messagesEl.appendChild(row);
        scrollToBottom();

        return { row, bubble, status, content, trace, traceSummary, traceBody, sources, meta };
    }

    function renderMarkdown(text, citations) {
        let markdown = text || '';
        if (citations && citations.length) {
            const map = new Map(citations.map((c) => [String(c.n), c]));
            markdown = markdown.replace(/\[(\d+)\](?!\()/g, (match, n) => {
                const citation = map.get(n);
                if (!citation) return match;
                const url = citation.kind === 'news'
                    ? `/news/${citation.news_id}`
                    : (citation.source_url || '#');
                return `[${n}](${url})`;
            });
        }
        const html = window.marked
            ? window.marked.parse(markdown, { breaks: true, gfm: true })
            : markdown.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\n/g, '<br>');
        return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
    }

    function setAssistantContent(bubble, text, citations) {
        bubble.content.innerHTML = renderMarkdown(text, citations);
        bubble.content.querySelectorAll('a[href^="http"]').forEach((a) => {
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
        });
        scrollToBottom();
    }

    function renderSources(bubble, citations) {
        bubble.sources.innerHTML = '';
        if (!citations || !citations.length) return;
        const label = document.createElement('span');
        label.className = 'chat-sources-label';
        label.textContent = '来源';
        bubble.sources.appendChild(label);
        citations.forEach((citation) => {
            const link = document.createElement('a');
            link.className = 'chat-source';
            link.href = citation.kind === 'news'
                ? `/news/${citation.news_id}`
                : (citation.source_url || '#');
            if (citation.kind !== 'news') {
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
            }
            link.textContent = `[${citation.n}] ${citation.title || citation.source_url || '来源'}`;
            bubble.sources.appendChild(link);
        });
    }

    function renderTrace(bubble, trace) {
        if (!trace || !trace.length) {
            bubble.trace.hidden = true;
            return;
        }
        bubble.trace.hidden = false;
        bubble.traceSummary.textContent = `工具调用 ${trace.length} 次 · 查看过程`;
        bubble.traceBody.innerHTML = '';
        trace.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'chat-trace-row';

            const name = document.createElement('span');
            name.className = 'chat-trace-name';
            name.textContent = item.name || 'tool';

            const summary = document.createElement('span');
            summary.className = 'chat-trace-meta';
            summary.textContent = `${item.ok ? '✓' : '✗'} ${item.summary || ''}`;

            row.append(name, summary);
            bubble.traceBody.appendChild(row);
        });
    }

    function setStatusText(bubble, text) {
        if (!bubble) return;
        bubble.status.textContent = text || '';
        bubble.status.style.display = text ? '' : 'none';
    }

    function resetStreamingUI() {
        state.streaming = false;
        state.controller = null;
        sendBtn.disabled = false;
        stopBtn.hidden = true;
        inputEl.disabled = false;
    }

    function applyEvent(bubble, event, data) {
        switch (event) {
            case 'meta':
                state.conversationId = data.conversation_id;
                break;
            case 'status': {
                const labels = {
                    thinking: '正在理解问题…',
                    tool: '正在检索新闻库…',
                    answering: '正在整理回答…'
                };
                setStatusText(bubble, labels[data.stage] || '处理中…');
                break;
            }
            case 'tool_call':
                state.trace.push({
                    id: data.id,
                    name: data.name,
                    arguments: data.arguments,
                    ok: true,
                    summary: '执行中…'
                });
                renderTrace(bubble, state.trace);
                break;
            case 'tool_result': {
                for (let i = state.trace.length - 1; i >= 0; i -= 1) {
                    if (state.trace[i].id === data.id) {
                        state.trace[i].ok = data.ok;
                        state.trace[i].summary = data.summary || '';
                        break;
                    }
                }
                renderTrace(bubble, state.trace);
                break;
            }
            case 'token':
                state.answer = (state.answer || '') + (data.text || '');
                setAssistantContent(bubble, state.answer, state.citations);
                break;
            case 'citation':
                state.citations.push(data);
                renderSources(bubble, state.citations);
                break;
            case 'usage':
                if (data.total_tokens) {
                    bubble.meta.textContent = `本轮 ${data.total_tokens} tokens`;
                }
                break;
            case 'error':
                setStatusText(bubble, data.message || '出错了');
                bubble.bubble.classList.add('chat-bubble-error');
                break;
            case 'done':
                setStatusText(bubble, '');
                if (data.content) {
                    state.answer = data.content;
                    setAssistantContent(bubble, state.answer, data.citations || state.citations);
                }
                if (data.citations) {
                    state.citations = data.citations;
                    renderSources(bubble, state.citations);
                }
                if (data.tool_trace) {
                    state.trace = data.tool_trace;
                    renderTrace(bubble, state.trace);
                }
                if (data.conversation_id) state.conversationId = data.conversation_id;
                break;
            default:
                break;
        }
    }

    function parseBlocks(buffer, onEvent) {
        let rest = buffer;
        let index;
        while ((index = rest.indexOf('\n\n')) >= 0) {
            const block = rest.slice(0, index);
            rest = rest.slice(index + 2);
            if (block.startsWith(':')) continue;
            let event = 'message';
            let payload = '';
            block.split('\n').forEach((line) => {
                if (line.startsWith('event:')) event = line.slice(6).trim();
                else if (line.startsWith('data:')) payload += line.slice(5).trim();
            });
            if (!payload) continue;
            try {
                onEvent(event, JSON.parse(payload));
            } catch (err) {
                // 忽略无法解析的心跳/片段
            }
        }
        return rest;
    }

    async function sendMessage(text) {
        if (state.streaming) return;
        removeEmpty();
        userBubble(text);
        inputEl.value = '';
        autoGrow();

        if (embedded) {
            // 首页嵌入模式：发起提问即切到对话全屏，避免用户错过回答
            const layout = document.getElementById('homeLayout');
            if (layout && layout.dataset.mode === 'split') {
                layout.dataset.mode = 'agent';
                const feed = document.getElementById('homeFeed');
                const toggle = document.getElementById('homeFeedToggle');
                const expand = document.getElementById('homeAgentExpand');
                if (feed) feed.style.display = 'none';
                if (toggle) toggle.style.display = '';
                if (expand) expand.title = '恢复分栏';
                try { localStorage.setItem('cronnews.homeLayout', 'agent'); } catch (e) { /* ignore */ }
            }
        }

        const bubble = assistantBubble();
        state.bubble = bubble;
        state.citations = [];
        state.trace = [];
        state.answer = '';
        state.streaming = true;
        sendBtn.disabled = true;
        stopBtn.hidden = false;
        inputEl.disabled = true;

        const controller = new AbortController();
        state.controller = controller;

        try {
            const response = await fetch('/api/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ conversation_id: state.conversationId, message: text }),
                signal: controller.signal
            });
            if (!response.ok || !response.body) {
                throw new Error(`HTTP ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                buffer = parseBlocks(buffer, (event, data) => applyEvent(bubble, event, data));
            }
        } catch (error) {
            if (error.name === 'AbortError') {
                setStatusText(bubble, '已停止');
            } else {
                setStatusText(bubble, `出错了：${error.message}`);
                bubble.bubble.classList.add('chat-bubble-error');
            }
        } finally {
            resetStreamingUI();
            if (!embedded) loadConversations();
            inputEl.focus();
        }
    }

    function messageFromData(message) {
        if (message.role === 'user') {
            userBubble(message.content || '');
            return;
        }
        const bubble = assistantBubble();
        setStatusText(bubble, '');
        if (message.content) setAssistantContent(bubble, message.content, message.citations || []);
        renderSources(bubble, message.citations || []);
        renderTrace(bubble, message.tool_trace || []);
        if (message.status === 'error') bubble.bubble.classList.add('chat-bubble-error');
        if (message.usage && message.usage.total_tokens) {
            bubble.meta.textContent = `本轮 ${message.usage.total_tokens} tokens`;
        }
    }

    async function selectConversation(id) {
        try {
            const response = await fetch(`/api/chat/conversations/${id}/messages?limit=100`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            state.conversationId = id;
            state.answer = '';
            state.citations = [];
            state.trace = [];
            messagesEl.innerHTML = '';
            if (!data.items.length) {
                showEmpty();
            } else {
                data.items.forEach(messageFromData);
            }
            const current = document.querySelector(`.chat-conv-item[data-id="${id}"]`);
            if (titleEl) {
                titleEl.textContent = current ? current.querySelector('.chat-conv-title').textContent : 'AI 资讯助手';
            }
            document.querySelectorAll('.chat-conv-item').forEach((item) => {
                item.classList.toggle('active', Number(item.dataset.id) === Number(id));
            });
            if (sideEl) sideEl.classList.remove('open');
            scrollToBottom();
        } catch (error) {
            if (window.showToast) window.showToast('加载会话失败', 'error');
        }
    }

    async function loadConversations() {
        if (!convListEl) return;
        try {
            const response = await fetch('/api/chat/conversations?limit=50');
            const data = await response.json();
            convListEl.innerHTML = '';
            (data.items || []).forEach((conversation) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'chat-conv-item';
                button.dataset.id = conversation.id;
                if (Number(conversation.id) === Number(state.conversationId)) {
                    button.classList.add('active');
                }

                const title = document.createElement('div');
                title.className = 'chat-conv-title';
                title.textContent = conversation.title || '新对话';

                const time = document.createElement('div');
                time.className = 'chat-conv-time';
                time.textContent = formatTime(conversation.updated_at);

                button.append(title, time);
                button.addEventListener('click', () => selectConversation(conversation.id));
                convListEl.appendChild(button);
            });
            if (!(data.items || []).length) {
                const hint = document.createElement('div');
                hint.className = 'chat-conv-empty';
                hint.textContent = '还没有对话，问一个试试。';
                convListEl.appendChild(hint);
            }
        } catch (error) {
            convListEl.innerHTML = '<div class="chat-conv-empty">会话列表加载失败</div>';
        }
    }

    async function loadStatus() {
        if (!statusEl) return;
        try {
            const response = await fetch('/api/retrieval/status');
            const data = await response.json();
            const parts = [
                data.fts_available ? '全文检索 ✓' : '全文检索降级',
                `共 ${data.news_total || 0} 条新闻`
            ];
            statusEl.textContent = parts.join(' · ');
        } catch (error) {
            statusEl.textContent = '服务状态未知';
        }
    }

    function newChat() {
        state.conversationId = null;
        state.answer = '';
        state.citations = [];
        state.trace = [];
        state.bubble = null;
        messagesEl.innerHTML = '';
        showEmpty();
        if (titleEl) titleEl.textContent = 'AI 资讯助手';
        document.querySelectorAll('.chat-conv-item').forEach((item) => item.classList.remove('active'));
        if (sideEl) sideEl.classList.remove('open');
        inputEl.focus();
    }

    formEl.addEventListener('submit', (event) => {
        event.preventDefault();
        const text = inputEl.value.trim();
        if (!text || state.streaming) return;
        sendMessage(text);
    });

    inputEl.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            formEl.requestSubmit();
        }
    });
    inputEl.addEventListener('input', autoGrow);

    stopBtn.addEventListener('click', () => {
        if (state.controller) state.controller.abort();
    });

    if ($('newChatBtn')) $('newChatBtn').addEventListener('click', newChat);
    const homeNewBtn = $('homeNewChat');
    if (homeNewBtn) homeNewBtn.addEventListener('click', newChat);
    if ($('sideToggle') && sideEl) {
        $('sideToggle').addEventListener('click', () => sideEl.classList.toggle('open'));
    }

    bindSuggestions();
    loadConversations();
    loadStatus();
    autoGrow();
    inputEl.focus();
})();
