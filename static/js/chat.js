(function () {
    'use strict';

    const STORAGE_KEY = 'cronnews.chat.conversation';
    const controllers = [];
    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

    const FEEDBACK_REASONS = [
        { value: 'irrelevant', label: '不相关' },
        { value: 'shallow', label: '内容太浅' },
        { value: 'duplicate', label: '重复' },
        { value: 'outdated', label: '过时' },
        { value: 'inaccurate', label: '信息有误' },
        { value: 'source', label: '来源不可靠' },
        { value: 'too_long', label: '太啰嗦' }
    ];

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function escapeAttr(text) {
        return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

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

    function renderMarkdown(text, citations) {
        let markdown = text || '';
        if (citations && citations.length) {
            const map = new Map(citations.map((citation) => [String(citation.n), citation]));
            markdown = markdown.replace(/\[(\d+)\](?!\()/g, (match, n) => {
                const citation = map.get(n);
                if (!citation) return match;
                const url = citation.kind === 'news'
                    ? `/news/${citation.news_id}`
                    : (citation.source_url || '#');
                return `[\\[${n}\\]](${url})`;
            });
        }
        const html = window.marked
            ? window.marked.parse(markdown, { breaks: true, gfm: true })
            : escapeHtml(markdown).replace(/\n/g, '<br>');
        return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
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
                // 忽略心跳或无法解析的片段
            }
        }
        return rest;
    }

    function readStoredConversation() {
        try {
            return localStorage.getItem(STORAGE_KEY) || '';
        } catch (e) {
            return '';
        }
    }

    function storeConversation(id) {
        try {
            if (id) localStorage.setItem(STORAGE_KEY, String(id));
            else localStorage.removeItem(STORAGE_KEY);
        } catch (e) { /* ignore */ }
    }

    class ChatController {
        constructor(root) {
            this.root = root;
            this.variant = root.dataset.chatVariant || 'page';
            this.messagesEl = $('[data-chat-messages]', root);
            if (!this.messagesEl) return;

            this.emptyTemplate = $('template[data-chat-empty]', root);
            this.formEl = $('[data-chat-form]', root);
            this.inputEl = $('[data-chat-input]', root);
            this.sendBtn = $('[data-chat-send]', root);
            this.stopBtn = $('[data-chat-stop]', root);
            this.titleEl = this.variant === 'page' ? $('[data-chat-title]', root) : null;
            this.convListEl = this.variant === 'page' ? $('[data-chat-conv-list]') : null;
            this.statusEl = this.variant === 'page' ? $('[data-chat-status]') : null;
            this.memoryPanel = this.variant === 'page' ? $('[data-memory-panel]') : null;
            this.state = {
                conversationId: null,
                streaming: false,
                controller: null,
                bubble: null,
                citations: [],
                trace: [],
                reasoning: '',
                answer: '',
                messageId: null
            };
            this.restored = false;
            this.memoryPromise = null;
            this.pendingPrefill = '';
            this.bind();
            this.showEmpty();
            if (this.variant === 'page') {
                this.restore();
                this.loadConversations();
                this.loadStatus();
                this.wireMemoryPanel();
                this.loadMemoryHint();
            }
        }

        bind() {
            this.formEl.addEventListener('submit', (event) => {
                event.preventDefault();
                const text = this.inputEl.value.trim();
                if (!text || this.state.streaming) return;
                this.sendMessage(text);
            });

            this.inputEl.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                    event.preventDefault();
                    this.formEl.requestSubmit();
                }
            });
            this.inputEl.addEventListener('input', () => this.autoGrow());

            if (this.stopBtn) {
                this.stopBtn.addEventListener('click', () => {
                    if (this.state.controller) this.state.controller.abort();
                });
            }

            this.root.addEventListener('click', (event) => {
                const quick = event.target.closest('[data-q]');
                if (quick && this.root.contains(quick) && !quick.disabled) {
                    this.quickSend(quick.dataset.q || '');
                    return;
                }
                const action = event.target.closest('[data-chat-quick-text]');
                if (action && this.root.contains(action)) {
                    this.sendMessage(action.dataset.chatQuickText || '');
                }
            });
        }

        quickSend(text) {
            if (!text) return;
            if (this.state.streaming) return;
            this.inputEl.value = text;
            this.autoGrow();
            this.formEl.requestSubmit();
        }

        showEmpty() {
            this.messagesEl.innerHTML = '';
            if (this.emptyTemplate) {
                this.messagesEl.appendChild(this.emptyTemplate.content.cloneNode(true));
            }
        }

        removeEmpty() {
            const empty = $('.chat-empty', this.messagesEl);
            if (empty) empty.remove();
        }

        autoGrow() {
            if (!this.inputEl) return;
            this.inputEl.style.height = 'auto';
            this.inputEl.style.height = Math.min(this.inputEl.scrollHeight, 180) + 'px';
        }

        scrollToBottom() {
            this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        }

        userBubble(text) {
            const row = document.createElement('div');
            row.className = 'chat-row chat-row-user';
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble chat-bubble-user';
            bubble.textContent = text;
            row.appendChild(bubble);
            this.messagesEl.appendChild(row);
            this.scrollToBottom();
        }

        assistantBubble() {
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
            const traceBody = document.createElement('div');
            traceBody.className = 'chat-trace-body';
            const reasoningEl = document.createElement('div');
            reasoningEl.className = 'chat-reasoning';
            reasoningEl.hidden = true;
            const rowsEl = document.createElement('div');
            rowsEl.className = 'chat-trace-rows';
            traceBody.append(reasoningEl, rowsEl);
            trace.append(traceSummary, traceBody);

            const sources = document.createElement('div');
            sources.className = 'chat-sources';

            const meta = document.createElement('div');
            meta.className = 'chat-bubble-meta';

            const feedback = document.createElement('div');
            feedback.className = 'chat-feedback-slot';

            bubble.append(status, trace, content, sources, meta, feedback);
            row.appendChild(bubble);
            this.messagesEl.appendChild(row);
            this.scrollToBottom();

            return {
                row, bubble, status, content, trace, traceSummary, traceBody,
                reasoningEl, rowsEl, sources, meta, feedback
            };
        }

        setAssistantContent(bubble, text, citations) {
            bubble.content.innerHTML = renderMarkdown(text, citations);
            bubble.content.querySelectorAll('a[href^="http"]').forEach((link) => {
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
            });
            this.scrollToBottom();
        }

        renderSources(bubble, citations) {
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

        renderTrace(bubble, trace) {
            const hasReasoning = !bubble.reasoningEl.hidden;
            if ((!trace || !trace.length) && !hasReasoning) {
                bubble.trace.hidden = true;
                return;
            }
            bubble.trace.hidden = false;
            bubble.traceSummary.textContent = trace && trace.length
                ? `思考与工具过程 · ${trace.length} 次`
                : '思考过程';
            bubble.rowsEl.innerHTML = '';
            (trace || []).forEach((item) => {
                const row = document.createElement('div');
                row.className = 'chat-trace-row';

                const name = document.createElement('span');
                name.className = 'chat-trace-name';
                name.textContent = item.name || 'tool';

                const summary = document.createElement('span');
                summary.className = 'chat-trace-meta';
                summary.textContent = `${item.ok ? '✓' : '✗'} ${item.summary || ''}`;

                row.append(name, summary);
                bubble.rowsEl.appendChild(row);
            });
        }

        appendReasoning(bubble, text) {
            if (!text) return;
            this.state.reasoning = (this.state.reasoning || '') + text;
            bubble.reasoningEl.hidden = false;
            bubble.reasoningEl.textContent = this.state.reasoning;
            bubble.trace.hidden = false;
            bubble.trace.open = true;
            if (!bubble.rowsEl.children.length) {
                bubble.traceSummary.textContent = '思考过程';
            }
            if (bubble.reasoningEl.textContent.length > 4000) {
                bubble.reasoningEl.textContent = '…' + bubble.reasoningEl.textContent.slice(-4000);
            }
            this.scrollToBottom();
        }

        setStatusText(bubble, text) {
            if (!bubble) return;
            bubble.status.textContent = text || '';
            bubble.status.style.display = text ? '' : 'none';
        }

        resetStreamingUI() {
            this.state.streaming = false;
            this.state.controller = null;
            if (this.sendBtn) this.sendBtn.disabled = false;
            if (this.stopBtn) this.stopBtn.hidden = true;
            if (this.inputEl) this.inputEl.disabled = false;
        }

        applyEvent(bubble, event, data) {
            switch (event) {
                case 'meta':
                    this.state.conversationId = data.conversation_id;
                    storeConversation(data.conversation_id);
                    this.updateUrl();
                    break;
                case 'status': {
                    const labels = {
                        thinking: '正在理解问题…',
                        tool: '正在检索新闻库…',
                        answering: '正在整理回答…'
                    };
                    this.setStatusText(bubble, data.text || labels[data.stage] || '处理中…');
                    break;
                }
                case 'tool_call':
                    this.state.trace.push({
                        id: data.id,
                        name: data.name,
                        arguments: data.arguments,
                        ok: true,
                        summary: '执行中…'
                    });
                    bubble.trace.open = true;
                    this.renderTrace(bubble, this.state.trace);
                    break;
                case 'tool_result':
                    for (let i = this.state.trace.length - 1; i >= 0; i -= 1) {
                        if (this.state.trace[i].id === data.id) {
                            this.state.trace[i].ok = data.ok;
                            this.state.trace[i].summary = data.summary || '';
                            break;
                        }
                    }
                    this.renderTrace(bubble, this.state.trace);
                    break;
                case 'reasoning':
                    this.appendReasoning(bubble, data.text || '');
                    break;
                case 'reset':
                    this.state.answer = '';
                    bubble.content.innerHTML = '';
                    break;
                case 'token':
                    this.state.answer = (this.state.answer || '') + (data.text || '');
                    this.setAssistantContent(bubble, this.state.answer, this.state.citations);
                    break;
                case 'citation':
                    this.state.citations.push(data);
                    this.renderSources(bubble, this.state.citations);
                    break;
                case 'usage':
                    if (data.total_tokens) {
                        bubble.meta.textContent = `本轮 ${data.total_tokens} tokens`;
                    }
                    break;
                case 'error':
                    this.setStatusText(bubble, data.message || '出错了');
                    bubble.bubble.classList.add('chat-bubble-error');
                    break;
                case 'done':
                    this.setStatusText(bubble, '');
                    if (data.content) {
                        this.state.answer = data.content;
                        this.setAssistantContent(bubble, this.state.answer, data.citations || this.state.citations);
                    }
                    if (data.citations) {
                        this.state.citations = data.citations;
                        this.renderSources(bubble, this.state.citations);
                    }
                    if (data.tool_trace) {
                        this.state.trace = data.tool_trace;
                        this.renderTrace(bubble, this.state.trace);
                    }
                    if (bubble.trace) {
                        bubble.trace.open = this.state.trace.length > 0;
                    }
                    if (data.conversation_id) {
                        this.state.conversationId = data.conversation_id;
                        storeConversation(data.conversation_id);
                        this.updateUrl();
                    }
                    this.state.messageId = data.assistant_message_id || null;
                    this.attachFeedback(bubble, this.state.messageId);
                    break;
                default:
                    break;
            }
        }

        async sendMessage(text) {
            if (this.state.streaming || !text) return;
            this.removeEmpty();
            this.userBubble(text);
            this.inputEl.value = '';
            this.autoGrow();

            const bubble = this.assistantBubble();
            this.state.bubble = bubble;
            this.state.citations = [];
            this.state.trace = [];
            this.state.reasoning = '';
            this.state.answer = '';
            this.state.messageId = null;
            this.state.streaming = true;
            this.sendBtn.disabled = true;
            this.stopBtn.hidden = false;
            this.inputEl.disabled = true;

            const controller = new AbortController();
            this.state.controller = controller;

            try {
                const response = await fetch('/api/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ conversation_id: this.state.conversationId, message: text }),
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
                    buffer = parseBlocks(buffer, (event, data) => this.applyEvent(bubble, event, data));
                }
            } catch (error) {
                if (error.name === 'AbortError') {
                    this.setStatusText(bubble, '已停止');
                } else {
                    this.setStatusText(bubble, `出错了：${error.message}`);
                    bubble.bubble.classList.add('chat-bubble-error');
                }
            } finally {
                this.resetStreamingUI();
                if (this.variant === 'page') this.loadConversations();
                this.syncExpandLink();
                this.loadMemoryHint(true);
                this.inputEl.focus();
            }
        }

        attachFeedback(bubble, messageId) {
            if (!bubble || !bubble.feedback || !messageId) return;
            const slot = bubble.feedback;
            slot.innerHTML = '';

            const post = async (payload) => {
                try {
                    const response = await fetch('/api/feedback', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            target_type: 'message',
                            target_id: messageId,
                            conversation_id: this.state.conversationId,
                            ...payload
                        })
                    });
                    if (!response.ok) {
                        if (response.status === 404) throw new Error('反馈接口未加载，请重启服务后重试');
                        throw new Error(`HTTP ${response.status}`);
                    }
                    slot.innerHTML = '<span class="chat-feedback-done">已记录，会据此调整后续回答</span>';
                    if (window.showToast) window.showToast('感谢反馈，助手会记住', 'success');
                    this.loadMemoryHint(true);
                } catch (error) {
                    slot.innerHTML = `<span class="chat-feedback-error">${escapeHtml(error.message || '反馈失败，请稍后再试')}</span>`;
                }
            };

            const bar = document.createElement('div');
            bar.className = 'chat-feedback';
            const label = document.createElement('span');
            label.className = 'chat-feedback-label';
            label.textContent = '这条回答有帮助吗？';
            const up = document.createElement('button');
            up.type = 'button';
            up.className = 'chat-feedback-btn up';
            up.textContent = '有帮助';
            up.addEventListener('click', () => post({ rating: 1, reason: 'helpful' }));
            const down = document.createElement('button');
            down.type = 'button';
            down.className = 'chat-feedback-btn down';
            down.textContent = '不够好';
            down.addEventListener('click', () => showReasons());
            bar.append(label, up, down);

            const showReasons = () => {
                slot.innerHTML = '';
                const panel = document.createElement('div');
                panel.className = 'chat-feedback-panel';
                const reasons = document.createElement('div');
                reasons.className = 'chat-feedback-reasons';
                let selected = 'irrelevant';
                FEEDBACK_REASONS.forEach((reason) => {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = 'chat-feedback-reason' + (reason.value === selected ? ' active' : '');
                    button.textContent = reason.label;
                    button.addEventListener('click', () => {
                        selected = reason.value;
                        Array.from(reasons.children).forEach((item) => {
                            item.classList.toggle('active', item === button);
                        });
                    });
                    reasons.appendChild(button);
                });
                const row = document.createElement('div');
                row.className = 'chat-feedback-row';
                const input = document.createElement('input');
                input.type = 'text';
                input.maxLength = 300;
                input.placeholder = '补充说明（可选）';
                const submit = document.createElement('button');
                submit.type = 'button';
                submit.className = 'chat-feedback-submit';
                submit.textContent = '提交';
                submit.addEventListener('click', () => post({ rating: -1, reason: selected, comment: input.value.trim() }));
                const cancel = document.createElement('button');
                cancel.type = 'button';
                cancel.className = 'chat-feedback-cancel';
                cancel.textContent = '取消';
                cancel.addEventListener('click', () => slot.innerHTML = '');
                row.append(input, submit, cancel);
                panel.append(reasons, row);
                slot.appendChild(panel);
                input.focus();
            };

            slot.appendChild(bar);
        }

        messageFromData(message) {
            if (message.role === 'user') {
                this.userBubble(message.content || '');
                return;
            }
            const bubble = this.assistantBubble();
            this.setStatusText(bubble, '');
            if (message.content) this.setAssistantContent(bubble, message.content, message.citations || []);
            this.renderSources(bubble, message.citations || []);
            this.renderTrace(bubble, message.tool_trace || []);
            if (message.status === 'error') bubble.bubble.classList.add('chat-bubble-error');
            if (message.usage && message.usage.total_tokens) {
                bubble.meta.textContent = `本轮 ${message.usage.total_tokens} tokens`;
            }
            if (message.role === 'assistant' && message.id) {
                this.attachFeedback(bubble, message.id);
            }
        }

        async selectConversation(id, silent = false) {
            if (!id) return false;
            try {
                const response = await fetch(`/api/chat/conversations/${id}/messages?limit=100`);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                this.state.conversationId = Number(id);
                this.state.answer = '';
                this.state.citations = [];
                this.state.trace = [];
                this.state.messageId = null;
                storeConversation(id);
                this.messagesEl.innerHTML = '';
                if (!data.items.length) {
                    this.showEmpty();
                } else {
                    data.items.forEach((message) => this.messageFromData(message));
                }
                const current = document.querySelector(`.chat-conv-item[data-id="${id}"]`);
                if (this.titleEl) {
                    const title = current ? current.querySelector('.chat-conv-title') : null;
                    this.titleEl.textContent = title ? title.textContent : 'AI 资讯助手';
                }
                $$('.chat-conv-item').forEach((item) => {
                    item.classList.toggle('active', Number(item.dataset.id) === Number(id));
                });
                const side = $('[data-chat-side]');
                if (side) side.classList.remove('open');
                this.scrollToBottom();
                this.updateUrl();
                this.syncExpandLink();
                return true;
            } catch (error) {
                if (!silent && window.showToast) window.showToast('加载会话失败', 'error');
                if (silent) {
                    storeConversation('');
                    this.state.conversationId = null;
                }
                return false;
            }
        }

        async loadConversations() {
            if (!this.convListEl) return;
            try {
                const response = await fetch('/api/chat/conversations?limit=50');
                const data = await response.json();
                this.convListEl.innerHTML = '';
                (data.items || []).forEach((conversation) => {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = 'chat-conv-item';
                    button.dataset.id = conversation.id;
                    if (Number(conversation.id) === Number(this.state.conversationId)) {
                        button.classList.add('active');
                    }

                    const title = document.createElement('div');
                    title.className = 'chat-conv-title';
                    title.textContent = conversation.title || '新对话';

                    const meta = document.createElement('div');
                    meta.className = 'chat-conv-meta';
                    const time = document.createElement('span');
                    time.className = 'chat-conv-time';
                    time.textContent = formatTime(conversation.updated_at);
                    const remove = document.createElement('span');
                    remove.className = 'chat-conv-delete';
                    remove.title = '删除会话';
                    remove.textContent = '×';
                    remove.addEventListener('click', async (event) => {
                        event.stopPropagation();
                        try {
                            await fetch(`/api/chat/conversations/${conversation.id}`, { method: 'DELETE' });
                            if (Number(conversation.id) === Number(this.state.conversationId)) this.newChat();
                            this.loadConversations();
                        } catch (e) {
                            if (window.showToast) window.showToast('删除失败', 'error');
                        }
                    });
                    meta.append(time, remove);

                    button.append(title, meta);
                    button.addEventListener('click', () => this.selectConversation(conversation.id));
                    this.convListEl.appendChild(button);
                });
                if (!(data.items || []).length) {
                    const hint = document.createElement('div');
                    hint.className = 'chat-conv-empty';
                    hint.textContent = '还没有对话，问一个试试。';
                    this.convListEl.appendChild(hint);
                }
            } catch (error) {
                this.convListEl.innerHTML = '<div class="chat-conv-empty">会话列表加载失败</div>';
            }
        }

        async loadStatus() {
            if (!this.statusEl) return;
            try {
                const response = await fetch('/api/retrieval/status');
                const data = await response.json();
                const parts = [
                    data.fts_available ? '全文检索 ✓' : '全文检索降级',
                    `共 ${data.news_total || 0} 条新闻`
                ];
                this.statusEl.textContent = parts.join(' · ');
            } catch (error) {
                this.statusEl.textContent = '服务状态未知';
            }
        }

        newChat() {
            if (this.state.controller) this.state.controller.abort();
            this.state.conversationId = null;
            this.state.answer = '';
            this.state.citations = [];
            this.state.trace = [];
            this.state.messageId = null;
            this.state.bubble = null;
            storeConversation('');
            this.messagesEl.innerHTML = '';
            this.showEmpty();
            if (this.titleEl) this.titleEl.textContent = 'AI 资讯助手';
            $$('.chat-conv-item').forEach((item) => item.classList.remove('active'));
            const side = $('[data-chat-side]');
            if (side) side.classList.remove('open');
            this.updateUrl(true);
            this.syncExpandLink();
            this.loadMemoryHint();
            this.inputEl.focus();
        }

        async restore() {
            if (this.restored) return;
            this.restored = true;
            const params = new URLSearchParams(window.location.search);
            const urlId = params.get('c');
            const saved = readStoredConversation();
            const id = urlId || saved;
            if (!id) return;
            const ok = await this.selectConversation(id, true);
            if (!ok) this.showEmpty();
        }

        async ensureRestored() {
            if (this.restored) return;
            await this.restore();
        }

        updateUrl(remove = false) {
            if (this.variant !== 'page' || !window.history || !window.history.replaceState) return;
            const url = new URL(window.location.href);
            if (remove || !this.state.conversationId) {
                url.searchParams.delete('c');
            } else {
                url.searchParams.set('c', String(this.state.conversationId));
            }
            window.history.replaceState({}, '', url);
        }

        syncExpandLink() {
            if (this.variant !== 'float') return;
            const link = $('[data-chat-expand]');
            if (!link) return;
            link.href = this.state.conversationId ? `/chat?c=${this.state.conversationId}` : '/chat';
        }

        async loadMemoryHint(force = false) {
            if (this.memoryPromise && !force) return this.memoryPromise;
            this.memoryPromise = (async () => {
                try {
                    const response = await fetch('/api/memory');
                    if (!response.ok) return;
                    const data = await response.json();
                    if (!data.enabled) {
                        $$('[data-chat-memory-hint], [data-assistant-memory-hint]').forEach((el) => { el.hidden = true; });
                        if (this.memoryPanel) {
                            const profileEl = $('[data-memory-profile]', this.memoryPanel);
                            if (profileEl) profileEl.innerHTML = '<div class="memory-empty">记忆系统未启用（可在 .env 中设置 AGENT_MEMORY_ENABLED=true）。</div>';
                            const contentEl = $('[data-memory-content]', this.memoryPanel);
                            if (contentEl) contentEl.innerHTML = '';
                            const feedbackEl = $('[data-memory-feedback]', this.memoryPanel);
                            if (feedbackEl) feedbackEl.innerHTML = '';
                        }
                        return;
                    }
                    const profile = data.profile || {};
                    const topics = (profile.top_topics || []).slice(0, 3);
                    const tags = (profile.top_tags || []).slice(0, 3);
                    let text = '';
                    if (topics.length) text = `我记得你最近关注：${topics.join('、')}`;
                    else if (tags.length) text = `我记得你常读：${tags.join('、')}`;
                    else if (profile.has_signal) text = '我正在从你的阅读习惯里学习';
                    $$('[data-chat-memory-hint], [data-assistant-memory-hint]').forEach((el) => {
                        if (text) {
                            el.textContent = text;
                            el.hidden = false;
                        }
                    });
                    const sub = $('[data-assistant-sub]');
                    if (sub && text) sub.textContent = text;
                    if (this.memoryPanel) this.renderMemoryPanel(data);
                } catch (error) {
                    // 记忆不可用时保持静默，不影响对话
                }
            })();
            try {
                await this.memoryPromise;
            } finally {
                this.memoryPromise = null;
            }
        }

        wireMemoryPanel() {
            if (!this.memoryPanel) return;
            const edit = $('[data-memory-edit]', this.memoryPanel);
            const content = $('[data-memory-content]', this.memoryPanel);
            const editor = $('[data-memory-editor]', this.memoryPanel);
            const actions = $('[data-memory-editor-actions]', this.memoryPanel);
            const refresh = $('[data-memory-refresh]', this.memoryPanel);
            const reset = $('[data-memory-reset]', this.memoryPanel);

            const showEditor = () => {
                if (!editor) return;
                editor.value = this.memoryText || '';
                editor.hidden = false;
                actions.hidden = false;
                content.hidden = true;
                editor.focus();
            };
            const hideEditor = () => {
                if (!editor) return;
                editor.hidden = true;
                actions.hidden = true;
                content.hidden = false;
            };

            if (edit) edit.addEventListener('click', showEditor);
            const cancel = $('[data-memory-cancel]', this.memoryPanel);
            if (cancel) cancel.addEventListener('click', hideEditor);
            const save = $('[data-memory-save]', this.memoryPanel);
            if (save) {
                save.addEventListener('click', async () => {
                    save.disabled = true;
                    try {
                        const response = await fetch('/api/memory', {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ content: editor.value })
                        });
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
                        this.memoryText = data.content;
                        content.innerHTML = renderMarkdown(data.content, []);
                        hideEditor();
                        if (window.showToast) window.showToast('记忆已保存', 'success');
                    } catch (error) {
                        if (window.showToast) window.showToast(`保存失败：${error.message}`, 'error');
                    } finally {
                        save.disabled = false;
                    }
                });
            }
            if (refresh) {
                refresh.addEventListener('click', async () => {
                    refresh.disabled = true;
                    const old = refresh.textContent;
                    refresh.textContent = '…';
                    try {
                        const response = await fetch('/api/memory/refresh', { method: 'POST' });
                        const data = await response.json();
                        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
                        this.renderMemoryPanel(data);
                        const count = (data.reflection && data.reflection.reflected) || 0;
                        if (window.showToast) window.showToast(count ? `已重新整理，消化 ${count} 条反馈` : '画像已刷新', 'success');
                    } catch (error) {
                        if (window.showToast) window.showToast(`整理失败：${error.message}`, 'error');
                    } finally {
                        refresh.disabled = false;
                        refresh.textContent = old || '↻';
                    }
                });
            }
            if (reset) {
                reset.addEventListener('click', async () => {
                    if (!window.confirm('确定重置长期记忆？用户画像、跟踪任务和反馈整改都会被清空。')) return;
                    try {
                        await fetch('/api/memory', { method: 'DELETE' });
                        await this.loadMemoryHint(true);
                        if (window.showToast) window.showToast('记忆已重置', 'success');
                    } catch (error) {
                        if (window.showToast) window.showToast('重置失败', 'error');
                    }
                });
            }
            const toggle = $('[data-memory-toggle]');
            if (toggle) toggle.addEventListener('click', () => this.memoryPanel.classList.toggle('open'));
            const close = $('[data-memory-close]', this.memoryPanel);
            if (close) close.addEventListener('click', () => this.memoryPanel.classList.remove('open'));
            const jump = $('[data-memory-jump]');
            if (jump) {
                jump.addEventListener('click', (event) => {
                    event.preventDefault();
                    this.memoryPanel.classList.add('open');
                });
            }
            if (new URLSearchParams(window.location.search).get('panel') === 'memory') {
                this.memoryPanel.classList.add('open');
            }
        }

        renderMemoryPanel(data) {
            const profile = data.profile || {};
            const totals = profile.totals || {};
            const profileEl = $('[data-memory-profile]', this.memoryPanel);
            if (profileEl) {
                const categories = (profile.tags || []).filter((item) => item.read || item.fav).slice(0, 5);
                const topics = (profile.topics || []).slice(0, 8);
                const recent = (profile.recent_reads || []).slice(0, 3);
                let html = `
                    <div class="memory-stats">
                        <div><b>${totals.read || 0}</b><span>已读</span></div>
                        <div><b>${totals.fav || 0}</b><span>收藏</span></div>
                        <div><b>${totals.read_week || 0}</b><span>近 7 天</span></div>
                        <div><b>${Math.round((totals.read_rate || 0) * 100)}%</b><span>阅读率</span></div>
                    </div>`;
                if (categories.length) {
                    html += '<div class="memory-bars">' + categories.map((item) => `
                        <div class="memory-bar-row">
                            <span>${escapeHtml(item.tag || item.name)}</span>
                            <div class="memory-bar"><i style="width:${Math.max(4, Math.round((item.affinity || 0) * 100))}%"></i></div>
                            <em>${item.read || 0}</em>
                        </div>`).join('') + '</div>';
                }
                if (topics.length) {
                    html += '<div class="memory-chips">' + topics.map((item) => `<span>${escapeHtml(item.tag)} <em>${item.count}</em></span>`).join('') + '</div>';
                }
                if (recent.length) {
                    html += '<div class="memory-recent"><span>最近在读</span>' + recent.map((item) => `<a href="/news/${item.id}">${escapeHtml(item.title)}</a>`).join('') + '</div>';
                }
                if (!profile.has_signal) {
                    html += '<p class="memory-empty">多读几篇、收藏或反馈，我就能更懂你。</p>';
                }
                profileEl.innerHTML = html;
            }

            this.memoryText = data.content || '';
            const contentEl = $('[data-memory-content]', this.memoryPanel);
            if (contentEl) contentEl.innerHTML = renderMarkdown(this.memoryText, []);
            const path = $('[data-memory-path]', this.memoryPanel);
            if (path && data.meta) path.textContent = data.meta.path || '';

            const feedbackEl = $('[data-memory-feedback]', this.memoryPanel);
            const countEl = $('[data-memory-feedback-count]', this.memoryPanel);
            if (feedbackEl) {
                const items = data.feedback || [];
                if (countEl) countEl.textContent = items.length ? `${items.length} 条` : '';
                if (!items.length) {
                    feedbackEl.innerHTML = '<div class="memory-empty">还没有反馈。在回答或文章下点「不够好」，我会记住。</div>';
                } else {
                    feedbackEl.innerHTML = items.slice(0, 8).map((item) => {
                        const positive = item.rating > 0;
                        return `<div class="memory-feedback-item ${positive ? 'up' : 'down'}">
                            <div class="memory-feedback-top">
                                <span>${positive ? '👍' : '👎'} ${escapeHtml(item.reason_label || '反馈')}</span>
                                <span>${formatTime(item.created_at)}</span>
                            </div>
                            <div class="memory-feedback-target">${escapeHtml(item.title || item.excerpt || '助手回答')}</div>
                            ${item.comment ? `<div class="memory-feedback-comment">${escapeHtml(item.comment)}</div>` : ''}
                        </div>`;
                    }).join('');
                }
            }
        }
    }

    function init() {
        $$('[data-chat-root]').forEach((root) => {
            const controller = new ChatController(root);
            if (controller.messagesEl) controllers.push(controller);
        });

        const page = controllers.find((item) => item.variant === 'page');
        const floatController = controllers.find((item) => item.variant === 'float');
        const assistant = floatController || page;

        document.addEventListener('click', (event) => {
            const newButton = event.target.closest('[data-chat-new]');
            if (newButton && assistant) {
                event.preventDefault();
                assistant.newChat();
                return;
            }
            const sideToggle = event.target.closest('[data-chat-side-toggle], #sideToggle');
            if (sideToggle) {
                event.preventDefault();
                const side = $('[data-chat-side]');
                if (side) side.classList.toggle('open');
            }
        });

        const orb = $('[data-assistant-orb]');
        const panel = $('[data-assistant-panel]');
        const closeButton = panel ? $('[data-assistant-close]', panel) : null;

        const openAssistant = (prefill = '') => {
            if (!panel) {
                if (page) {
                    if (prefill) page.inputEl.value = prefill;
                    page.inputEl.focus();
                }
                return;
            }
            panel.classList.add('open');
            panel.setAttribute('aria-hidden', 'false');
            document.body.classList.add('assistant-open');
            positionAssistantPanel();
            if (orb) orb.classList.add('active');
            if (floatController) {
                floatController.ensureRestored();
                floatController.loadMemoryHint();
                if (prefill) {
                    floatController.inputEl.value = prefill;
                    floatController.autoGrow();
                }
                setTimeout(() => {
                    floatController.inputEl.focus();
                    const length = floatController.inputEl.value.length;
                    try { floatController.inputEl.setSelectionRange(length, length); } catch (e) { /* ignore */ }
                }, 60);
            }
        };

        const closeAssistant = () => {
            if (!panel) return;
            panel.classList.remove('open');
            panel.setAttribute('aria-hidden', 'true');
            document.body.classList.remove('assistant-open');
            if (orb) orb.classList.remove('active');
        };

        window.CronNewsAssistant = {
            open: openAssistant,
            close: closeAssistant,
            toggle: () => {
                if (!panel) return;
                if (panel.classList.contains('open')) closeAssistant();
                else openAssistant();
            },
            send: (text) => {
                openAssistant();
                if (floatController && text) floatController.sendMessage(text);
                else if (page && text) page.sendMessage(text);
            },
            newChat: () => {
                if (assistant) assistant.newChat();
            }
        };

        const POS_KEY = 'cronnews.assistant.pos';
        let suppressOrbClickUntil = 0;

        const clamp = (value, low, high) => Math.max(low, Math.min(value, high));

        function setOrbPosition(left, top) {
            if (!orb) return;
            const target = orb.closest('[data-assistant]') || orb;
            const width = target.offsetWidth || 56;
            const height = target.offsetHeight || 56;
            const nextLeft = clamp(left, 8, Math.max(8, window.innerWidth - width - 8));
            const nextTop = clamp(top, 8, Math.max(8, window.innerHeight - height - 8));
            target.style.left = `${nextLeft}px`;
            target.style.top = `${nextTop}px`;
            target.style.right = 'auto';
            target.style.bottom = 'auto';
            orb.style.left = '';
            orb.style.top = '';
            orb.style.right = '';
            orb.style.bottom = '';
        }

        function restoreOrbPosition() {
            try {
                const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
                if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
                    setOrbPosition(saved.left, saved.top);
                }
            } catch (e) { /* ignore */ }
        }

        function positionAssistantPanel() {
            if (!panel) return;
            if (window.innerWidth <= 680) {
                panel.style.left = '';
                panel.style.top = '';
                panel.style.right = '';
                panel.style.bottom = '';
                return;
            }
            const orbRect = orb ? orb.getBoundingClientRect() : null;
            const width = panel.offsetWidth || 424;
            const height = panel.offsetHeight || 638;
            let left = orbRect ? orbRect.right - width : window.innerWidth - width - 22;
            let top = orbRect ? orbRect.top + orbRect.height - height : window.innerHeight - height - 90;
            if (orbRect && orbRect.left < window.innerWidth / 2) {
                left = orbRect.left;
            }
            if (top < 8) {
                top = orbRect ? orbRect.bottom + 10 : 8;
            }
            left = clamp(left, 8, Math.max(8, window.innerWidth - width - 8));
            top = clamp(top, 8, Math.max(8, window.innerHeight - height - 8));
            panel.style.left = `${left}px`;
            panel.style.top = `${top}px`;
            panel.style.right = 'auto';
            panel.style.bottom = 'auto';
        }

        function makeOrbDraggable() {
            if (!orb) return;
            let dragging = false;
            let moved = false;
            let startX = 0;
            let startY = 0;
            let startLeft = 0;
            let startTop = 0;

            orb.addEventListener('pointerdown', (event) => {
                if (event.button !== 0) return;
                const rect = orb.getBoundingClientRect();
                dragging = true;
                moved = false;
                startX = event.clientX;
                startY = event.clientY;
                startLeft = rect.left;
                startTop = rect.top;
                try { orb.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
            });

            orb.addEventListener('pointermove', (event) => {
                if (!dragging) return;
                const dx = event.clientX - startX;
                const dy = event.clientY - startY;
                if (!moved && Math.abs(dx) + Math.abs(dy) < 5) return;
                moved = true;
                setOrbPosition(startLeft + dx, startTop + dy);
                if (panel && panel.classList.contains('open')) positionAssistantPanel();
            });

            const finish = (event) => {
                if (!dragging) return;
                dragging = false;
                if (moved) {
                    const rect = orb.getBoundingClientRect();
                    try {
                        localStorage.setItem(POS_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
                    } catch (e) { /* ignore */ }
                    suppressOrbClickUntil = Date.now() + 350;
                    event.preventDefault();
                }
                try { orb.releasePointerCapture(event.pointerId); } catch (e) { /* ignore */ }
            };
            orb.addEventListener('pointerup', finish);
            orb.addEventListener('pointercancel', () => { dragging = false; });

            restoreOrbPosition();
            window.addEventListener('resize', () => {
                const rect = orb.getBoundingClientRect();
                setOrbPosition(rect.left, rect.top);
                if (panel && panel.classList.contains('open')) positionAssistantPanel();
            });
        }

        if (orb && panel) {
            makeOrbDraggable();
            orb.addEventListener('click', () => {
                if (Date.now() < suppressOrbClickUntil) return;
                if (panel.classList.contains('open')) closeAssistant();
                else openAssistant();
            });
            if (closeButton) closeButton.addEventListener('click', closeAssistant);
            document.addEventListener('keydown', (event) => {
                if (event.key === 'Escape' && panel.classList.contains('open')) closeAssistant();
            });
            document.addEventListener('click', (event) => {
                if (!panel.classList.contains('open')) return;
                if (panel.contains(event.target) || orb.contains(event.target)) return;
                closeAssistant();
            }, true);
        }

        $$('[data-assistant-open]').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                openAssistant(button.dataset.assistantOpen || '');
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
