/**
 * Conversation History Manager
 * Manages sidebar tabs, conversation history display, and session management
 */

// Utility function to normalize project_id (only allow valid UUIDs)
function normalizeProjectId(projectId) {
    if (!projectId) return null;

    // UUID pattern check
    const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    if (uuidPattern.test(projectId)) {
        return projectId;
    }

    return null;
}

class ConversationHistoryManager {
    constructor() {
        this.currentSessionId = null;
        this.characterName = null;
        this.conversations = [];
        this.isLoading = false;
        this.autoRefreshTimer = null;
        this.pendingAutoRefresh = false;
        this.autoRefreshDelayMs = 1200;

        // DOM Elements
        this.elements = {
            // Sidebar tabs
            tabFiles: document.getElementById('sidebarTabFiles'),
            tabHistory: document.getElementById('sidebarTabHistory'),
            contentFiles: document.getElementById('sidebarContentFiles'),
            contentHistory: document.getElementById('sidebarContentHistory'),
            // History list
            historyList: document.getElementById('conversationHistoryList'),
            refreshHistoryBtn: document.getElementById('refreshHistoryBtn'),
            historyProjectFilter: document.getElementById('historyProjectFilter'),
            // New conversation button
            newConversationBtn: document.getElementById('newConversationBtn'),
            // Character select (for getting current character)
            characterSelect: document.getElementById('characterSelect')
        };

        this.init();
    }

    init() {
        this.setupTabListeners();
        this.setupEventListeners();
        this.setupKeyboardShortcuts();
        this.setupAutoRefreshListeners();
    }

    setupTabListeners() {
        const tabs = [this.elements.tabFiles, this.elements.tabHistory];
        tabs.forEach(tab => {
            if (!tab) return;
            tab.addEventListener('click', (e) => {
                const tabName = tab.dataset.sidebarTab;
                this.switchTab(tabName);
            });
        });
    }

    switchTab(tabName) {
        // Update tab styles
        const tabs = document.querySelectorAll('[data-sidebar-tab]');
        tabs.forEach(tab => {
            if (tab.dataset.sidebarTab === tabName) {
                tab.classList.remove('text-gray-400', 'border-transparent');
                tab.classList.add('text-emerald-400', 'border-emerald-400');
            } else {
                tab.classList.remove('text-emerald-400', 'border-emerald-400');
                tab.classList.add('text-gray-400', 'border-transparent');
            }
        });

        // Show/hide content
        const contents = document.querySelectorAll('[data-sidebar-content]');
        contents.forEach(content => {
            if (content.dataset.sidebarContent === tabName) {
                content.classList.remove('hidden');
            } else {
                content.classList.add('hidden');
            }
        });

        // Load history when history tab is activated
        if (tabName === 'history' && !this.isLoading) {
            this.loadConversations();
        }
    }

    setupEventListeners() {
        // Refresh button
        if (this.elements.refreshHistoryBtn) {
            this.elements.refreshHistoryBtn.addEventListener('click', () => {
                this.loadConversations();
            });
        }

        // Project filter
        if (this.elements.historyProjectFilter) {
            this.elements.historyProjectFilter.addEventListener('change', () => {
                console.log('[ConversationHistory] Project filter changed to:', this.elements.historyProjectFilter.value);
                this.loadConversations();
            });
        }

        // New conversation button
        if (this.elements.newConversationBtn) {
            this.elements.newConversationBtn.addEventListener('click', () => {
                this.createNewConversation();
            });
        }
    }

    setupAutoRefreshListeners() {
        document.addEventListener('conversationHistoryRefresh', (event) => {
            const reason = event?.detail?.reason || 'event';
            this.scheduleAutoRefresh(reason);
        });
    }

    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ctrl+Shift+O - New conversation
            if (e.ctrlKey && e.shiftKey && e.key === 'O') {
                e.preventDefault();
                this.createNewConversation();
            }
        });
    }

    getCurrentCharacter() {
        if (this.elements.characterSelect) {
            return this.elements.characterSelect.value || '葵';
        }
        return '葵';
    }

    scheduleAutoRefresh(reason = 'auto') {
        if (this.isLoading) {
            this.pendingAutoRefresh = true;
            return;
        }

        if (this.autoRefreshTimer) {
            clearTimeout(this.autoRefreshTimer);
        }

        this.autoRefreshTimer = setTimeout(() => {
            this.autoRefreshTimer = null;
            this.loadConversations();
        }, this.autoRefreshDelayMs);
    }

    async loadConversations() {
        if (this.isLoading) {
            this.pendingAutoRefresh = true;
            return;
        }
        this.isLoading = true;

        if (this.elements.historyList) {
            this.elements.historyList.innerHTML = `
                <div class="text-xs text-gray-500 text-center py-4">読み込み中...</div>
            `;
        }

        try {
            // Build URL with optional project filter
            const projectId = this.elements.historyProjectFilter?.value || '';
            let url;

            console.log('[ConversationHistory] Loading conversations with filter:', projectId);

            if (projectId === '' || projectId === 'all') {
                // Empty string or 'all' means "すべてのプロジェクト" - show all conversations
                url = '/api/conversations?limit=50';
            } else if (projectId === 'none') {
                // 'none' means "プロジェクトなし" - only show conversations without project_id
                url = '/api/conversations?project_id=none&limit=50';
            } else {
                // Specific project ID
                url = `/api/conversations?project_id=${encodeURIComponent(projectId)}&limit=50`;
            }

            console.log('[ConversationHistory] API URL:', url);

            const response = await fetch(url, {
                credentials: 'include'
            });

            if (!response.ok) {
                throw new Error('Failed to load conversations');
            }

            const data = await response.json();

            console.log(`[ConversationHistory] API returned ${data.conversations?.length || 0} conversations`);
            if (data.conversations && data.conversations.length > 0) {
                console.log('[ConversationHistory] Sample project_ids:',
                    data.conversations.slice(0, 5).map(c => ({
                        id: c.id?.substring(0, 8) || 'no-id',
                        project_id: c.project_id?.substring(0, 8) || 'null'
                    })));
            }

            this.conversations = data.conversations || [];
            this.renderConversations();

        } catch (error) {
            console.error('Failed to load conversations:', error);
            if (this.elements.historyList) {
                this.elements.historyList.innerHTML = `
                    <div class="text-xs text-red-400 text-center py-4">読み込みに失敗しました</div>
                `;
            }
        } finally {
            this.isLoading = false;
            if (this.pendingAutoRefresh) {
                this.pendingAutoRefresh = false;
                this.scheduleAutoRefresh('pending');
            }
        }
    }

    renderConversations() {
        if (!this.elements.historyList) return;

        console.log(`[ConversationHistory] Rendering ${this.conversations?.length || 0} conversations`);

        if (!this.conversations.length) {
            this.elements.historyList.innerHTML = `
                <div class="text-xs text-gray-500 text-center py-8">
                    <svg class="w-8 h-8 mx-auto mb-2 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
                    </svg>
                    <p>会話履歴がありません</p>
                    <p class="mt-1 text-gray-600">新規会話を始めましょう</p>
                </div>
            `;
            return;
        }

        this.elements.historyList.innerHTML = '';

        this.conversations.forEach(conv => {
            const item = this.createConversationItem(conv);
            this.elements.historyList.appendChild(item);
        });
    }

    createConversationItem(conv) {
        const div = document.createElement('div');
        div.className = `conversation-item group relative p-2 rounded cursor-pointer transition-colors ${conv.id === this.currentSessionId
            ? 'bg-emerald-900/30 border border-emerald-700'
            : 'hover:bg-gray-700/50'
            }`;
        div.dataset.sessionId = conv.id;

        // Format date
        const date = conv.last_activity ? new Date(conv.last_activity) : new Date();
        const dateStr = this.formatDate(date);

        // Title or fallback
        const title = conv.title || `会話 ${conv.message_count || 0} 件`;

        div.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-sm text-gray-200 truncate flex-1" title="${this.escapeHtml(title)}">${this.escapeHtml(title)}</span>
                <span class="text-xs text-gray-500 ml-2 flex-shrink-0 group-hover:hidden">${dateStr}</span>
                <button class="delete-btn hidden group-hover:flex items-center justify-center w-6 h-6 ml-2 flex-shrink-0 rounded hover:bg-red-600/50 text-gray-400 hover:text-red-400 transition-colors" title="削除">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                    </svg>
                </button>
            </div>
                    <div class="flex items-center justify-between mt-0.5">
                        <span class="text-xs text-gray-400">${conv.character_name || ''}</span>
                        <span class="text-xs text-gray-500">${conv.message_count || 0}件</span>
                    </div>
                `;

        // Delete button click handler
        const deleteBtn = div.querySelector('.delete-btn');
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation(); // Prevent triggering resume
            this.confirmDeleteConversation(conv.id, title);
        });

        // Click to resume
        div.addEventListener('click', () => {
            this.resumeConversation(conv.id);
        });

        return div;
    }

    async confirmDeleteConversation(sessionId, title) {
        const confirmed = confirm(`「${title}」を削除しますか？\nこの操作は取り消せません。`);
        if (!confirmed) return;

        await this.deleteConversation(sessionId);
    }

    async deleteConversation(sessionId) {
        try {
            const response = await fetch(`/api/conversations/${sessionId}`, {
                method: 'DELETE',
                credentials: 'include'
            });

            if (!response.ok) {
                throw new Error('Failed to delete conversation');
            }

            const data = await response.json();

            if (data.success) {
                // Remove from local array
                this.conversations = this.conversations.filter(c => c.id !== sessionId);

                // If deleted the current session, reset it
                if (this.currentSessionId === sessionId) {
                    this.currentSessionId = null;
                    if (window.chatClient) {
                        window.chatClient.setCurrentConversationSessionId(null);
                    }
                    const chatMessages = document.getElementById('chatMessages');
                    if (chatMessages) {
                        chatMessages.innerHTML = '';
                    }
                }

                // Re-render the list
                this.renderConversations();

                this.showNotification('会話を削除しました', 'success');
            }
        } catch (error) {
            console.error('Failed to delete conversation:', error);
            this.showNotification('削除に失敗しました', 'error');
        }
    }

    formatDate(date) {
        const now = new Date();
        const diffMs = now - date;
        const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

        if (diffDays === 0) {
            return date.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
        } else if (diffDays === 1) {
            return '昨日';
        } else if (diffDays < 7) {
            return `${diffDays} 日前`;
        } else {
            return date.toLocaleDateString('ja-JP', { month: 'numeric', day: 'numeric' });
        }
    }

    async createNewConversation() {
        // 複数ソースからproject_idを取得（優先順位付きフォールバック）
        let projectId = null;

        // 1. projectSelect（ヘッダー）から取得
        const projectSelect = document.getElementById('projectSelect');
        const selectValue = projectSelect?.value || null;

        // 特殊値（空文字、none、all）でない場合のみ使用
        if (selectValue && selectValue !== 'none' && selectValue !== 'all' && selectValue !== '') {
            projectId = selectValue;
        }

        // 2. historyProjectFilter（サイドバー）からフォールバック
        if (!projectId) {
            const historyFilter = this.elements.historyProjectFilter;
            const filterValue = historyFilter?.value || null;
            if (filterValue && filterValue !== 'none' && filterValue !== 'all' && filterValue !== '') {
                projectId = filterValue;
            }
        }

        // 3. localStorageからフォールバック
        if (!projectId) {
            const savedProjectId = localStorage.getItem('selectedProjectId');
            if (savedProjectId && savedProjectId !== 'none' && savedProjectId !== 'all' && savedProjectId !== '') {
                projectId = savedProjectId;
            }
        }

        console.log('[ConversationHistory] createNewConversation projectId sources:', {
            projectSelect: selectValue,
            historyFilter: this.elements.historyProjectFilter?.value,
            localStorage: localStorage.getItem('selectedProjectId'),
            resolved: projectId
        });

        // Normalize project_id (only UUIDs are valid)
        const normalizedProjectId = normalizeProjectId(projectId);

        // キャラクター名を取得
        const characterName = this.getCurrentCharacter();

        try {
            // セッションを即座に作成し、プロジェクトIDを含める
            const payload = {
                character_name: characterName
            };

            // Only include project_id if it's a valid UUID
            if (normalizedProjectId) {
                payload.project_id = normalizedProjectId;
            }

            const response = await fetch('/api/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                throw new Error('セッションの作成に失敗しました');
            }

            const data = await response.json();
            if (data.success && data.session) {
                this.currentSessionId = data.session.id;

                // Notify chatClient about new session
                if (window.chatClient) {
                    window.chatClient.setCurrentConversationSessionId(data.session.id);
                }
            }
        } catch (error) {
            console.error('Failed to create new conversation session:', error);
            // エラーが発生しても、従来の挙動（メッセージ送信時に作成）にフォールバック
            this.currentSessionId = null;
            if (window.chatClient) {
                window.chatClient.setCurrentConversationSessionId(null);
            }
        }

        // Clear chat messages UI
        const chatMessages = document.getElementById('chatMessages');
        if (chatMessages) {
            chatMessages.innerHTML = '';
        }

        // WebSocket経由でサーバー側もクリア
        if (window.chatClient && window.chatClient.ws && window.chatClient.isConnected) {
            window.chatClient.ws.send(JSON.stringify({ type: 'clear_chat' }));
        }

        // Show notification
        this.showNotification('新規会話を開始しました', 'success');

        // Dispatch event for other components
        document.dispatchEvent(new CustomEvent('conversationChanged', {
            detail: {
                sessionId: this.currentSessionId,
                isNew: true,
                projectId: normalizedProjectId  // プロジェクトID情報も含める
            }
        }));
    }

    async resumeConversation(sessionId) {
        if (sessionId === this.currentSessionId) return;

        try {
            const response = await fetch(`/api/conversations/${sessionId}/resume`, {
                method: 'POST',
                credentials: 'include'
            });

            if (!response.ok) {
                throw new Error('Failed to resume conversation');
            }

            const data = await response.json();

            if (data.success) {
                this.currentSessionId = sessionId;

                // Notify chatClient about session change
                if (window.chatClient) {
                    window.chatClient.setCurrentConversationSessionId(sessionId);
                }

                // Clear and restore chat messages
                const chatMessages = document.getElementById('chatMessages');
                if (chatMessages && data.messages) {
                    chatMessages.innerHTML = '';
                    data.messages.forEach(msg => {
                        this.appendMessage(msg);
                    });
                    // Scroll to bottom
                    chatMessages.scrollTop = chatMessages.scrollHeight;
                }

                // Update history list UI
                this.renderConversations();

                // Show notification
                const conv = this.conversations.find(c => c.id === sessionId);
                const title = conv?.title || '会話';
                this.showNotification(`${title} を再開しました`, 'success');

                // Dispatch event
                document.dispatchEvent(new CustomEvent('conversationChanged', {
                    detail: { sessionId: this.currentSessionId, isNew: false }
                }));
            }

        } catch (error) {
            console.error('Failed to resume conversation:', error);
            this.showNotification('会話の再開に失敗しました', 'error');
        }
    }

    appendMessage(msg) {
        const chatMessages = document.getElementById('chatMessages');
        if (!chatMessages) return;

        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${msg.role === 'user' ? 'user-message' : 'assistant-message'}`;

        if (msg.role === 'user') {
            messageDiv.className = 'flex justify-end';
            messageDiv.innerHTML = `
                <div class="max-w-[85%] px-4 py-3 rounded-2xl bg-emerald-700 text-white">
                    <p class="text-sm whitespace-pre-wrap">${this.escapeHtml(msg.content)}</p>
                </div>
            `;
        } else {
            messageDiv.className = 'flex justify-start';
            messageDiv.innerHTML = `
                <div class="max-w-[85%] px-4 py-3 rounded-2xl bg-gray-700 text-gray-100">
                    <p class="text-sm whitespace-pre-wrap">${this.escapeHtml(msg.content)}</p>
                </div>
            `;
        }

        chatMessages.appendChild(messageDiv);
    }

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    showNotification(message, type = 'info') {
        // Try to use ChatClient's notification system if available
        if (window.chatClient && typeof window.chatClient.showNotification === 'function') {
            window.chatClient.showNotification(message, type);
            return;
        }

        // Fallback: simple console log
        console.log(`[${type}] ${message}`);
    }

    // Public method to get current session ID
    getCurrentSessionId() {
        return this.currentSessionId;
    }

    // Public method to set current session ID (called from WebSocket handler)
    setCurrentSessionId(sessionId) {
        this.currentSessionId = sessionId;
    }

    // Reset current session (called when project selection changes)
    resetCurrentSession() {
        this.currentSessionId = null;
    }

    // Render project filter dropdown
    renderProjectFilter(projects) {
        if (!this.elements.historyProjectFilter) return;

        const select = this.elements.historyProjectFilter;
        const currentValue = select.value; // Preserve current selection

        // Clear existing options (without destroying the select element)
        while (select.firstChild) {
            select.removeChild(select.firstChild);
        }

        // Add "All Projects" option
        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = 'すべてのプロジェクト';
        select.appendChild(allOption);

        // Add "No Project" option
        const noneOption = document.createElement('option');
        noneOption.value = 'none';
        noneOption.textContent = 'プロジェクトなし';
        select.appendChild(noneOption);

        if (projects && projects.length > 0) {
            projects.forEach(project => {
                const option = document.createElement('option');
                option.value = project.id;
                option.textContent = project.name;
                select.appendChild(option);
            });
        }

        // Restore previous selection if it still exists
        if (currentValue) {
            select.value = currentValue;
        }
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.conversationHistoryManager = new ConversationHistoryManager();
});
