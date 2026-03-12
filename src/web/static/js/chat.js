// AoiTalk WebSocket Chat Client

class ChatClient {
    constructor() {
        // WebSocket connection
        this.ws = null;
        this.reconnectInterval = null;
        this.isConnected = false;
        this.isAuthenticated = false;
        this.appStarted = false;
        this.mobileCommands = [];
        this.commandHistory = [];
        this.mobileSettings = {};
        this.commandOverlayVisible = false;
        this.documents = [];
        this.currentDocumentName = null;  // Name of document being edited (null = new)

        // Image input state
        this.pendingImage = null;  // {data: base64, mimeType: string, name: string}

        // Media browser state
        this.mediaRootPath = '';         // Root path from config
        this.mediaCurrentPath = '';      // Current absolute path
        this.mediaBookmarks = [];        // Bookmark list
        this.mediaPathHistory = [];      // Navigation history
        this.mediaIsBookmarked = false;  // Current path bookmarked status
        this.mediaCanGoUp = false;       // Whether can navigate to parent
        this.mediaViewMode = 'thumbnail'; // View mode: 'thumbnail' or 'list'
        this.mediaAudioOnlyMode = false; // Audio-only playback mode for videos

        // DOM elements
        this.elements = {
            chatPane: document.getElementById('chatPane'),
            chatMessages: document.getElementById('chatMessages'),
            messageInput: document.getElementById('messageInput'),
            sendBtn: document.getElementById('sendBtn'),
            clearBtn: document.getElementById('newConversationBtn'),  // Legacy name, now points to newConversationBtn
            charCount: document.getElementById('charCount'),
            characterSelect: document.getElementById('characterSelect'),
            llmModel: document.getElementById('llmModel'),
            asrModel: document.getElementById('asrModel'),
            voiceStatusDot: document.getElementById('voiceStatusDot'),
            voiceStatusText: document.getElementById('voiceStatusText'),
            micLevelFill: document.getElementById('micLevelFill'),
            micLevelValue: document.getElementById('micLevelValue'),
            connectionDot: document.getElementById('connectionDot'),
            connectionText: document.getElementById('connectionText'),
            notifications: document.getElementById('notifications'),
            commandButtonList: document.getElementById('commandButtonList'),
            commandEmptyState: document.getElementById('commandEmptyState'),
            commandHistoryList: document.getElementById('commandHistoryList'),
            commandLauncherBtn: document.getElementById('commandLauncherBtn'),
            commandOverlay: document.getElementById('commandOverlay'),
            commandOverlayClose: document.getElementById('commandOverlayClose'),
            commandOverlayRefresh: document.getElementById('commandOverlayRefresh'),
            crawlerStatusSection: document.getElementById('crawlerStatusSection'),
            crawlerStatusList: document.getElementById('crawlerStatusList'),
            crawlerStatusRefresh: document.getElementById('crawlerStatusRefresh'),
            modeSwitch: document.getElementById('modeSwitch'),
            modeButtons: Array.from(document.querySelectorAll('[data-mode-option]')),
            modeStatus: document.getElementById('modeSwitchStatus'),
            modeOverlay: document.getElementById('modeRestartOverlay'),
            modeOverlayMessage: document.getElementById('modeRestartMessage'),
            modeOverlayCountdown: document.getElementById('modeRestartCountdown'),
            loginOverlay: document.getElementById('loginOverlay'),
            loginForm: document.getElementById('loginForm'),
            loginUser: document.getElementById('loginUser'),
            loginPass: document.getElementById('loginPass'),
            loginError: document.getElementById('loginError'),
            loginSubmit: document.getElementById('loginSubmit'),
            // Document management
            documentsSection: document.getElementById('documentsSection'),
            documentsList: document.getElementById('documentsList'),
            documentNewBtn: document.getElementById('documentNewBtn'),
            documentRefreshBtn: document.getElementById('documentRefreshBtn'),
            documentEditorModal: document.getElementById('documentEditorModal'),
            documentEditorName: document.getElementById('documentEditorName'),
            documentEditorContent: document.getElementById('documentEditorContent'),
            documentEditorClose: document.getElementById('documentEditorClose'),
            documentEditorSave: document.getElementById('documentEditorSave'),
            documentEditorDelete: document.getElementById('documentEditorDelete'),
            documentEditorCancel: document.getElementById('documentEditorCancel'),
            // Office file upload
            officeFileInput: document.getElementById('officeFileInput'),
            documentUploadBtn: document.getElementById('documentUploadBtn'),
            officeUploadResult: document.getElementById('officeUploadResult'),
            officeUploadFileName: document.getElementById('officeUploadFileName'),
            officeUploadContent: document.getElementById('officeUploadContent'),
            officeUploadInsert: document.getElementById('officeUploadInsert'),
            officeUploadSave: document.getElementById('officeUploadSave'),
            officeUploadClose: document.getElementById('officeUploadClose'),
            // Media browser
            mediaBrowserSection: document.getElementById('mediaBrowserSection'),
            mediaFolderList: document.getElementById('mediaFolderList'),
            mediaFileGrid: document.getElementById('mediaFileGrid'),
            mediaBreadcrumb: document.getElementById('mediaBreadcrumb'),
            mediaBookmarks: document.getElementById('mediaBookmarks'),
            mediaBackBtn: document.getElementById('mediaBackBtn'),
            mediaHomeBtn: document.getElementById('mediaHomeBtn'),
            mediaGoUpBtn: document.getElementById('mediaGoUpBtn'),
            mediaAddBookmarkBtn: document.getElementById('mediaAddBookmarkBtn'),
            // View mode toggle
            mediaViewModeThumbnail: document.getElementById('mediaViewModeThumbnail'),
            mediaViewModeList: document.getElementById('mediaViewModeList'),
            // Media viewer
            mediaViewerModal: document.getElementById('mediaViewerModal'),
            mediaViewerContent: document.getElementById('mediaViewerContent'),
            mediaViewerClose: document.getElementById('mediaViewerClose'),
            mediaViewerFileName: document.getElementById('mediaViewerFileName'),
            mediaViewerCounter: document.getElementById('mediaViewerCounter'),
            mediaViewerPrev: document.getElementById('mediaViewerPrev'),
            mediaViewerNext: document.getElementById('mediaViewerNext'),
            mediaViewerAudioToggle: document.getElementById('mediaViewerAudioToggle'),
            mediaViewerAudioIcon: document.getElementById('mediaViewerAudioIcon'),
            // External LLM permission modal
            externalLLMPermissionModal: document.getElementById('externalLLMPermissionModal'),
            permissionToolName: document.getElementById('permissionToolName'),
            permissionDescription: document.getElementById('permissionDescription'),
            permissionApproveBtn: document.getElementById('permissionApproveBtn'),
            permissionDenyBtn: document.getElementById('permissionDenyBtn'),
            // Image input elements
            imageInput: document.getElementById('imageInput'),
            imagePreviewContainer: document.getElementById('imagePreviewContainer'),
            imagePreview: document.getElementById('imagePreview'),
            imageFileName: document.getElementById('imageFileName'),
            removeImageBtn: document.getElementById('removeImageBtn'),
            // Processing indicator
            processingIndicator: document.getElementById('processingIndicator'),
            processingText: document.getElementById('processingText'),
            // (RAG management moved to rag-collection-manager.js)
            // User Files management
            userFilesSection: document.getElementById('userFilesSection'),
            userFilesList: document.getElementById('userFilesList'),
            userFileInput: document.getElementById('userFileInput'),
            userFileUploadBtn: document.getElementById('userFileUploadBtn'),
            userFileRefreshBtn: document.getElementById('userFileRefreshBtn'),
            // Music Player elements
            miniPlayerBar: document.getElementById('miniPlayerBar'),
            miniPlayerExpand: document.getElementById('miniPlayerExpand'),
            miniPlayerArt: document.getElementById('miniPlayerArt'),
            miniPlayerTitle: document.getElementById('miniPlayerTitle'),
            miniPlayerArtist: document.getElementById('miniPlayerArtist'),
            miniPlayerCurrentTime: document.getElementById('miniPlayerCurrentTime'),
            miniPlayerDuration: document.getElementById('miniPlayerDuration'),
            miniPlayerVolume: document.getElementById('miniPlayerVolume'),
            miniPlayerProgress: document.getElementById('miniPlayerProgress'),
            miniPlayerProgressFill: document.getElementById('miniPlayerProgressFill'),
            miniPlayerPlayPause: document.getElementById('miniPlayerPlayPause'),
            miniPlayerPlayIcon: document.getElementById('miniPlayerPlayIcon'),
            miniPlayerPauseIcon: document.getElementById('miniPlayerPauseIcon'),
            miniPlayerPrev: document.getElementById('miniPlayerPrev'),
            miniPlayerNext: document.getElementById('miniPlayerNext'),
            miniPlayerClose: document.getElementById('miniPlayerClose'),
            musicPlayerModal: document.getElementById('musicPlayerModal'),
            musicPlayerClose: document.getElementById('musicPlayerClose'),
            musicPlayerPlaylist: document.getElementById('musicPlayerPlaylist'),
            musicPlayerArt: document.getElementById('musicPlayerArt'),
            musicPlayerTitle: document.getElementById('musicPlayerTitle'),
            musicPlayerArtist: document.getElementById('musicPlayerArtist'),
            musicPlayerCurrentTime: document.getElementById('musicPlayerCurrentTime'),
            musicPlayerDuration: document.getElementById('musicPlayerDuration'),
            musicPlayerProgress: document.getElementById('musicPlayerProgress'),
            musicPlayerProgressFill: document.getElementById('musicPlayerProgressFill'),
            musicPlayerVolume: document.getElementById('musicPlayerVolume'),
            musicPlayerPlayPause: document.getElementById('musicPlayerPlayPause'),
            musicPlayerPlayIcon: document.getElementById('musicPlayerPlayIcon'),
            musicPlayerPauseIcon: document.getElementById('musicPlayerPauseIcon'),
            musicPlayerPrev: document.getElementById('musicPlayerPrev'),
            musicPlayerNext: document.getElementById('musicPlayerNext'),
            musicPlayerShuffle: document.getElementById('musicPlayerShuffle'),
            musicPlayerRepeat: document.getElementById('musicPlayerRepeat'),
            musicPlayerRepeatBadge: document.getElementById('musicPlayerRepeatBadge'),
            musicPlayerPlaylistPanel: document.getElementById('musicPlayerPlaylistPanel'),
            musicPlayerPlaylistItems: document.getElementById('musicPlayerPlaylistItems')
        };

        // Media viewer navigation state
        this.mediaViewerFiles = [];      // Current file list
        this.mediaViewerIndex = 0;       // Current file index
        this.mediaViewerType = null;     // Current media type

        // Voice status
        this.voiceStatus = {
            ready: false,
            rms: 0,
            recording: false
        };

        this.modeInfo = null;
        this.restartCountdownTimer = null;
        this.restartReloadTimer = null;

        // Feedback system
        this.messageCounter = 0;  // For generating unique message IDs
        this.feedbackModalData = null;  // Current message data for feedback

        // External LLM permission state
        this.pendingPermissionRequestId = null;

        // Music player state
        this.musicPlayer = {
            audio: new Audio(),
            playlist: [],           // Array of file objects
            currentIndex: 0,        // Current track index
            isPlaying: false,
            volume: 0.8,
            shuffle: false,
            repeat: 'none',         // 'none' | 'one' | 'all'
            shuffledOrder: [],      // Shuffled indices
            originalOrder: []       // Original indices for unshuffle
        };

        // Conversation session tracking
        this.currentConversationSessionId = null;  // Current active conversation session ID

        // LLM mode state ('fast' or 'thinking')
        this.llmMode = 'fast';

        // Initialize
        this.init();
    }

    async init() {
        // Setup event listeners
        this.setupEventListeners();
        this.setupMusicPlayerListeners();

        // Check auth status
        const authenticated = await this.checkAuthStatus();
        if (!authenticated) {
            this.showLoginOverlay();
            return;
        }

        this.startApp();
    }

    async startApp() {
        if (this.appStarted) return;
        this.appStarted = true;

        // Get config
        await this.fetchConfig();

        // Get characters list
        await this.fetchCharacters();

        // Load mode information
        await this.fetchModeStatus();

        // Load mobile commands / settings
        await this.fetchMobileCommands();

        // Load current LLM mode
        await this.fetchLlmMode();

        // Connect to WebSocket
        this.connect();

        // Setup periodic voice status check
        this.startVoiceStatusCheck();

        // Render initial command history placeholder
        this.renderCommandHistory();

        // Focus input
        this.elements.messageInput.focus();
        this.updateCharCount();
    }

    async checkAuthStatus() {
        try {
            const response = await fetch('/api/auth/status', { credentials: 'include' });
            if (!response.ok) {
                this.isAuthenticated = false;
                return false;
            }
            const data = await response.json();
            this.isAuthenticated = !!data.authenticated;
            if (this.isAuthenticated) {
                this.hideLoginOverlay();
            }
            return this.isAuthenticated;
        } catch (error) {
            console.error('Failed to check auth status:', error);
            this.isAuthenticated = false;
            return false;
        }
    }

    showLoginOverlay() {
        if (!this.elements.loginOverlay) return;
        this.elements.loginOverlay.classList.remove('hidden');
        this.isAuthenticated = false;
        if (this.reconnectInterval) {
            clearInterval(this.reconnectInterval);
            this.reconnectInterval = null;
        }
    }

    hideLoginOverlay() {
        if (!this.elements.loginOverlay) return;
        this.elements.loginOverlay.classList.add('hidden');
        this.isAuthenticated = true;
    }

    showLoginError(message) {
        if (!this.elements.loginError) return;
        this.elements.loginError.textContent = message;
        this.elements.loginError.classList.remove('hidden');
    }

    clearLoginError() {
        if (!this.elements.loginError) return;
        this.elements.loginError.textContent = '';
        this.elements.loginError.classList.add('hidden');
    }

    async handleLogin() {
        if (!this.elements.loginUser || !this.elements.loginPass) return;
        const username = this.elements.loginUser.value.trim();
        const password = this.elements.loginPass.value;
        if (!username || !password) {
            this.showLoginError('ユーザーIDとパスワードを入力してください');
            return;
        }

        try {
            this.clearLoginError();
            if (this.elements.loginSubmit) {
                this.elements.loginSubmit.disabled = true;
                this.elements.loginSubmit.classList.add('opacity-60');
            }
            const response = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ username, password })
            });
            if (!response.ok) {
                throw new Error('ログインに失敗しました');
            }
            this.hideLoginOverlay();
            await this.resumeAfterLogin();
        } catch (error) {
            console.error('Login failed:', error);
            this.showLoginError('ログインに失敗しました');
        } finally {
            if (this.elements.loginSubmit) {
                this.elements.loginSubmit.disabled = false;
                this.elements.loginSubmit.classList.remove('opacity-60');
            }
        }
    }

    async resumeAfterLogin() {
        if (!this.appStarted) {
            await this.startApp();
            return;
        }
        await this.fetchConfig();
        await this.fetchCharacters();
        await this.fetchModeStatus();
        await this.fetchMobileCommands();
        if (!this.isConnected) {
            this.connect();
        }
    }

    async apiFetch(url, options = {}) {
        const requestOptions = { ...options, credentials: 'include' };
        let response = await fetch(url, requestOptions);
        if (response.status === 401) {
            this.isAuthenticated = false;
            this.showLoginOverlay();
        }
        return response;
    }

    async fetchConfig() {
        try {
            const response = await this.apiFetch('/api/config');
            if (!response.ok) {
                throw new Error('設定情報の取得に失敗しました');
            }
            const config = await response.json();
            console.log('API Config Response:', config); // デバッグログ

            // Display LLM and ASR info
            // エージェントツール系の場合はllm_modelとllm_providerが同じ値なので重複を避ける
            const llmInfo = config.llm_model === config.llm_provider
                ? config.llm_provider  // エージェントツール系: プロバイダー名のみ
                : `${config.llm_model} (${config.llm_provider})`;  // API利用: モデル名 (プロバイダー)
            const asrInfo = config.asr_model !== 'unknown'
                ? `${config.asr_engine} (${config.asr_model})`
                : config.asr_engine;

            console.log('LLM Info:', llmInfo); // デバッグログ
            console.log('ASR Info:', asrInfo); // デバッグログ

            this.elements.llmModel.textContent = llmInfo;
            this.elements.asrModel.textContent = asrInfo;

            // セッションIDを保存（フィードバック送信時に使用）
            this.sessionId = config.session_id || null;
        } catch (error) {
            console.error('Failed to fetch config:', error);
        }
    }

    async fetchCharacters() {
        try {
            const response = await this.apiFetch('/api/characters');
            if (!response.ok) {
                throw new Error('キャラクター一覧の取得に失敗しました');
            }
            const data = await response.json();
            console.log('Characters:', data);

            // Clear existing options
            this.elements.characterSelect.innerHTML = '';

            // Add character options
            data.characters.forEach(character => {
                const option = document.createElement('option');
                option.value = character;
                option.textContent = character;
                if (character === data.current) {
                    option.selected = true;
                }
                this.elements.characterSelect.appendChild(option);
            });
        } catch (error) {
            console.error('Failed to fetch characters:', error);
            // Show error in dropdown
            this.elements.characterSelect.innerHTML = '<option>エラー: キャラクター取得失敗</option>';
        }
    }

    async fetchModeStatus() {
        if (!this.elements.modeButtons || !this.elements.modeButtons.length) return;
        try {
            const response = await this.apiFetch('/api/modes');
            if (!response.ok) throw new Error('モード情報の取得に失敗しました');
            this.modeInfo = await response.json();
            this.renderModeButtons();
        } catch (error) {
            console.error('Failed to fetch mode status:', error);
            if (this.elements.modeStatus) {
                this.elements.modeStatus.textContent = 'モード情報の取得に失敗しました';
            }
        }
    }

    renderModeButtons() {
        if (!this.modeInfo || !this.elements.modeButtons) return;
        const current = this.modeInfo.current_mode;
        const pending = this.modeInfo.next_mode;

        this.elements.modeButtons.forEach(button => {
            const mode = button.dataset.modeOption;
            button.classList.remove('bg-purple-600', 'text-white', 'shadow-inner', 'opacity-60');
            button.disabled = false;
            if (mode === current && !pending) {
                button.classList.add('bg-purple-600', 'text-white', 'shadow-inner');
            } else if (pending && mode === pending) {
                button.classList.add('bg-yellow-500', 'text-white');
                button.disabled = true;
            }
        });

        if (this.elements.modeStatus) {
            if (pending) {
                this.elements.modeStatus.textContent = `切替予定: ${this.getModeLabel(pending)}`;
            } else if (current) {
                this.elements.modeStatus.textContent = `稼働中: ${this.getModeLabel(current)}`;
            } else {
                this.elements.modeStatus.textContent = '';
            }
        }
    }

    getModeLabel(mode) {
        const labels = {
            terminal: 'Terminal',
            voice_chat: 'Voice Chat',
            discord: 'Discord'
        };
        return labels[mode] || mode;
    }

    handleModeSwitchClick(targetMode) {
        if (!targetMode || !this.modeInfo) return;
        if (this.restartCountdownTimer) return;
        if (targetMode === this.modeInfo.current_mode) {
            this.showNotification(`${this.getModeLabel(targetMode)}は既に稼働中です`, 'info');
            return;
        }
        this.requestModeSwitch(targetMode);
    }

    async requestModeSwitch(targetMode) {
        try {
            this.toggleModeButtons(true);
            this.showRestartOverlay(`${this.getModeLabel(targetMode)} モードに切り替えます`);
            const response = await this.apiFetch('/api/mode-switch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_mode: targetMode })
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'モード切り替えに失敗しました');
            }
            const result = await response.json();
            const delay = result.restart_delay_seconds || 3;
            this.startRestartCountdown(delay);
            this.showNotification(result.message || 'モード切り替えを開始しました', 'info');
        } catch (error) {
            console.error('Mode switch failed:', error);
            this.showNotification(error.message || 'モード切り替えに失敗しました', 'error');
            this.hideRestartOverlay();
            this.toggleModeButtons(false);
        }
    }

    toggleModeButtons(disabled) {
        if (!this.elements.modeButtons) return;
        this.elements.modeButtons.forEach(button => {
            button.disabled = disabled;
            if (disabled) {
                button.classList.add('opacity-60', 'cursor-not-allowed');
            } else {
                button.classList.remove('opacity-60', 'cursor-not-allowed');
            }
        });
    }

    showRestartOverlay(message = '') {
        if (!this.elements.modeOverlay) return;
        this.elements.modeOverlay.classList.remove('hidden');
        if (this.elements.modeOverlayMessage) {
            this.elements.modeOverlayMessage.textContent = message;
        }
    }

    hideRestartOverlay() {
        if (!this.elements.modeOverlay) return;
        this.elements.modeOverlay.classList.add('hidden');
        if (this.elements.modeOverlayCountdown) {
            this.elements.modeOverlayCountdown.textContent = '';
        }
        clearInterval(this.restartCountdownTimer);
        clearTimeout(this.restartReloadTimer);
        this.restartCountdownTimer = null;
        this.restartReloadTimer = null;
    }

    startRestartCountdown(seconds = 3) {
        if (!this.elements.modeOverlayCountdown) return;
        let remaining = Math.max(1, Math.ceil(seconds));
        this.elements.modeOverlayCountdown.textContent = `${remaining} 秒後に再接続を試みます`;
        this.restartCountdownTimer = setInterval(() => {
            remaining -= 1;
            if (remaining <= 0) {
                clearInterval(this.restartCountdownTimer);
                this.restartCountdownTimer = null;
                this.elements.modeOverlayCountdown.textContent = '再接続を待機中...';
            } else {
                this.elements.modeOverlayCountdown.textContent = `${remaining} 秒後に再接続を試みます`;
            }
        }, 1000);

        this.restartReloadTimer = setTimeout(() => {
            window.location.reload();
        }, (seconds + 2) * 1000);
    }

    async fetchMobileCommands() {
        if (!this.elements.commandButtonList) return;

        try {
            const response = await this.apiFetch('/api/mobile/commands');
            if (!response.ok) {
                throw new Error('コマンドの読み込みに失敗しました');
            }
            const data = await response.json();
            this.mobileSettings = data;
            this.mobileCommands = Array.isArray(data.commands) ? data.commands : [];
            this.renderMobileCommands();
        } catch (error) {
            console.error('Failed to fetch mobile commands:', error);
            if (this.elements.commandEmptyState) {
                this.elements.commandEmptyState.textContent = 'コマンドの読み込みに失敗しました';
            }
        }
    }

    // ── LLM Mode Management ─────────────────────────────────────────────
    async fetchLlmMode() {
        try {
            const response = await this.apiFetch('/api/llm/mode');
            if (response.ok) {
                const data = await response.json();
                this.llmMode = data.mode || 'fast';
                this.updateLlmModeUI();
            }
        } catch (error) {
            console.error('Failed to fetch LLM mode:', error);
        }
    }

    async setLlmMode(mode) {
        if (mode !== 'fast' && mode !== 'thinking') {
            console.warn('Invalid LLM mode:', mode);
            return;
        }

        try {
            const response = await this.apiFetch('/api/llm/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode })
            });

            if (response.ok) {
                const data = await response.json();
                this.llmMode = data.mode;
                this.updateLlmModeUI();
                console.log(`LLM mode set to: ${mode}`);
            }
        } catch (error) {
            console.error('Failed to set LLM mode:', error);
        }
    }

    updateLlmModeUI() {
        const fastBtn = document.getElementById('llmModeFastBtn');
        const thinkingBtn = document.getElementById('llmModeThinkingBtn');
        const indicator = document.getElementById('llmModeIndicator');

        if (fastBtn) {
            fastBtn.classList.toggle('active', this.llmMode === 'fast');
        }
        if (thinkingBtn) {
            thinkingBtn.classList.toggle('active', this.llmMode === 'thinking');
        }
        if (indicator) {
            indicator.textContent = this.llmMode === 'thinking' ? '🧠' : '⚡';
            indicator.title = this.llmMode === 'thinking' ? '思考モード' : '高速モード';
        }
    }

    renderMobileCommands() {
        if (!this.elements.commandButtonList) return;
        const container = this.elements.commandButtonList;

        // Find or create the commands container
        // Insert it before Crawler Status section
        let commandsContainer = container.querySelector('#mobileCommandsContainer');
        if (!commandsContainer) {
            // First time: create the container and insert it before Crawler Status
            commandsContainer = document.createElement('div');
            commandsContainer.id = 'mobileCommandsContainer';
            const crawlerSection = container.querySelector('#crawlerStatusSection');
            if (crawlerSection) {
                container.insertBefore(commandsContainer, crawlerSection);
            } else {
                // Fallback: append to end if crawler section not found
                container.appendChild(commandsContainer);
            }
        }

        commandsContainer.innerHTML = '';

        if (!this.mobileCommands.length) {
            const emptyState = document.createElement('p');
            emptyState.className = 'text-sm text-gray-400';
            emptyState.textContent = '利用可能なクイックコマンドが設定されていません。';
            commandsContainer.appendChild(emptyState);
            return;
        }

        const categories = this.groupCommandsByCategory(this.mobileCommands);
        categories.forEach(({ category, commands }) => {
            if (category) {
                const heading = document.createElement('p');
                heading.className = 'text-xs uppercase tracking-wide text-gray-400 mt-2';
                heading.textContent = category;
                commandsContainer.appendChild(heading);
            }

            commands.forEach((command) => {
                const button = this.createCommandButton(command);
                commandsContainer.appendChild(button);
            });
        });
    }

    groupCommandsByCategory(commands) {
        const grouped = new Map();
        commands.forEach((cmd) => {
            const category = cmd.category || '';
            if (!grouped.has(category)) {
                grouped.set(category, []);
            }
            grouped.get(category).push(cmd);
        });

        return Array.from(grouped.entries()).map(([category, list]) => ({
            category,
            commands: list
        }));
    }

    createCommandButton(command) {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.commandId = command.id;
        button.className = 'w-full p-4 rounded-2xl border border-gray-700 bg-gray-800 text-left transition-all hover:-translate-y-0.5 hover:shadow-lg flex items-center justify-between gap-4';
        this.applyCommandButtonAccent(button, command.accent);

        button.innerHTML = `
            <div>
                <p class="text-sm font-semibold text-white">${this.escapeHtml(command.label || 'コマンド')}</p>
                <p class="text-xs text-gray-200/80 mt-1">${this.escapeHtml(command.hint || '')}</p>
            </div>
            <div class="w-10 h-10 rounded-xl bg-black/10 flex items-center justify-center">
                ${this.getCommandIconSvg(command.icon)}
            </div>
        `;

        button.addEventListener('click', () => {
            if (command.requires_confirmation) {
                const confirmed = confirm(command.confirmation_text || 'このコマンドを実行しますか？');
                if (!confirmed) return;
            }
            this.triggerCommand(command.id, button, command.label);
        });

        return button;
    }

    applyCommandButtonAccent(button, accent) {
        const accentMap = {
            indigo: ['from-indigo-600/70', 'to-indigo-500/60'],
            violet: ['from-violet-600/70', 'to-fuchsia-500/50'],
            cyan: ['from-cyan-600/70', 'to-sky-500/60'],
            rose: ['from-rose-600/70', 'to-orange-500/60'],
            emerald: ['from-emerald-600/70', 'to-teal-500/60'],
            slate: ['from-slate-700/70', 'to-slate-600/50']
        };

        button.classList.add('bg-gradient-to-r');
        const classes = accentMap[accent] || accentMap['slate'];
        classes.forEach(cls => button.classList.add(cls));
    }

    getCommandIconSvg(iconKey = 'sparkles') {
        const icons = {
            sparkles: '<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M5 3v4m0 0v4m0-4h4m-4 0H1m18 6v4m0 0v4m0-4h4m-4 0h-4M9 13l2 8 2-8 2-8 2 8"/></svg>',
            status: '<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M3 10h4v11H3zM10 3h4v18h-4zM17 14h4v7h-4z"/></svg>',
            user: '<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 14c-4.418 0-8 2.686-8 6v1h16v-1c0-3.314-3.582-6-8-6zm0-2a4 4 0 100-8 4 4 0 000 8z"/></svg>',
            trash: '<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M9 7V4h6v3m-9 0h12"/></svg>',
            notebook: '<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M7 4h10a2 2 0 012 2v14l-5-3-5 3V6a2 2 0 00-2-2z"/></svg>'
        };
        return icons[iconKey] || icons['sparkles'];
    }

    async triggerCommand(commandId, buttonEl, label = '') {
        if (!commandId || !buttonEl) return;
        buttonEl.disabled = true;
        buttonEl.classList.add('opacity-60');

        try {
            const response = await this.apiFetch('/api/mobile/commands/run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ command_id: commandId })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'コマンド実行に失敗しました');
            }

            const result = await response.json();
            const executedLabel = result.command?.label || label || 'コマンド';
            this.showNotification(`${executedLabel} を実行しました`, 'success');
            this.addCommandHistoryEntry(result.command, result.result);
        } catch (error) {
            console.error('Failed to run command:', error);
            this.showNotification(error.message || 'コマンド実行に失敗しました', 'error');
        } finally {
            buttonEl.disabled = false;
            buttonEl.classList.remove('opacity-60');
        }
    }

    addCommandHistoryEntry(command, result) {
        if (!command) return;
        const entry = {
            id: command.id,
            label: command.label,
            action: command.action,
            result,
            timestamp: new Date()
        };
        this.commandHistory.unshift(entry);
        this.commandHistory = this.commandHistory.slice(0, 5);
        this.renderCommandHistory();
    }

    renderCommandHistory() {
        if (!this.elements.commandHistoryList) return;
        const container = this.elements.commandHistoryList;
        container.innerHTML = '';

        if (!this.commandHistory.length) {
            const empty = document.createElement('p');
            empty.className = 'text-gray-500';
            empty.textContent = 'まだ実行履歴がありません';
            container.appendChild(empty);
            return;
        }

        this.commandHistory.forEach(entry => {
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between bg-gray-800/80 border border-gray-700 rounded-lg px-3 py-2';
            const timeLabel = entry.timestamp.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
            row.innerHTML = `
                <span class="text-[13px] text-gray-100">${this.escapeHtml(entry.label || entry.id)}</span>
                <span class="text-[11px] text-gray-400">${timeLabel}</span>
            `;
            container.appendChild(row);
        });
    }

    openCommandOverlay() {
        if (!this.elements.commandOverlay) return;
        this.elements.commandOverlay.classList.remove('hidden');
        this.commandOverlayVisible = true;
        // Auto-fetch settings, crawler status, documents, and user files when overlay opens
        this.fetchSettings();
        this.fetchCrawlerStatus();
        this.fetchDocuments();
        this.fetchUserFiles();
        // Load media browser config if not already loaded
        if (!this._mediaConfigLoaded) {
            this.fetchMediaConfig();
        } else if (this.mediaCurrentPath) {
            // If we have a current path, just refresh the display
            this._restoreMediaBrowserState();
        }
    }


    _restoreMediaBrowserState() {
        // Restore the cached media browser state without refetching
        this.updateMediaBreadcrumb();
        this.updateMediaNavButtons();
        this.renderMediaBookmarks();

        // Re-render cached data if available
        if (this._cachedMediaFolders && this._cachedMediaFiles) {
            this.renderMediaFolders(this._cachedMediaFolders);
            this.renderMediaFiles(this._cachedMediaFiles);
        }
    }

    closeCommandOverlay() {
        if (!this.elements.commandOverlay) return;
        this.elements.commandOverlay.classList.add('hidden');
        this.commandOverlayVisible = false;
    }

    // ── Settings Management ───────────────────────────────────────────────
    async fetchSettings() {
        try {
            const response = await this.apiFetch('/api/settings');
            if (!response.ok) {
                throw new Error('設定の取得に失敗しました');
            }
            const data = await response.json();
            this.renderSettings(data.settings);
        } catch (error) {
            console.error('Failed to fetch settings:', error);
        }
    }

    renderSettings(settings) {
        if (!settings) return;

        // External LLM Auto Approve toggle
        const externalLlmToggle = document.getElementById('toggleExternalLlmApprove');
        if (externalLlmToggle && settings.external_llm) {
            this.updateToggleButton(externalLlmToggle, settings.external_llm.auto_approve);
        }

        // RAG Enabled toggle
        const ragToggle = document.getElementById('toggleRagEnabled');
        if (ragToggle && settings.rag) {
            this.updateToggleButton(ragToggle, settings.rag.enabled);
        }

        // Reasoning Enabled toggle
        const reasoningToggle = document.getElementById('toggleReasoningEnabled');
        if (reasoningToggle && settings.reasoning) {
            this.updateToggleButton(reasoningToggle, settings.reasoning.enabled);
        }

        // Reasoning Display Mode dropdown
        const displayModeSelect = document.getElementById('reasoningDisplayMode');
        if (displayModeSelect && settings.reasoning) {
            displayModeSelect.value = settings.reasoning.display_mode || 'progress';
        }

        // Agent/Tool toggles
        if (settings.agents) {
            const filesystemToggle = document.getElementById('toggleFilesystemAgent');
            if (filesystemToggle && settings.agents.filesystem) {
                this.updateToggleButton(filesystemToggle, settings.agents.filesystem.enabled);
            }

            const clickupToggle = document.getElementById('toggleClickupAgent');
            if (clickupToggle && settings.agents.clickup) {
                this.updateToggleButton(clickupToggle, settings.agents.clickup.enabled);
            }

            const spotifyAgentToggle = document.getElementById('toggleSpotifyAgent');
            if (spotifyAgentToggle && settings.agents.spotify) {
                this.updateToggleButton(spotifyAgentToggle, settings.agents.spotify.enabled);
            }
        }

        // ClickUp Sync toggle
        const clickupSyncToggle = document.getElementById('toggleClickupSync');
        if (clickupSyncToggle && settings.clickup_sync) {
            this.updateToggleButton(clickupSyncToggle, settings.clickup_sync.enabled);
        }

        // Spotify feature toggle
        const spotifyToggle = document.getElementById('toggleSpotify');
        if (spotifyToggle && settings.spotify) {
            this.updateToggleButton(spotifyToggle, settings.spotify.enabled);
        }
    }


    updateToggleButton(button, enabled) {
        button.dataset.enabled = enabled ? 'true' : 'false';
        const knob = button.querySelector('span');

        if (enabled) {
            button.classList.remove('bg-gray-600');
            button.classList.add('bg-blue-600');
            if (knob) knob.classList.add('translate-x-6');
        } else {
            button.classList.remove('bg-blue-600');
            button.classList.add('bg-gray-600');
            if (knob) knob.classList.remove('translate-x-6');
        }
    }

    async updateSetting(key, value, persist = true) {
        try {
            const response = await this.apiFetch('/api/settings', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key, value, persist })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '設定の更新に失敗しました');
            }

            const result = await response.json();
            console.log('Setting updated:', result);
            this.showNotification(`設定を更新しました: ${key}`, 'success');
            return true;
        } catch (error) {
            console.error('Failed to update setting:', error);
            this.showNotification(error.message || '設定の更新に失敗しました', 'error');
            return false;
        }
    }

    setupSettingsEventListeners() {
        // Toggle buttons
        const toggleButtons = [
            { id: 'toggleExternalLlmApprove', key: 'external_llm.auto_approve' },
            { id: 'toggleRagEnabled', key: 'rag.enabled' },
            { id: 'toggleReasoningEnabled', key: 'reasoning.enabled' },
            // Tool/Agent toggles
            { id: 'toggleFilesystemAgent', key: 'agents.filesystem.enabled' },
            { id: 'toggleClickupAgent', key: 'agents.clickup.enabled' },
            { id: 'toggleSpotifyAgent', key: 'agents.spotify.enabled' },
            { id: 'toggleClickupSync', key: 'clickup_sync.enabled' },
            { id: 'toggleSpotify', key: 'spotify.enabled' }
        ];

        toggleButtons.forEach(({ id, key }) => {
            const button = document.getElementById(id);
            if (button) {
                button.addEventListener('click', async () => {
                    const currentEnabled = button.dataset.enabled === 'true';
                    const newEnabled = !currentEnabled;

                    // Optimistic UI update
                    this.updateToggleButton(button, newEnabled);

                    // Send to server
                    const success = await this.updateSetting(key, newEnabled);
                    if (!success) {
                        // Revert on failure
                        this.updateToggleButton(button, currentEnabled);
                    }
                });
            }
        });

        // Reasoning Display Mode dropdown
        const displayModeSelect = document.getElementById('reasoningDisplayMode');
        if (displayModeSelect) {
            displayModeSelect.addEventListener('change', (e) => {
                this.updateSetting('reasoning.display_mode', e.target.value);
            });
        }

        // Settings refresh button
        const refreshBtn = document.getElementById('settingsRefreshBtn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => this.fetchSettings());
        }

    }


    async fetchCrawlerStatus() {
        if (!this.elements.crawlerStatusList) return;

        // Show loading state
        this.elements.crawlerStatusList.innerHTML = '<p class="text-xs text-gray-500">ステータスを取得中...</p>';

        try {
            const response = await this.apiFetch('/api/crawler/status');
            if (!response.ok) {
                throw new Error('ステータスの取得に失敗しました');
            }
            const data = await response.json();
            this.renderCrawlerStatus(data.crawlers || []);
        } catch (error) {
            console.error('Failed to fetch crawler status:', error);
            this.elements.crawlerStatusList.innerHTML = '<p class="text-xs text-red-400">ステータスの取得に失敗しました</p>';
        }
    }

    renderCrawlerStatus(crawlers) {
        if (!this.elements.crawlerStatusList) return;
        const container = this.elements.crawlerStatusList;
        container.innerHTML = '';

        if (!crawlers.length) {
            container.innerHTML = '<p class="text-xs text-gray-500">クローラー情報なし</p>';
            return;
        }

        crawlers.forEach(crawler => {
            const card = document.createElement('div');
            card.className = 'flex items-center justify-between bg-gray-800/60 border border-gray-700 rounded-xl px-4 py-3';

            // Status indicator colors
            const statusColors = {
                running: 'bg-green-500',
                idle: 'bg-yellow-400',
                stopped: 'bg-gray-500',
                sleeping: 'bg-purple-400',
                error: 'bg-red-500',
                timeout: 'bg-orange-500',
                unreachable: 'bg-red-400',
                building: 'bg-blue-400',
                paused: 'bg-gray-400',
                not_found: 'bg-gray-600',
                not_configured: 'bg-yellow-600',
                unknown: 'bg-gray-500'
            };

            const statusLabels = {
                running: '稼働中',
                idle: '待機中',
                stopped: '停止',
                sleeping: 'スリープ',
                error: 'エラー',
                timeout: 'タイムアウト',
                unreachable: '接続不可',
                building: 'ビルド中',
                paused: '一時停止',
                not_found: '未検出',
                not_configured: '未設定',
                unknown: '不明'
            };

            const dotColor = statusColors[crawler.status] || 'bg-gray-500';
            const statusLabel = statusLabels[crawler.status] || crawler.status;

            // Details text
            let detailText = '';
            if (crawler.details) {
                // DiscordCrawler details
                if (crawler.details.processed_servers !== undefined || crawler.details.processed_channels !== undefined) {
                    const servers = crawler.details.processed_servers || 0;
                    const channels = crawler.details.processed_channels || 0;
                    detailText = `${servers}サーバー / ${channels}チャンネル`;
                    if (crawler.details.current_server) {
                        detailText += ` (${crawler.details.current_server})`;
                    }
                }
                // EventMonitor details
                else if (crawler.details.processed_accounts !== undefined || crawler.details.new_tweets !== undefined) {
                    const accounts = crawler.details.processed_accounts || 0;
                    const tweets = crawler.details.new_tweets || 0;
                    const events = crawler.details.event_tweets || 0;
                    const totalTargets = Number(crawler.details.total_targets || 0);
                    const completedTargets = Number(crawler.details.completed_targets || 0);
                    const progressPercent = Number(crawler.details.progress_percent);

                    if (totalTargets > 0 && Number.isFinite(progressPercent)) {
                        detailText = `${progressPercent.toFixed(1)}% (${completedTargets}/${totalTargets})`;
                        detailText += ` | ${accounts}アカウント / ${tweets}件`;
                    } else {
                        detailText = `${accounts}アカウント / ${tweets}件`;
                    }
                    if (events > 0) {
                        detailText += ` (イベント: ${events})`;
                    }
                    if (crawler.details.current_account) {
                        detailText += ` @${crawler.details.current_account}`;
                    }
                }
                // VideoCrawler / other details
                else if (crawler.details.enabled_servers !== undefined) {
                    detailText = `${crawler.details.enabled_servers} servers`;
                } else if (crawler.details.log_age_minutes !== undefined) {
                    detailText = `${Math.round(crawler.details.log_age_minutes)}分前`;
                } else if (crawler.details.hardware) {
                    // hardware is already shown, but add more Push fields if available
                    detailText = crawler.details.hardware;
                    if (crawler.details.total_videos !== undefined) {
                        detailText += ` | ${crawler.details.total_videos}件`;
                    }
                    if (crawler.details.queue_size !== undefined && crawler.details.queue_size > 0) {
                        detailText += ` (待機: ${crawler.details.queue_size})`;
                    }
                } else if (crawler.details.total_videos !== undefined) {
                    // VideoCrawler Push data without hardware (cloud mode)
                    detailText = `${crawler.details.total_videos}件処理`;
                    if (crawler.details.queue_size !== undefined && crawler.details.queue_size > 0) {
                        detailText += ` (待機: ${crawler.details.queue_size})`;
                    }
                    if (crawler.details.models_per_hour !== undefined) {
                        detailText += ` | ${Math.round(crawler.details.models_per_hour)}/h`;
                    }
                } else if (crawler.details.stats) {
                    // Hydrus Client stats
                    const stats = crawler.details.stats;
                    detailText = `${stats.total_files.toLocaleString()}ファイル (${stats.size_total_gb}GB)`;
                    if (crawler.details.active_jobs > 0) {
                        detailText += ` | ${crawler.details.active_jobs}件ダウンロード中`;
                    }
                } else if (crawler.details.hydrus_version) {
                    detailText = `v${crawler.details.hydrus_version}`;
                }
            }
            if (crawler.error && !detailText) {
                detailText = crawler.error.substring(0, 40) + (crawler.error.length > 40 ? '...' : '');
            }

            // Type badge
            const typeBadge = crawler.type === 'cloud'
                ? '<span class="text-[10px] bg-cyan-900/50 text-cyan-300 px-1.5 py-0.5 rounded">Cloud</span>'
                : '<span class="text-[10px] bg-gray-700 text-gray-400 px-1.5 py-0.5 rounded">Local</span>';

            // Simple button logic: Start when stopped, Stop when running
            let actionBtn = '';

            // Local crawlers (DiscordCrawler, EventMonitor, HydrusClient)
            if (crawler.name === 'DiscordCrawler' || crawler.name === 'EventMonitor' || crawler.name === 'HydrusClient') {
                if (crawler.status === 'stopped') {
                    actionBtn = `<button class="text-xs bg-green-600 hover:bg-green-500 text-white px-3 py-1 rounded restart-btn" data-crawler="${this.escapeHtml(crawler.name)}">起動</button>`;
                } else if (crawler.status === 'running') {
                    actionBtn = `<button class="text-xs bg-red-600 hover:bg-red-500 text-white px-3 py-1 rounded stop-btn" data-crawler="${this.escapeHtml(crawler.name)}">停止</button>`;
                }
            }
            // VideoCrawler: can restart if paused or sleeping
            else if (crawler.can_restart) {
                actionBtn = `<button class="text-xs bg-cyan-600 hover:bg-cyan-500 text-white px-3 py-1 rounded restart-btn" data-crawler="${this.escapeHtml(crawler.name)}">再起動</button>`;
            }

            card.innerHTML = `
                <div class="flex items-center justify-between w-full">
                    <div class="flex items-center gap-3">
                        <span class="w-2 h-2 rounded-full ${dotColor}"></span>
                        <div>
                            <div class="flex items-center gap-2">
                                <span class="text-sm font-medium text-white">${this.escapeHtml(crawler.name)}</span>
                                ${typeBadge}
                            </div>
                            ${detailText ? `<p class="text-xs text-gray-400 mt-0.5">${this.escapeHtml(detailText)}</p>` : ''}
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        ${actionBtn}
                        <span class="text-xs font-medium ${crawler.status === 'running' ? 'text-green-400' : 'text-gray-400'} min-w-[60px] text-right">${statusLabel}</span>
                    </div>
                </div>
            `;

            // Add click handlers for buttons
            const restartButton = card.querySelector('.restart-btn');
            const stopButton = card.querySelector('.stop-btn');

            if (restartButton) {
                restartButton.addEventListener('click', async (e) => {
                    e.preventDefault();
                    const crawlerName = restartButton.dataset.crawler;
                    const originalText = restartButton.textContent;
                    restartButton.disabled = true;
                    restartButton.textContent = '起動中...';

                    try {
                        const response = await this.apiFetch(`/api/crawler/restart/${crawlerName}`, {
                            method: 'POST'
                        });
                        const result = await response.json();

                        if (result.success) {
                            this.showNotification(result.message, 'success');
                            // Refresh status after a short delay
                            setTimeout(() => this.fetchCrawlerStatus(), 3000);
                        } else {
                            this.showNotification(result.message || '再起動に失敗しました', 'error');
                            if (result.url) {
                                setTimeout(() => {
                                    window.open(result.url, '_blank');
                                }, 500);
                            }
                        }
                    } catch (error) {
                        console.error('Restart failed:', error);
                        this.showNotification('再起動リクエストに失敗しました', 'error');
                    } finally {
                        restartButton.disabled = false;
                        restartButton.textContent = originalText;
                    }
                });
            }

            if (stopButton) {
                stopButton.addEventListener('click', async (e) => {
                    e.preventDefault();
                    const crawlerName = stopButton.dataset.crawler;
                    stopButton.disabled = true;
                    stopButton.textContent = '...';

                    try {
                        const response = await this.apiFetch(`/api/crawler/stop/${crawlerName}`, {
                            method: 'POST'
                        });
                        const result = await response.json();

                        if (result.success) {
                            this.showNotification(result.message, 'success');
                            // Refresh status after a short delay
                            setTimeout(() => this.fetchCrawlerStatus(), 2000);
                        } else {
                            this.showNotification(result.message || '停止に失敗しました', 'error');
                        }
                    } catch (error) {
                        console.error('Stop failed:', error);
                        this.showNotification('停止リクエストに失敗しました', 'error');
                    } finally {
                        stopButton.disabled = false;
                        stopButton.textContent = '×';
                    }
                });
            }

            container.appendChild(card);
        });
    }

    // ── Document Management ──────────────────────────────────────────────
    async fetchDocuments() {
        if (!this.elements.documentsList) return;

        this.elements.documentsList.innerHTML = '<p class="text-xs text-gray-500">読み込み中...</p>';

        try {
            const response = await this.apiFetch('/api/documents');
            if (!response.ok) {
                throw new Error('ドキュメントの取得に失敗しました');
            }
            const data = await response.json();
            this.documents = data.documents || [];
            this.renderDocuments();
        } catch (error) {
            console.error('Failed to fetch documents:', error);
            this.elements.documentsList.innerHTML = '<p class="text-xs text-red-400">読み込みに失敗しました</p>';
        }
    }

    renderDocuments() {
        if (!this.elements.documentsList) return;
        const container = this.elements.documentsList;
        container.innerHTML = '';

        if (!this.documents.length) {
            container.innerHTML = '<p class="text-xs text-gray-500">ドキュメントがありません</p>';
            return;
        }

        this.documents.forEach(doc => {
            const card = document.createElement('div');
            card.className = 'flex items-center justify-between bg-gray-800/60 border border-gray-700 rounded-xl px-4 py-3 cursor-pointer hover:bg-gray-700/60 transition';
            card.dataset.documentName = doc.name;

            const modifiedDate = new Date(doc.modified_at);
            const timeStr = modifiedDate.toLocaleDateString('ja-JP', { month: 'short', day: 'numeric' });

            card.innerHTML = `
                <div class="flex items-center gap-3">
                    <svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <span class="text-sm font-medium text-white">${this.escapeHtml(doc.name)}</span>
                </div>
                <span class="text-xs text-gray-400">${timeStr}</span>
            `;

            card.addEventListener('click', () => this.openDocumentEditor(doc.name));
            container.appendChild(card);
        });
    }

    openDocumentEditor(name = null) {
        if (!this.elements.documentEditorModal) return;

        this.currentDocumentName = name;

        // Reset fields
        this.elements.documentEditorName.value = name || '';
        this.elements.documentEditorContent.value = '';
        this.elements.documentEditorName.disabled = !!name; // Disable name editing for existing

        // Show/hide delete button
        if (this.elements.documentEditorDelete) {
            this.elements.documentEditorDelete.classList.toggle('hidden', !name);
        }

        // Load content if editing existing document
        if (name) {
            this.loadDocumentContent(name);
        }

        this.elements.documentEditorModal.classList.remove('hidden');
    }

    closeDocumentEditor() {
        if (!this.elements.documentEditorModal) return;
        this.elements.documentEditorModal.classList.add('hidden');
        this.currentDocumentName = null;
    }

    async loadDocumentContent(name) {
        try {
            const response = await this.apiFetch(`/api/documents/${encodeURIComponent(name)}`);
            if (!response.ok) {
                throw new Error('ドキュメントの読み込みに失敗しました');
            }
            const data = await response.json();
            this.elements.documentEditorContent.value = data.content || '';
        } catch (error) {
            console.error('Failed to load document:', error);
            this.showNotification('ドキュメントの読み込みに失敗しました', 'error');
        }
    }

    async saveDocument() {
        const name = this.elements.documentEditorName.value.trim();
        const content = this.elements.documentEditorContent.value;

        if (!name) {
            this.showNotification('ドキュメント名を入力してください', 'error');
            return;
        }

        try {
            const response = await this.apiFetch(`/api/documents/${encodeURIComponent(name)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '保存に失敗しました');
            }

            const result = await response.json();
            this.showNotification(result.message || '保存しました', 'success');
            this.closeDocumentEditor();
            this.fetchDocuments();
        } catch (error) {
            console.error('Failed to save document:', error);
            this.showNotification(error.message || '保存に失敗しました', 'error');
        }
    }

    async deleteDocument() {
        const name = this.currentDocumentName;
        if (!name) return;

        if (!confirm(`「${name}」を削除しますか？`)) {
            return;
        }

        try {
            const response = await this.apiFetch(`/api/documents/${encodeURIComponent(name)}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '削除に失敗しました');
            }

            const result = await response.json();
            this.showNotification(result.message || '削除しました', 'success');
            this.closeDocumentEditor();
            this.fetchDocuments();
        } catch (error) {
            console.error('Failed to delete document:', error);
            this.showNotification(error.message || '削除に失敗しました', 'error');
        }
    }

    // ── Office File Upload ──────────────────────────────────────────────────
    async uploadOfficeFile(file) {
        if (!file) return;

        const validExtensions = ['.docx', '.xlsx', '.pptx', '.pdf'];
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        if (!validExtensions.includes(ext)) {
            this.showNotification(`対応していないファイル形式です: ${ext}`, 'error');
            return;
        }

        try {
            this.showNotification('ファイルを変換中...', 'info');

            const formData = new FormData();
            formData.append('file', file);

            const response = await this.apiFetch('/api/documents/upload', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'ファイルの変換に失敗しました');
            }

            const result = await response.json();
            this.showOfficeUploadResult(result);
            this.showNotification(`${result.filename} を変換しました`, 'success');
        } catch (error) {
            console.error('Failed to upload office file:', error);
            this.showNotification(error.message || 'アップロードに失敗しました', 'error');
        }
    }

    showOfficeUploadResult(result) {
        if (!this.elements.officeUploadResult) return;

        this.lastOfficeUploadResult = result;
        this.elements.officeUploadFileName.textContent = result.filename || '-';
        this.elements.officeUploadContent.textContent = result.content || '';
        this.elements.officeUploadResult.classList.remove('hidden');
    }

    hideOfficeUploadResult() {
        if (!this.elements.officeUploadResult) return;
        this.elements.officeUploadResult.classList.add('hidden');
        this.lastOfficeUploadResult = null;
    }

    insertOfficeContentToChat() {
        if (!this.lastOfficeUploadResult || !this.lastOfficeUploadResult.content) return;

        const content = this.lastOfficeUploadResult.content;
        const filename = this.lastOfficeUploadResult.filename || 'document';

        // Insert into message input
        const prefix = `【${filename}の内容】\n\n`;
        this.elements.messageInput.value = prefix + content;
        this.updateCharCount();
        this.adjustTextareaHeight();
        this.elements.messageInput.focus();

        this.hideOfficeUploadResult();
        this.showNotification('チャットに挿入しました', 'success');
    }

    async saveOfficeContentAsDocument() {
        if (!this.lastOfficeUploadResult || !this.lastOfficeUploadResult.content) return;

        const filename = this.lastOfficeUploadResult.filename || 'document';
        const baseName = filename.replace(/\.[^/.]+$/, '');  // Remove extension
        const content = this.lastOfficeUploadResult.content;

        try {
            const response = await this.apiFetch(`/api/documents/${encodeURIComponent(baseName)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '保存に失敗しました');
            }

            const result = await response.json();
            this.showNotification(result.message || 'ドキュメントとして保存しました', 'success');
            this.hideOfficeUploadResult();
            this.fetchDocuments();
        } catch (error) {
            console.error('Failed to save office content as document:', error);
            this.showNotification(error.message || '保存に失敗しました', 'error');
        }
    }

    // ── User Files Management ──────────────────────────────────────────────
    async fetchUserFiles() {
        if (!this.elements.userFilesList) return;

        this.elements.userFilesList.innerHTML = '<p class="text-xs text-gray-500">読み込み中...</p>';

        try {
            const response = await this.apiFetch('/api/user-files');
            if (!response.ok) {
                throw new Error('ファイルの取得に失敗しました');
            }
            const data = await response.json();
            this.userFiles = data.files || [];
            this.renderUserFiles();
        } catch (error) {
            console.error('Failed to fetch user files:', error);
            this.elements.userFilesList.innerHTML = '<p class="text-xs text-red-400">読み込みに失敗しました</p>';
        }
    }

    renderUserFiles() {
        if (!this.elements.userFilesList) return;
        const container = this.elements.userFilesList;
        container.innerHTML = '';

        if (!this.userFiles || !this.userFiles.length) {
            container.innerHTML = '<p class="text-xs text-gray-500">ファイルがありません</p>';
            return;
        }

        this.userFiles.forEach(file => {
            const card = document.createElement('div');
            card.className = 'flex items-center justify-between bg-gray-800/60 border border-gray-700 rounded-xl px-4 py-3 hover:bg-gray-700/60 transition';
            card.dataset.filename = file.filename;

            const modifiedDate = new Date(file.modified_at);
            const timeStr = modifiedDate.toLocaleDateString('ja-JP', { month: 'short', day: 'numeric' });

            card.innerHTML = `
                <div class="flex items-center gap-3 flex-1 min-w-0">
                    <svg class="w-4 h-4 text-teal-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
                    </svg>
                    <div class="flex-1 min-w-0">
                        <span class="text-sm font-medium text-white truncate block">${this.escapeHtml(file.filename)}</span>
                        <span class="text-xs text-gray-500">${file.size_display || ''}</span>
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0">
                    <span class="text-xs text-gray-400">${timeStr}</span>
                    <button class="user-file-download p-1 text-gray-400 hover:text-teal-300 transition" title="ダウンロード">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                    </button>
                    <button class="user-file-delete p-1 text-gray-400 hover:text-red-400 transition" title="削除">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                    </button>
                </div>
            `;

            // Download button handler
            const downloadBtn = card.querySelector('.user-file-download');
            downloadBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.downloadUserFile(file.filename);
            });

            // Delete button handler
            const deleteBtn = card.querySelector('.user-file-delete');
            deleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.deleteUserFile(file.filename);
            });

            container.appendChild(card);
        });
    }

    async uploadUserFile(file) {
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await this.apiFetch('/api/user-files/upload', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'アップロードに失敗しました');
            }

            const result = await response.json();
            this.showNotification(`${file.name} をアップロードしました`, 'success');
            this.fetchUserFiles();
        } catch (error) {
            console.error('Failed to upload user file:', error);
            this.showNotification(error.message || 'アップロードに失敗しました', 'error');
        }
    }

    downloadUserFile(filename) {
        // Create a temporary anchor to trigger download
        const url = `/api/user-files/${encodeURIComponent(filename)}`;
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    async deleteUserFile(filename) {
        if (!confirm(`「${filename}」を削除しますか？`)) return;

        try {
            const response = await this.apiFetch(`/api/user-files/${encodeURIComponent(filename)}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '削除に失敗しました');
            }

            this.showNotification(`${filename} を削除しました`, 'success');
            this.fetchUserFiles();
        } catch (error) {
            console.error('Failed to delete user file:', error);
            this.showNotification(error.message || '削除に失敗しました', 'error');
        }
    }

    // ── Media Browser ──────────────────────────────────────────────────────
    async fetchMediaConfig() {
        if (!this.elements.mediaFolderList) return;

        this.elements.mediaFolderList.innerHTML = '<p class="text-xs text-gray-500">読み込み中...</p>';
        this.elements.mediaFileGrid.innerHTML = '';

        try {
            const response = await this.apiFetch('/api/media/config');
            if (!response.ok) {
                throw new Error('メディア設定の取得に失敗しました');
            }
            const data = await response.json();

            if (!data.configured) {
                this.elements.mediaFolderList.innerHTML = '<p class="text-xs text-gray-500">メディアブラウザは設定されていません</p>';
                return;
            }

            this._mediaConfigLoaded = true;
            this.mediaRootPath = data.root_path;
            this.mediaBookmarks = data.bookmarks || [];

            // Render bookmarks
            this.renderMediaBookmarks();

            // Try to restore last browsed path from localStorage
            const lastPath = localStorage.getItem('aoitalk_media_last_path');
            const initialPath = lastPath || this.mediaRootPath;

            // Browse initial path
            await this.browseMediaPath(initialPath);
        } catch (error) {
            console.error('Failed to fetch media config:', error);
            this.elements.mediaFolderList.innerHTML = '<p class="text-xs text-red-400">メディアブラウザは設定されていません</p>';
        }
    }

    renderMediaBookmarks() {
        if (!this.elements.mediaBookmarks) return;
        const container = this.elements.mediaBookmarks;
        container.innerHTML = '';

        if (!this.mediaBookmarks.length) {
            return; // Don't show anything if no bookmarks
        }

        this.mediaBookmarks.forEach(bookmark => {
            const chip = document.createElement('button');
            chip.className = 'flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 hover:bg-emerald-900/50 border border-gray-700 hover:border-emerald-600 rounded-full text-xs text-gray-300 hover:text-emerald-300 transition group';
            chip.title = bookmark.path;

            chip.innerHTML = `
                <span>${bookmark.icon || '📁'}</span>
                <span>${this.escapeHtml(bookmark.name)}</span>
                <span class="hidden group-hover:inline text-gray-500 hover:text-red-400 ml-1" data-remove-bookmark="${this.escapeHtml(bookmark.path)}">×</span>
            `;

            // Click on chip to navigate
            chip.addEventListener('click', (e) => {
                // Check if remove button clicked
                if (e.target.hasAttribute('data-remove-bookmark')) {
                    e.stopPropagation();
                    this.removeMediaBookmark(bookmark.path);
                    return;
                }
                this.browseMediaPath(bookmark.path);
            });

            container.appendChild(chip);
        });
    }

    async browseMediaPath(path) {
        if (!this.elements.mediaFolderList) return;

        // Save history before navigating (only if we have a current path)
        if (this.mediaCurrentPath && this.mediaCurrentPath !== path) {
            this.mediaPathHistory.push(this.mediaCurrentPath);
        }

        this.elements.mediaFolderList.innerHTML = '<p class="text-xs text-gray-500">読み込み中...</p>';
        this.elements.mediaFileGrid.innerHTML = '';

        try {
            const encodedPath = encodeURIComponent(path);
            const response = await this.apiFetch(`/api/media/browse?path=${encodedPath}`);
            if (!response.ok) {
                throw new Error('フォルダの読み込みに失敗しました');
            }
            const data = await response.json();

            // Update state
            this.mediaCurrentPath = data.current_path || path;
            this.mediaCanGoUp = data.can_go_up || false;
            this.mediaIsBookmarked = data.is_bookmarked || false;

            // Save current path to localStorage for next session
            try {
                localStorage.setItem('aoitalk_media_last_path', this.mediaCurrentPath);
            } catch (e) {
                // localStorage may be unavailable in some contexts
            }

            // Cache the current folder data
            this._cachedMediaFolders = data.folders || [];
            this._cachedMediaFiles = data.files || [];

            // Update UI
            this.updateMediaBreadcrumb();
            this.updateMediaNavButtons();
            this.renderMediaFolders(this._cachedMediaFolders);
            this.renderMediaFiles(this._cachedMediaFiles);
        } catch (error) {
            console.error('Failed to browse media folder:', error);
            this.elements.mediaFolderList.innerHTML = '<p class="text-xs text-red-400">フォルダの読み込みに失敗しました</p>';
        }
    }

    setMediaViewMode(mode) {
        if (mode !== 'thumbnail' && mode !== 'list') return;
        if (this.mediaViewMode === mode) return;

        this.mediaViewMode = mode;

        // Update toggle button styling
        if (this.elements.mediaViewModeThumbnail && this.elements.mediaViewModeList) {
            if (mode === 'thumbnail') {
                this.elements.mediaViewModeThumbnail.className = 'text-xs px-2 py-1 rounded-md text-emerald-300 bg-emerald-900/50 transition flex items-center gap-1';
                this.elements.mediaViewModeList.className = 'text-xs px-2 py-1 rounded-md text-gray-400 hover:text-emerald-300 transition flex items-center gap-1';
            } else {
                this.elements.mediaViewModeThumbnail.className = 'text-xs px-2 py-1 rounded-md text-gray-400 hover:text-emerald-300 transition flex items-center gap-1';
                this.elements.mediaViewModeList.className = 'text-xs px-2 py-1 rounded-md text-emerald-300 bg-emerald-900/50 transition flex items-center gap-1';
            }
        }

        // Update file grid container class based on view mode
        if (this.elements.mediaFileGrid) {
            if (mode === 'thumbnail') {
                this.elements.mediaFileGrid.className = 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2';
            } else {
                this.elements.mediaFileGrid.className = 'grid gap-2';
            }
        }

        // Re-render with cached data if available
        if (this._cachedMediaFolders) {
            this.renderMediaFolders(this._cachedMediaFolders);
        }
        if (this._cachedMediaFiles) {
            this.renderMediaFiles(this._cachedMediaFiles);
        }
    }

    renderMediaFolders(folders) {
        if (!this.elements.mediaFolderList) return;
        const container = this.elements.mediaFolderList;
        container.innerHTML = '';

        if (!folders.length && !this._cachedMediaFiles?.length) {
            container.innerHTML = '<p class="text-xs text-gray-500">ファイルが見つかりません</p>';
            return;
        }

        // Update container class based on view mode
        if (this.mediaViewMode === 'thumbnail') {
            container.className = 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2 mb-3';
        } else {
            container.className = 'grid gap-2 mb-3';
        }

        folders.forEach(folder => {
            const card = document.createElement('div');
            const hasThumbnail = !!folder.thumbnail;

            if (this.mediaViewMode === 'thumbnail') {
                // Thumbnail view - square grid with image background
                card.className = 'relative aspect-square bg-gray-800/60 border border-gray-700 rounded-lg overflow-hidden cursor-pointer hover:border-emerald-500 transition group';

                if (hasThumbnail) {
                    // Show thumbnail image with folder overlay
                    card.innerHTML = `
                        <img src="/api/media/file?path=${encodeURIComponent(folder.thumbnail)}" 
                             alt="${this.escapeHtml(folder.name)}"
                             class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
                             loading="lazy"
                             onerror="this.parentElement.innerHTML='<div class=\\'flex flex-col items-center justify-center h-full p-2\\'><svg class=\\'w-10 h-10 text-emerald-400 mb-2\\' fill=\\'none\\' stroke=\\'currentColor\\' viewBox=\\'0 0 24 24\\'><path stroke-linecap=\\'round\\' stroke-linejoin=\\'round\\' stroke-width=\\'2\\' d=\\'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z\\'/></svg><span class=\\'text-xs text-white text-center truncate w-full\\'>${this.escapeHtml(folder.name)}</span><span class=\\'text-[10px] text-gray-500\\'>${folder.item_count} items</span></div>'" />
                        <div class="absolute top-1 left-1 bg-black/60 rounded p-1">
                            <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                            </svg>
                        </div>
                        <div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent p-2">
                            <p class="text-xs text-white truncate">${this.escapeHtml(folder.name)}</p>
                            <p class="text-[10px] text-gray-400">${folder.item_count} items</p>
                        </div>
                    `;
                } else {
                    // No thumbnail - show folder icon
                    card.className = 'relative aspect-square bg-gray-800/60 border border-gray-700 rounded-lg overflow-hidden cursor-pointer hover:bg-emerald-900/30 hover:border-emerald-700 transition group flex flex-col items-center justify-center p-2';
                    card.innerHTML = `
                        <svg class="w-10 h-10 text-emerald-400 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                        </svg>
                        <span class="text-xs text-white text-center truncate w-full">${this.escapeHtml(folder.name)}</span>
                        <span class="text-[10px] text-gray-500">${folder.item_count} items</span>
                    `;
                }
            } else {
                // List view - horizontal row with small thumbnail
                card.className = 'flex items-center gap-3 bg-gray-800/60 border border-gray-700 rounded-xl px-3 py-2 cursor-pointer hover:bg-emerald-900/30 hover:border-emerald-700 transition';

                if (hasThumbnail) {
                    card.innerHTML = `
                        <div class="w-12 h-12 flex-shrink-0 rounded-lg overflow-hidden bg-gray-900 relative">
                            <img src="/api/media/file?path=${encodeURIComponent(folder.thumbnail)}" 
                                 alt="${this.escapeHtml(folder.name)}"
                                 class="w-full h-full object-cover"
                                 loading="lazy"
                                 onerror="this.parentElement.innerHTML='<div class=\\'flex items-center justify-center h-full\\'><svg class=\\'w-6 h-6 text-emerald-400\\' fill=\\'none\\' stroke=\\'currentColor\\' viewBox=\\'0 0 24 24\\'><path stroke-linecap=\\'round\\' stroke-linejoin=\\'round\\' stroke-width=\\'2\\' d=\\'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z\\'/></svg></div>'" />
                            <div class="absolute bottom-0 right-0 bg-black/60 rounded-tl p-0.5">
                                <svg class="w-3 h-3 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                                </svg>
                            </div>
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm text-white truncate">${this.escapeHtml(folder.name)}</p>
                            <p class="text-xs text-gray-500">${folder.item_count} items</p>
                        </div>
                        <svg class="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                        </svg>
                    `;
                } else {
                    card.innerHTML = `
                        <div class="w-12 h-12 flex-shrink-0 rounded-lg bg-gray-900 flex items-center justify-center">
                            <svg class="w-6 h-6 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                            </svg>
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm text-white truncate">${this.escapeHtml(folder.name)}</p>
                            <p class="text-xs text-gray-500">${folder.item_count} items</p>
                        </div>
                        <svg class="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                        </svg>
                    `;
                }
            }

            card.addEventListener('click', () => this.browseMediaPath(folder.path));
            container.appendChild(card);
        });
    }

    renderMediaFiles(files) {
        if (!this.elements.mediaFileGrid) return;
        const container = this.elements.mediaFileGrid;
        container.innerHTML = '';

        // Update container class based on view mode
        if (this.mediaViewMode === 'thumbnail') {
            container.className = 'grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-2';
        } else {
            container.className = 'grid gap-2';
        }

        const fileList = files;

        files.forEach((file, index) => {
            const card = document.createElement('div');

            // Shortcut indicator
            const shortcutBadge = file.is_shortcut
                ? '<span class="absolute top-1 right-1 text-xs bg-black/60 px-1 rounded" title="ショートカット">🔗</span>'
                : '';

            const shortcutBadgeList = file.is_shortcut
                ? '<span class="text-xs ml-1" title="ショートカット">🔗</span>'
                : '';

            if (this.mediaViewMode === 'thumbnail') {
                // Thumbnail view - square grid
                card.className = 'relative aspect-square bg-gray-900 border border-gray-700 rounded-lg overflow-hidden cursor-pointer hover:border-emerald-500 transition group';

                if (file.type === 'image') {
                    // Image thumbnail - use absolute path
                    card.innerHTML = `
                        ${shortcutBadge}
                        <img src="/api/media/file?path=${encodeURIComponent(file.path)}" 
                             alt="${this.escapeHtml(file.name)}"
                             class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
                             loading="lazy"
                             onerror="this.parentElement.innerHTML='<div class=\\'flex items-center justify-center h-full text-2xl\\'>🖼️</div>'" />
                        <div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent p-2 opacity-0 group-hover:opacity-100 transition">
                            <p class="text-xs text-white truncate">${this.escapeHtml(file.name)}</p>
                        </div>
                    `;
                } else if (file.type === 'video') {
                    // Video with thumbnail
                    card.innerHTML = `
                        ${shortcutBadge}
                        <img src="/api/media/video-thumbnail?path=${encodeURIComponent(file.path)}" 
                             alt="${this.escapeHtml(file.name)}"
                             class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
                             loading="lazy"
                             onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" />
                        <div class="hidden flex-col items-center justify-center h-full text-gray-400 group-hover:text-emerald-400 transition absolute inset-0">
                            <svg class="w-8 h-8 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            <span class="text-xs uppercase">${file.extension.slice(1)}</span>
                        </div>
                        <div class="absolute top-1 left-1 bg-black/60 rounded p-1">
                            <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                        </div>
                        <div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent p-2">
                            <p class="text-xs text-white truncate">${this.escapeHtml(file.name)}</p>
                        </div>
                    `;
                } else if (file.type === 'audio') {
                    // Audio file thumbnail
                    card.innerHTML = `
                        ${shortcutBadge}
                        <div class="flex flex-col items-center justify-center h-full bg-gradient-to-br from-emerald-700/50 to-teal-900/50 text-emerald-400 group-hover:text-emerald-300 transition">
                            <svg class="w-8 h-8 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
                            </svg>
                            <span class="text-xs uppercase">${file.extension.slice(1)}</span>
                        </div>
                        <div class="absolute top-1 left-1 bg-emerald-600/80 rounded p-1">
                            <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
                            </svg>
                        </div>
                        <div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent p-2">
                            <p class="text-xs text-white truncate">${this.escapeHtml(file.name)}</p>
                        </div>
                    `;
                }
            } else {
                // List view - horizontal row with small thumbnail
                card.className = 'flex items-center gap-3 bg-gray-800/60 border border-gray-700 rounded-xl px-3 py-2 cursor-pointer hover:bg-emerald-900/30 hover:border-emerald-700 transition';

                if (file.type === 'image') {
                    card.innerHTML = `
                        <div class="w-12 h-12 flex-shrink-0 rounded-lg overflow-hidden bg-gray-900">
                            <img src="/api/media/file?path=${encodeURIComponent(file.path)}" 
                                 alt="${this.escapeHtml(file.name)}"
                                 class="w-full h-full object-cover"
                                 loading="lazy"
                                 onerror="this.parentElement.innerHTML='<div class=\\'flex items-center justify-center h-full text-lg\\'>🖼️</div>'" />
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm text-white truncate">${this.escapeHtml(file.name)}${shortcutBadgeList}</p>
                            <p class="text-xs text-gray-500">${file.extension.slice(1).toUpperCase()} • ${this._formatFileSize(file.size)}</p>
                        </div>
                        <svg class="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                        </svg>
                    `;
                } else if (file.type === 'video') {
                    card.innerHTML = `
                        <div class="w-12 h-12 flex-shrink-0 rounded-lg overflow-hidden bg-gray-900 relative">
                            <img src="/api/media/video-thumbnail?path=${encodeURIComponent(file.path)}" 
                                 alt="${this.escapeHtml(file.name)}"
                                 class="w-full h-full object-cover"
                                 loading="lazy"
                                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" />
                            <div class="hidden items-center justify-center h-full text-gray-400 absolute inset-0">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                </svg>
                            </div>
                            <div class="absolute bottom-0 right-0 bg-black/60 rounded-tl p-0.5">
                                <svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                </svg>
                            </div>
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm text-white truncate">${this.escapeHtml(file.name)}${shortcutBadgeList}</p>
                            <p class="text-xs text-gray-500">${file.extension.slice(1).toUpperCase()} • ${this._formatFileSize(file.size)}</p>
                        </div>
                        <svg class="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                        </svg>
                    `;
                } else if (file.type === 'audio') {
                    // Audio file list item
                    card.innerHTML = `
                        <div class="w-12 h-12 flex-shrink-0 rounded-lg overflow-hidden bg-gradient-to-br from-emerald-700/50 to-teal-900/50 flex items-center justify-center">
                            <svg class="w-6 h-6 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
                            </svg>
                        </div>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm text-white truncate">${this.escapeHtml(file.name)}${shortcutBadgeList}</p>
                            <p class="text-xs text-emerald-400">${file.extension.slice(1).toUpperCase()} • ${this._formatFileSize(file.size)}</p>
                        </div>
                        <svg class="w-4 h-4 text-emerald-400 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
                            <path d="M8 5v14l11-7z"/>
                        </svg>
                    `;
                }
            }

            card.addEventListener('click', () => this.openMediaViewer(file, fileList, index));
            container.appendChild(card);
        });
    }

    _formatFileSize(bytes) {
        if (!bytes || bytes === 0) return '';
        const units = ['B', 'KB', 'MB', 'GB'];
        let unitIndex = 0;
        let size = bytes;
        while (size >= 1024 && unitIndex < units.length - 1) {
            size /= 1024;
            unitIndex++;
        }
        return size.toFixed(unitIndex > 0 ? 1 : 0) + ' ' + units[unitIndex];
    }

    openMediaViewer(file, fileList = null, fileIndex = -1) {
        // Audio files go to music player instead
        if (file.type === 'audio') {
            this.playAudioFile(file, fileList, fileIndex);
            return;
        }

        if (!this.elements.mediaViewerModal || !this.elements.mediaViewerContent) return;

        // Store file list for navigation
        if (fileList) {
            this.mediaViewerFiles = fileList;
            this.mediaViewerIndex = fileIndex >= 0 ? fileIndex : 0;
        } else {
            this.mediaViewerFiles = [file];
            this.mediaViewerIndex = 0;
        }

        // Reset audio-only mode when opening new file
        this.mediaAudioOnlyMode = false;

        this._displayMediaFile(file);
        this._updateViewerNavButtons();
        this._setupSwipeGestures();

        // Show/hide audio toggle button based on file type
        if (this.elements.mediaViewerAudioToggle) {
            if (file.type === 'video') {
                this.elements.mediaViewerAudioToggle.classList.remove('hidden');
                this._updateAudioToggleIcon();
                // Setup click handler (remove existing first to avoid duplicates)
                this.elements.mediaViewerAudioToggle.onclick = () => this.toggleAudioOnlyMode();
            } else {
                this.elements.mediaViewerAudioToggle.classList.add('hidden');
            }
        }

        this.elements.mediaViewerModal.classList.remove('hidden');
    }

    _displayMediaFile(file) {
        this.elements.mediaViewerContent.innerHTML = '';

        if (file.type === 'image') {
            // Image files use HTTPS (same origin)
            const fileUrl = `/api/media/file?path=${encodeURIComponent(file.path)}`;
            const img = document.createElement('img');
            img.src = fileUrl;
            img.alt = file.name;
            img.className = 'max-w-[calc(100vw-2rem)] max-h-[calc(100vh-120px)] object-contain rounded-lg';
            this.elements.mediaViewerContent.appendChild(img);
        } else {
            // Video files use HTTP port for Android compatibility
            const fileUrl = this._getVideoUrl(file.path);
            const video = document.createElement('video');
            video.src = fileUrl;
            video.className = 'max-w-[calc(100vw-2rem)] max-h-[calc(100vh-120px)] rounded-lg';
            video.controls = true;
            video.autoplay = true;
            video.playsInline = true;
            video.setAttribute('playsinline', '');
            video.setAttribute('webkit-playsinline', '');

            // Error handling for playback issues
            video.addEventListener('error', (e) => {
                console.error('Video playback error:', e, video.error);
            });

            // Fullscreen landscape lock for mobile devices
            this._setupVideoFullscreenHandler(video);

            this.elements.mediaViewerContent.appendChild(video);
        }

        if (this.elements.mediaViewerFileName) {
            this.elements.mediaViewerFileName.textContent = file.name;
        }

        // Update counter
        if (this.elements.mediaViewerCounter && this.mediaViewerFiles.length > 1) {
            this.elements.mediaViewerCounter.textContent = `${this.mediaViewerIndex + 1} / ${this.mediaViewerFiles.length}`;
        } else if (this.elements.mediaViewerCounter) {
            this.elements.mediaViewerCounter.textContent = '';
        }
    }

    _setupSwipeGestures() {
        const container = this.elements.mediaViewerContent;
        if (!container || this._swipeHandlersSetup) return;

        let touchStartX = 0;
        let touchStartY = 0;
        let touchEndX = 0;
        let touchEndY = 0;

        const handleTouchStart = (e) => {
            touchStartX = e.changedTouches[0].screenX;
            touchStartY = e.changedTouches[0].screenY;
        };

        const handleTouchEnd = (e) => {
            touchEndX = e.changedTouches[0].screenX;
            touchEndY = e.changedTouches[0].screenY;
            this._handleSwipe(touchStartX, touchStartY, touchEndX, touchEndY);
        };

        container.addEventListener('touchstart', handleTouchStart, { passive: true });
        container.addEventListener('touchend', handleTouchEnd, { passive: true });

        this._swipeHandlersSetup = true;

        // Store handlers for cleanup
        this._swipeCleanup = () => {
            container.removeEventListener('touchstart', handleTouchStart);
            container.removeEventListener('touchend', handleTouchEnd);
            this._swipeHandlersSetup = false;
        };
    }

    _handleSwipe(startX, startY, endX, endY) {
        const deltaX = endX - startX;
        const deltaY = endY - startY;
        const minSwipeDistance = 50;  // Minimum swipe distance in pixels

        // Check if horizontal swipe is dominant
        if (Math.abs(deltaX) > Math.abs(deltaY) && Math.abs(deltaX) > minSwipeDistance) {
            if (deltaX > 0) {
                // Swipe right -> previous
                this.navigateViewerPrev();
            } else {
                // Swipe left -> next
                this.navigateViewerNext();
            }
        }
    }

    _getVideoUrl(path) {
        // For HTTPS pages, use HTTP port for video streaming (Android compatibility)
        // Default video port is main port + 1 (e.g., 3000 -> 3001, 6002 -> 6003)
        if (window.location.protocol === 'https:') {
            const mainPort = parseInt(window.location.port) || 443;
            const videoPort = mainPort + 1;
            return `http://${window.location.hostname}:${videoPort}/api/media/file?path=${encodeURIComponent(path)}`;
        }
        // For HTTP pages, use same origin
        return `/api/media/file?path=${encodeURIComponent(path)}`;
    }

    _setupVideoFullscreenHandler(video) {
        // Check if Screen Orientation API is supported
        const supportsOrientationLock = screen.orientation && typeof screen.orientation.lock === 'function';

        // Handle fullscreen change events
        const handleFullscreenChange = async () => {
            const isFullscreen = !!(
                document.fullscreenElement ||
                document.webkitFullscreenElement ||
                document.mozFullScreenElement ||
                document.msFullscreenElement ||
                video.webkitDisplayingFullscreen  // iOS Safari
            );

            if (isFullscreen && supportsOrientationLock) {
                try {
                    await screen.orientation.lock('landscape');
                    console.log('[Video] Screen orientation locked to landscape');
                } catch (err) {
                    // Orientation lock may fail on some devices/browsers
                    console.debug('[Video] Could not lock orientation:', err.message);
                }
            } else if (!isFullscreen && supportsOrientationLock) {
                try {
                    screen.orientation.unlock();
                    console.log('[Video] Screen orientation unlocked');
                } catch (err) {
                    console.debug('[Video] Could not unlock orientation:', err.message);
                }
            }
        };

        // Standard fullscreen API
        document.addEventListener('fullscreenchange', handleFullscreenChange);
        document.addEventListener('webkitfullscreenchange', handleFullscreenChange);
        document.addEventListener('mozfullscreenchange', handleFullscreenChange);
        document.addEventListener('MSFullscreenChange', handleFullscreenChange);

        // iOS Safari specific: webkitbeginfullscreen / webkitendfullscreen
        video.addEventListener('webkitbeginfullscreen', handleFullscreenChange);
        video.addEventListener('webkitendfullscreen', handleFullscreenChange);

        // Cleanup function stored on video element for later removal
        video._fullscreenCleanup = () => {
            document.removeEventListener('fullscreenchange', handleFullscreenChange);
            document.removeEventListener('webkitfullscreenchange', handleFullscreenChange);
            document.removeEventListener('mozfullscreenchange', handleFullscreenChange);
            document.removeEventListener('MSFullscreenChange', handleFullscreenChange);
            video.removeEventListener('webkitbeginfullscreen', handleFullscreenChange);
            video.removeEventListener('webkitendfullscreen', handleFullscreenChange);

            // Ensure orientation is unlocked when video is removed
            if (supportsOrientationLock) {
                try {
                    screen.orientation.unlock();
                } catch (err) {
                    // Ignore
                }
            }
        };
    }

    _updateViewerNavButtons() {
        if (this.elements.mediaViewerPrev) {
            this.elements.mediaViewerPrev.disabled = this.mediaViewerIndex <= 0;
        }
        if (this.elements.mediaViewerNext) {
            this.elements.mediaViewerNext.disabled = this.mediaViewerIndex >= this.mediaViewerFiles.length - 1;
        }
    }

    navigateViewerPrev() {
        if (this.mediaViewerIndex > 0) {
            const video = this.elements.mediaViewerContent?.querySelector('video');
            if (video) {
                if (video._fullscreenCleanup) { video._fullscreenCleanup(); }
                video.pause();
            }

            this.mediaViewerIndex--;
            const file = this.mediaViewerFiles[this.mediaViewerIndex];
            this._displayMediaFile(file);
            this._updateViewerNavButtons();
        }
    }

    navigateViewerNext() {
        if (this.mediaViewerIndex < this.mediaViewerFiles.length - 1) {
            const video = this.elements.mediaViewerContent?.querySelector('video');
            if (video) {
                if (video._fullscreenCleanup) { video._fullscreenCleanup(); }
                video.pause();
            }

            this.mediaViewerIndex++;
            const file = this.mediaViewerFiles[this.mediaViewerIndex];
            this._displayMediaFile(file);
            this._updateViewerNavButtons();
        }
    }

    closeMediaViewer() {
        if (!this.elements.mediaViewerModal) return;
        this.elements.mediaViewerModal.classList.add('hidden');

        const video = this.elements.mediaViewerContent?.querySelector('video');
        if (video) {
            // Cleanup fullscreen orientation handlers
            if (video._fullscreenCleanup) {
                video._fullscreenCleanup();
            }
            video.pause();
            video.src = '';
        }

        // Cleanup swipe handlers
        if (this._swipeCleanup) {
            this._swipeCleanup();
        }

        // Reset audio-only mode
        this.mediaAudioOnlyMode = false;
        this._updateAudioToggleIcon();

        this.mediaViewerFiles = [];
        this.mediaViewerIndex = 0;
    }

    toggleAudioOnlyMode() {
        this.mediaAudioOnlyMode = !this.mediaAudioOnlyMode;
        this._updateAudioToggleIcon();

        // Find the current video element
        const video = this.elements.mediaViewerContent?.querySelector('video');
        if (video) {
            if (this.mediaAudioOnlyMode) {
                // Hide video, show audio-only visualizer
                video.style.opacity = '0';
                video.style.height = '0';
                video.style.position = 'absolute';
                this._showAudioVisualizer(video);
            } else {
                // Show video, hide visualizer
                video.style.opacity = '1';
                video.style.height = 'auto';
                video.style.position = 'relative';
                this._hideAudioVisualizer();
            }
        }
    }

    _updateAudioToggleIcon() {
        const toggle = this.elements.mediaViewerAudioToggle;
        const icon = this.elements.mediaViewerAudioIcon;
        if (!toggle || !icon) return;

        if (this.mediaAudioOnlyMode) {
            // Audio only mode - show video icon (to indicate clicking will return to video)
            toggle.classList.add('bg-purple-600', 'text-white');
            toggle.classList.remove('bg-black/50', 'text-white/70');
            toggle.title = '映像を表示';
            icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />';
        } else {
            // Video mode - show audio icon (to indicate clicking will switch to audio only)
            toggle.classList.remove('bg-purple-600', 'text-white');
            toggle.classList.add('bg-black/50', 'text-white/70');
            toggle.title = '音声のみモード';
            icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />';
        }
    }

    _showAudioVisualizer(video) {
        // Remove existing visualizer if any
        this._hideAudioVisualizer();

        // Create audio visualizer container
        const visualizer = document.createElement('div');
        visualizer.id = 'audioVisualizer';
        visualizer.className = 'flex flex-col items-center justify-center gap-6 p-8';
        visualizer.innerHTML = `
            <div class="w-32 h-32 rounded-full bg-gradient-to-br from-purple-600 to-blue-600 flex items-center justify-center shadow-2xl animate-pulse">
                <svg class="w-16 h-16 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
                </svg>
            </div>
            <p class="text-white/80 text-lg font-medium">音声のみ再生中</p>
            <div class="flex gap-1" id="audioWaveform">
                ${Array(12).fill(0).map(() => `<div class="w-1 bg-purple-400 rounded-full animate-pulse" style="height: ${Math.random() * 40 + 10}px; animation-delay: ${Math.random() * 0.5}s;"></div>`).join('')}
            </div>
        `;

        this.elements.mediaViewerContent.appendChild(visualizer);

        // Animate waveform bars
        this._audioVisualizerInterval = setInterval(() => {
            const bars = visualizer.querySelectorAll('#audioWaveform > div');
            bars.forEach(bar => {
                bar.style.height = `${Math.random() * 40 + 10}px`;
            });
        }, 150);
    }

    _hideAudioVisualizer() {
        const visualizer = document.getElementById('audioVisualizer');
        if (visualizer) {
            visualizer.remove();
        }
        if (this._audioVisualizerInterval) {
            clearInterval(this._audioVisualizerInterval);
            this._audioVisualizerInterval = null;
        }
    }


    navigateMediaBack() {
        if (this.mediaPathHistory.length > 0) {
            const prevPath = this.mediaPathHistory.pop();
            // Navigate without adding to history
            this.mediaCurrentPath = '';  // Clear to prevent duplicate push
            this.browseMediaPath(prevPath);
            // Remove the duplicate entry
            if (this.mediaPathHistory.length > 0 && this.mediaPathHistory[this.mediaPathHistory.length - 1] === prevPath) {
                this.mediaPathHistory.pop();
            }
        } else {
            this.browseMediaPath(this.mediaRootPath);
        }
    }

    navigateMediaUp() {
        // Request parent folder from current path
        if (this.mediaCanGoUp && this.mediaCurrentPath) {
            // Get parent path (works for both Windows and Unix paths)
            const pathParts = this.mediaCurrentPath.replace(/\\/g, '/').split('/').filter(Boolean);
            pathParts.pop();
            if (pathParts.length > 0) {
                const parentPath = pathParts.join('\\');
                this.browseMediaPath(parentPath);
            } else {
                this.browseMediaPath(this.mediaRootPath);
            }
        }
    }

    navigateMediaHome() {
        this.mediaPathHistory = [];
        this.browseMediaPath(this.mediaRootPath);
    }

    async addMediaBookmark() {
        if (!this.mediaCurrentPath) {
            this.showNotification('ブックマークするフォルダを開いてください', 'error');
            return;
        }

        if (this.mediaIsBookmarked) {
            this.showNotification('このフォルダは既にブックマークされています', 'info');
            return;
        }

        // Get folder name from path
        const pathParts = this.mediaCurrentPath.replace(/\\/g, '/').split('/').filter(Boolean);
        const folderName = pathParts[pathParts.length - 1] || 'ブックマーク';

        try {
            const response = await this.apiFetch('/api/media/bookmarks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: folderName,
                    path: this.mediaCurrentPath,
                    icon: '📁'
                })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'ブックマークの追加に失敗しました');
            }

            const result = await response.json();
            this.showNotification('ブックマークを追加しました', 'success');

            // Refresh bookmarks
            this.mediaBookmarks.push(result.bookmark);
            this.renderMediaBookmarks();
            this.mediaIsBookmarked = true;
            this.updateMediaNavButtons();
        } catch (error) {
            console.error('Failed to add bookmark:', error);
            this.showNotification(error.message || 'ブックマークの追加に失敗しました', 'error');
        }
    }

    async removeMediaBookmark(path) {
        if (!confirm('このブックマークを削除しますか？')) {
            return;
        }

        try {
            const response = await this.apiFetch('/api/media/bookmarks', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path })
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'ブックマークの削除に失敗しました');
            }

            this.showNotification('ブックマークを削除しました', 'success');

            // Update local state
            this.mediaBookmarks = this.mediaBookmarks.filter(bm => bm.path !== path);
            this.renderMediaBookmarks();

            // Update bookmark status if current path was removed
            if (this.mediaCurrentPath === path) {
                this.mediaIsBookmarked = false;
                this.updateMediaNavButtons();
            }
        } catch (error) {
            console.error('Failed to remove bookmark:', error);
            this.showNotification(error.message || 'ブックマークの削除に失敗しました', 'error');
        }
    }

    updateMediaBreadcrumb() {
        if (!this.elements.mediaBreadcrumb) return;

        if (!this.mediaCurrentPath) {
            this.elements.mediaBreadcrumb.textContent = '';
            return;
        }

        // Show current path with bookmark indicator
        const bookmarkIcon = this.mediaIsBookmarked ? '⭐ ' : '';
        this.elements.mediaBreadcrumb.textContent = bookmarkIcon + this.mediaCurrentPath.replace(/\\/g, ' / ');
    }

    updateMediaNavButtons() {
        // Back button
        if (this.elements.mediaBackBtn) {
            const canGoBack = this.mediaPathHistory.length > 0;
            this.elements.mediaBackBtn.disabled = !canGoBack;
            this.elements.mediaBackBtn.classList.toggle('opacity-30', !canGoBack);
        }

        // Go up button
        if (this.elements.mediaGoUpBtn) {
            this.elements.mediaGoUpBtn.disabled = !this.mediaCanGoUp;
            this.elements.mediaGoUpBtn.classList.toggle('opacity-30', !this.mediaCanGoUp);
        }

        // Add bookmark button - show star filled if bookmarked
        if (this.elements.mediaAddBookmarkBtn) {
            if (this.mediaIsBookmarked) {
                this.elements.mediaAddBookmarkBtn.classList.add('text-yellow-400');
                this.elements.mediaAddBookmarkBtn.classList.remove('text-gray-400');
            } else {
                this.elements.mediaAddBookmarkBtn.classList.remove('text-yellow-400');
                this.elements.mediaAddBookmarkBtn.classList.add('text-gray-400');
            }
        }
    }

    connect() {
        // Close existing WebSocket connection if any to prevent duplicate connections
        if (this.ws) {
            try {
                // Remove event handlers to prevent triggering reconnect logic
                this.ws.onclose = null;
                this.ws.onerror = null;
                this.ws.onmessage = null;
                this.ws.onopen = null;

                if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
                    this.ws.close();
                }
            } catch (e) {
                console.warn('Error closing existing WebSocket:', e);
            }
        }

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            this.isConnected = true;
            this.updateConnectionStatus('接続済み', 'connected');
            this.showNotification('サーバーに接続しました', 'success');
            this.hideRestartOverlay();
            this.toggleModeButtons(false);
            this.fetchModeStatus();

            // Clear reconnect interval
            if (this.reconnectInterval) {
                clearInterval(this.reconnectInterval);
                this.reconnectInterval = null;
            }
        };

        this.ws.onclose = (event) => {
            this.isConnected = false;
            this.updateConnectionStatus('切断中', 'disconnected');
            this.showNotification('サーバーから切断されました', 'error');

            if (event.code === 1008) {
                this.showLoginOverlay();
                return;
            }

            // Try to reconnect
            if (!this.reconnectInterval) {
                this.reconnectInterval = setInterval(() => {
                    this.connect();
                }, 3000);
            }
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            } catch (error) {
                console.error('Failed to parse message:', error);
            }
        };
    }

    handleMessage(data) {
        switch (data.type) {
            case 'chat_history':
                this.loadChatHistory(data.data);
                break;
            case 'new_message':
                // Hide processing indicator when assistant responds
                if (data.data && data.data.type === 'assistant') {
                    this.hideProcessingIndicator();
                }
                this.addMessage(data.data);
                this.notifyConversationHistoryUpdated('new_message');
                break;
            case 'chat_cleared':
                this.clearChat();
                this.notifyConversationHistoryUpdated('chat_cleared');
                break;
            case 'voice_status_change':
                this.updateVoiceStatus(data.data);
                break;
            case 'rms_update':
                this.updateMicLevel(data.data.rms);
                break;
            case 'character_switch':
                this.handleCharacterSwitch(data.data);
                break;
            case 'command_progress':
                this.handleCommandProgress(data);
                break;
            case 'external_llm_permission_request':
                this.handleExternalLLMPermissionRequest(data.data);
                break;
            case 'tool_call_status':
                this.handleToolCallStatus(data.data);
                break;
            case 'llm_mode_change':
                this.llmMode = data.data.mode;
                this.updateLlmModeUI();
                break;
            default:
                console.warn('Unknown message type:', data.type);
        }
    }

    notifyConversationHistoryUpdated(reason = 'event') {
        if (window.conversationHistoryManager &&
            typeof window.conversationHistoryManager.scheduleAutoRefresh === 'function') {
            window.conversationHistoryManager.scheduleAutoRefresh(reason);
            return;
        }

        document.dispatchEvent(new CustomEvent('conversationHistoryRefresh', {
            detail: { reason }
        }));
    }

    handleToolCallStatus(data) {
        // Update processing indicator with tool call info
        if (data.status === 'started') {
            this.updateProcessingText(`ツール実行中: ${data.tool_name || 'unknown'}...`);
        } else if (data.status === 'completed') {
            this.updateProcessingText('応答を生成中...');
        }
    }

    handleCommandProgress(data) {
        const progressData = data.data;
        const commandId = data.command_id;

        // Format progress message
        let message = progressData.message || '';

        // Add phase information
        const phaseLabels = {
            'master_db': 'マスターDB',
            'image_folders': '画像フォルダ',
            'crawler_logs': 'クローラーログ',
            'complete': '完了'
        };

        const phaseLabel = phaseLabels[progressData.phase] || progressData.phase;

        // Create detailed message
        if (progressData.phase === 'complete') {
            message = `✅ ${message}`;
        } else if (progressData.total > 0) {
            message = `📊 [${phaseLabel}] ${message} (${progressData.current}/${progressData.total})`;
        } else {
            message = `📊 [${phaseLabel}] ${message}`;
        }

        // Add stats if available
        if (progressData.stats && (progressData.stats.uploaded > 0 || progressData.stats.skipped > 0)) {
            message += ` | アップロード: ${progressData.stats.uploaded}, スキップ: ${progressData.stats.skipped}`;
            if (progressData.stats.failed > 0) {
                message += `, 失敗: ${progressData.stats.failed}`;
            }
        }

        // Display as system message
        this.addSystemMessage(message);
    }

    addSystemMessage(message) {
        const entry = {
            type: 'system',
            message: message,
            timestamp: new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        };
        this.addMessage(entry);
    }

    // ── External LLM Permission Handling ─────────────────────────────────

    handleExternalLLMPermissionRequest(data) {
        if (!this.elements.externalLLMPermissionModal) return;

        const { request_id, tool_name, description } = data;

        // Store request ID for response
        this.pendingPermissionRequestId = request_id;

        // Update modal content
        if (this.elements.permissionToolName) {
            this.elements.permissionToolName.textContent = tool_name || '-';
        }
        if (this.elements.permissionDescription) {
            this.elements.permissionDescription.textContent = description || '-';
        }

        // Show modal
        this.elements.externalLLMPermissionModal.classList.remove('hidden');

        // Add system message to chat so user notices
        this.addSystemMessage(`⚠️ 外部API使用の確認が必要です: ${description || tool_name}`);

        // Update processing indicator
        this.updateProcessingText('承認待ち...');

        console.log(`[ExternalLLM] Permission request: ${request_id} for ${tool_name}`);
    }

    sendExternalLLMPermissionResponse(approved) {
        if (!this.pendingPermissionRequestId) return;

        // Send response via WebSocket
        if (this.ws && this.isConnected) {
            this.ws.send(JSON.stringify({
                type: 'external_llm_permission_response',
                data: {
                    request_id: this.pendingPermissionRequestId,
                    approved: approved
                }
            }));
        }

        console.log(`[ExternalLLM] Permission response: ${this.pendingPermissionRequestId} -> ${approved ? 'approved' : 'denied'}`);

        // Hide modal and clear state
        if (this.elements.externalLLMPermissionModal) {
            this.elements.externalLLMPermissionModal.classList.add('hidden');
        }
        this.pendingPermissionRequestId = null;
    }

    async ensureConversationSession() {
        if (this.currentConversationSessionId) {
            return this.currentConversationSessionId;
        }

        const characterName = this.elements.characterSelect
            ? (this.elements.characterSelect.value || '葵')
            : '葵';

        // Get selected project ID
        const projectId = (typeof window.getSelectedProjectId === 'function')
            ? window.getSelectedProjectId()
            : null;

        console.log('[ChatClient] ensureConversationSession projectId:', projectId);

        try {
            const payload = { character_name: characterName };

            // Include project_id if selected
            if (projectId) {
                payload.project_id = projectId;
            }

            console.log('[ChatClient] Creating session with payload:', payload);

            const response = await this.apiFetch('/api/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to create session');
            }

            const data = await response.json();
            const sessionId = data?.session?.id;
            if (!data.success || !sessionId) {
                throw new Error('Invalid session response');
            }

            this.currentConversationSessionId = sessionId;

            if (window.conversationHistoryManager &&
                typeof window.conversationHistoryManager.setCurrentSessionId === 'function') {
                window.conversationHistoryManager.setCurrentSessionId(sessionId);
            }

            document.dispatchEvent(new CustomEvent('conversationChanged', {
                detail: { sessionId, isNew: true }
            }));

            this.notifyConversationHistoryUpdated('session_created');

            return sessionId;
        } catch (error) {
            console.error('Failed to create conversation session:', error);
            this.showNotification('新規セッションの作成に失敗しました', 'error');
            return null;
        }
    }

    resetConversationSession() {
        // Clear the cached session ID so a new session will be created on next message
        this.currentConversationSessionId = null;
        console.log('[ChatClient] Conversation session reset - new session will be created on next message');
    }

    async sendMessage() {
        const message = this.elements.messageInput.value.trim();
        if (!message && !this.pendingImage) return;
        if (!this.isConnected) return;

        const sessionId = await this.ensureConversationSession();
        if (!sessionId) return;

        // Get selected project ID from global function
        const projectId = (typeof window.getSelectedProjectId === 'function')
            ? window.getSelectedProjectId()
            : null;

        const payload = {
            type: 'user_message',
            data: {
                message,
                image: this.pendingImage || null,
                session_id: sessionId,  // Include current session ID
                project_id: projectId   // Include selected project ID
            }
        };

        this.ws.send(JSON.stringify(payload));

        // Show processing indicator
        this.showProcessingIndicator('処理中...');

        this.elements.messageInput.value = '';
        this.clearImagePreview();
        this.updateCharCount();
        this.adjustTextareaHeight();
    }

    showProcessingIndicator(text = '処理中...') {
        if (this.elements.processingIndicator) {
            this.elements.processingIndicator.classList.remove('hidden');
            if (this.elements.processingText) {
                this.elements.processingText.textContent = text;
            }
            this.scrollToBottom();
        }
    }

    hideProcessingIndicator() {
        if (this.elements.processingIndicator) {
            this.elements.processingIndicator.classList.add('hidden');
        }
    }

    updateProcessingText(text) {
        if (this.elements.processingText) {
            this.elements.processingText.textContent = text;
        }
    }

    // ── Image Input Handling ──────────────────────────────────────────────
    handleImageSelect(event) {
        const file = event.target.files[0];
        if (!file) return;

        // Validate file type
        if (!file.type.startsWith('image/')) {
            this.showNotification('画像ファイルを選択してください', 'error');
            return;
        }

        // Validate file size (max 10MB)
        const maxSize = 10 * 1024 * 1024;
        if (file.size > maxSize) {
            this.showNotification('画像サイズは10MB以下にしてください', 'error');
            return;
        }

        // Use unified processing method
        this.processImageFile(file);
    }

    showImagePreview(dataUrl, fileName) {
        if (!this.elements.imagePreviewContainer || !this.elements.imagePreview) return;

        this.elements.imagePreview.src = dataUrl;
        if (this.elements.imageFileName) {
            this.elements.imageFileName.textContent = fileName;
        }
        this.elements.imagePreviewContainer.classList.remove('hidden');
    }

    clearImagePreview() {
        this.pendingImage = null;
        if (this.elements.imagePreviewContainer) {
            this.elements.imagePreviewContainer.classList.add('hidden');
        }
        if (this.elements.imagePreview) {
            this.elements.imagePreview.src = '';
        }
        if (this.elements.imageFileName) {
            this.elements.imageFileName.textContent = '';
        }
        if (this.elements.imageInput) {
            this.elements.imageInput.value = '';
        }
    }

    // ── Drag & Drop Handling ──────────────────────────────────────────────
    handleDragOver(event) {
        event.preventDefault();
        event.stopPropagation();

        // Check if dragged item contains files
        if (event.dataTransfer.types.includes('Files')) {
            event.dataTransfer.dropEffect = 'copy';
            // Add visual feedback
            this.elements.messageInput.classList.add('border-blue-500', 'bg-gray-700');
        }
    }

    handleDragLeave(event) {
        event.preventDefault();
        event.stopPropagation();

        // Remove visual feedback
        this.elements.messageInput.classList.remove('border-blue-500', 'bg-gray-700');
    }

    // ── Chat Pane Drag & Drop (entire chat area) ────────────────────────
    handleChatPaneDragOver(event) {
        event.preventDefault();
        event.stopPropagation();

        // Check if dragged item contains files
        if (event.dataTransfer.types.includes('Files')) {
            event.dataTransfer.dropEffect = 'copy';
            // Add visual feedback to chat pane
            this.elements.chatPane.classList.add('ring-2', 'ring-blue-500', 'ring-inset');
        }
    }

    handleChatPaneDragLeave(event) {
        event.preventDefault();
        event.stopPropagation();

        // Only remove feedback if we're leaving the chat pane entirely
        // (not just entering a child element)
        if (!this.elements.chatPane.contains(event.relatedTarget)) {
            this.elements.chatPane.classList.remove('ring-2', 'ring-blue-500', 'ring-inset');
        }
    }

    handleDrop(event) {
        event.preventDefault();
        event.stopPropagation();

        // Remove visual feedback from both messageInput and chatPane
        this.elements.messageInput.classList.remove('border-blue-500', 'bg-gray-700');
        if (this.elements.chatPane) {
            this.elements.chatPane.classList.remove('ring-2', 'ring-blue-500', 'ring-inset');
        }

        const files = event.dataTransfer.files;
        if (files.length > 0) {
            const file = files[0];

            // Check if it's an image file
            if (file.type.startsWith('image/')) {
                // Validate file size (max 10MB)
                const maxSize = 10 * 1024 * 1024;
                if (file.size > maxSize) {
                    this.showNotification('画像サイズは10MB以下にしてください', 'error');
                    return;
                }
                // Process the image
                this.processImageFile(file);
                return;
            }

            // Check if it's an Office file
            if (this.isOfficeFile(file.name)) {
                this.uploadOfficeFile(file);
                return;
            }

            // Unsupported file type
            this.showNotification('対応していないファイル形式です（画像またはOfficeファイルをドロップしてください）', 'error');
        }
    }

    // Check if file is a supported document (Office or text/data files)
    isOfficeFile(filename) {
        const ext = '.' + filename.split('.').pop().toLowerCase();
        const supportedExtensions = [
            // Office files
            '.docx', '.xlsx', '.pptx', '.pdf',
            // Text files
            '.txt', '.log', '.md', '.markdown', '.rst', '.text',
            // Data/Config files
            '.csv', '.tsv', '.json', '.jsonl', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
            // Web
            '.html', '.htm', '.css',
            // Code (major languages)
            '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.c', '.cpp', '.h', '.hpp',
            '.cs', '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.bat', '.ps1',
            '.sql', '.r', '.swift', '.kt', '.scala', '.lua'
        ];
        return supportedExtensions.includes(ext);
    }

    // ── Clipboard Paste Handling ──────────────────────────────────────────
    handlePaste(event) {
        const items = event.clipboardData?.items;
        if (!items) return;

        // Look for image in clipboard
        for (let i = 0; i < items.length; i++) {
            const item = items[i];

            if (item.type.startsWith('image/')) {
                event.preventDefault();

                const file = item.getAsFile();
                if (file) {
                    // Validate file size (max 10MB)
                    const maxSize = 10 * 1024 * 1024;
                    if (file.size > maxSize) {
                        this.showNotification('画像サイズは10MB以下にしてください', 'error');
                        return;
                    }

                    // Process the image
                    this.processImageFile(file);
                    this.showNotification('クリップボードから画像を読み込みました', 'success');
                }
                break;
            }
        }
    }

    // ── Unified Image Processing ──────────────────────────────────────────
    processImageFile(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.pendingImage = {
                data: e.target.result,  // Base64 data URL
                mimeType: file.type,
                name: file.name
            };
            this.showImagePreview(e.target.result, file.name);
        };
        reader.onerror = () => {
            this.showNotification('画像の読み込みに失敗しました', 'error');
        };
        reader.readAsDataURL(file);
    }

    clearChatRequest() {
        this.ws.send(JSON.stringify({
            type: 'clear_chat'
        }));
    }

    loadChatHistory(history) {
        this.elements.chatMessages.innerHTML = '';
        history.forEach(entry => this.addMessage(entry));
        this.scrollToBottom();
    }

    addMessage(entry) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'animate-in fade-in slide-in-from-bottom-2 duration-300';

        // Store message ID for branching if available
        const messageId = entry.id || null;
        const hasBranches = entry.branch_count && entry.branch_count > 1;
        const branchIndex = entry.branch_index || 0;
        const branchCount = entry.branch_count || 1;

        switch (entry.type) {
            case 'user':
                // Build image preview HTML if image is attached
                const imageHtml = entry.image_preview
                    ? `<div class="mb-2"><img src="${entry.image_preview}" class="max-w-[200px] max-h-[200px] rounded-lg border border-gray-400 object-contain"></div>`
                    : '';
                const messageTextHtml = entry.message ? this.escapeHtml(entry.message) : '';

                // Build branch navigation if there are multiple branches
                const branchNavHtml = hasBranches ? `
                    <div class="branch-nav flex items-center gap-1 text-xs text-gray-400" data-message-id="${messageId}">
                        <button class="branch-prev p-1 hover:bg-gray-600 rounded disabled:opacity-30" 
                            ${branchIndex === 0 ? 'disabled' : ''} title="前の分岐">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M15 18l-6-6 6-6"/>
                            </svg>
                        </button>
                        <span class="branch-indicator">${branchIndex + 1}/${branchCount}</span>
                        <button class="branch-next p-1 hover:bg-gray-600 rounded disabled:opacity-30" 
                            ${branchIndex === branchCount - 1 ? 'disabled' : ''} title="次の分岐">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M9 18l6-6-6-6"/>
                            </svg>
                        </button>
                    </div>
                ` : '';

                // Edit button for user messages
                const editBtnHtml = messageId ? `
                    <button class="edit-msg-btn p-1 hover:bg-gray-600 rounded text-gray-400 hover:text-white transition"
                        data-message-id="${messageId}"
                        data-message-text="${this.escapeHtml(entry.message || '').replace(/"/g, '&quot;')}"
                        title="編集">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                    </button>
                ` : '';

                messageDiv.innerHTML = `
                    <div class="flex justify-end">
                        <div class="max-w-[80%]">
                            <div class="flex justify-between items-center mb-1 text-xs text-gray-400 gap-2">
                                <div class="flex items-center gap-2">
                                    <span class="font-semibold">あなた</span>
                                    ${branchNavHtml}
                                </div>
                                <div class="flex items-center gap-1">
                                    ${editBtnHtml}
                                    <span>${entry.timestamp}</span>
                                </div>
                            </div>
                            <div class="bg-slate-500 text-white px-4 py-3 rounded-2xl rounded-br-sm">
                                ${imageHtml}
                                <span class="message-content">${messageTextHtml}</span>
                            </div>
                        </div>
                    </div>
                `;

                // Attach edit button handler
                if (messageId) {
                    const editBtn = messageDiv.querySelector('.edit-msg-btn');
                    if (editBtn) {
                        editBtn.addEventListener('click', () => {
                            this.openEditMessageModal(messageId, entry.message || '');
                        });
                    }

                    // Attach branch navigation handlers
                    if (hasBranches) {
                        const prevBtn = messageDiv.querySelector('.branch-prev');
                        const nextBtn = messageDiv.querySelector('.branch-next');

                        if (prevBtn) {
                            prevBtn.addEventListener('click', () => {
                                this.navigateBranch(messageId, branchIndex - 1);
                            });
                        }
                        if (nextBtn) {
                            nextBtn.addEventListener('click', () => {
                                this.navigateBranch(messageId, branchIndex + 1);
                            });
                        }
                    }
                }
                break;

            case 'assistant':
                const msgId = `msg_${Date.now()}_${this.messageCounter++}`;
                messageDiv.innerHTML = `
                    <div class="flex justify-start group">
                        <div class="max-w-[80%]">
                            <div class="flex justify-between items-center mb-1 text-xs text-gray-400">
                                <span class="font-semibold">${this.escapeHtml(entry.character || 'アシスタント')}</span>
                                <span>${entry.timestamp}</span>
                            </div>
                            <div class="bg-gray-600 border border-gray-500 text-gray-100 px-4 py-3 rounded-2xl rounded-bl-sm">
                                ${this.escapeHtml(entry.message)}
                            </div>
                            <div class="flex justify-end mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                <button class="feedback-btn text-xs text-gray-500 hover:text-orange-400 transition flex items-center gap-1 px-2 py-1 rounded hover:bg-gray-700/50"
                                    data-msg-id="${msgId}"
                                    data-msg-text="${this.escapeHtml(entry.message).replace(/"/g, '&quot;')}"
                                    data-msg-char="${this.escapeHtml(entry.character || 'アシスタント')}"
                                    title="この回答にフィードバック">
                                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 21v-4m0 0V5a2 2 0 012-2h6.5l1 1H21l-3 6 3 6h-8.5l-1-1H5a2 2 0 00-2 2zm9-13.5V9"/>
                                    </svg>
                                    <span>報告</span>
                                </button>
                            </div>
                        </div>
                    </div>
                `;
                // Attach click handler for feedback button
                const feedbackBtn = messageDiv.querySelector('.feedback-btn');
                if (feedbackBtn) {
                    feedbackBtn.addEventListener('click', () => {
                        this.openFeedbackModal({
                            id: feedbackBtn.dataset.msgId,
                            message: feedbackBtn.dataset.msgText,
                            character: feedbackBtn.dataset.msgChar
                        });
                    });
                }
                break;

            case 'system':
                messageDiv.innerHTML = `
                    <div class="flex justify-center">
                        <div class="max-w-[90%] bg-blue-900/30 border border-blue-700 text-blue-300 px-4 py-2 rounded-xl text-sm flex items-center gap-2">
                            <span>ℹ️</span>
                            <span>${this.escapeHtml(entry.message)}</span>
                            <span class="text-xs opacity-60">${entry.timestamp}</span>
                        </div>
                    </div>
                `;
                break;
        }

        this.elements.chatMessages.appendChild(messageDiv);
        this.scrollToBottom();
    }

    // ── Message Edit & Branching ───────────────────────────────────────────

    openEditMessageModal(messageId, currentContent) {
        // Create edit modal if it doesn't exist
        let modal = document.getElementById('editMessageModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'editMessageModal';
            modal.className = 'fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 hidden';
            modal.innerHTML = `
                <div class="bg-gray-800 rounded-2xl border border-gray-700 shadow-2xl w-full max-w-lg">
                    <div class="p-4 border-b border-gray-700">
                        <h3 class="text-lg font-semibold text-white">メッセージを編集</h3>
                        <p class="text-xs text-gray-400 mt-1">編集すると新しい分岐が作成されます</p>
                    </div>
                    <div class="p-4">
                        <textarea id="editMessageText" rows="4" 
                            class="w-full px-4 py-3 bg-gray-700 text-gray-100 rounded-xl border border-gray-600 resize-none focus:outline-none focus:border-purple-500"
                            placeholder="メッセージを入力..."></textarea>
                    </div>
                    <div class="flex justify-end gap-2 p-4 border-t border-gray-700">
                        <button id="cancelEditBtn" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium text-gray-300 transition">
                            キャンセル
                        </button>
                        <button id="submitEditBtn" class="px-4 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium text-white transition">
                            送信
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            // Add event listeners
            document.getElementById('cancelEditBtn').addEventListener('click', () => {
                this.closeEditMessageModal();
            });

            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    this.closeEditMessageModal();
                }
            });
        }

        // Set current content and message ID
        const textarea = document.getElementById('editMessageText');
        textarea.value = currentContent;
        modal.dataset.messageId = messageId;

        // Update submit button handler
        const submitBtn = document.getElementById('submitEditBtn');
        const newSubmitBtn = submitBtn.cloneNode(true);
        submitBtn.parentNode.replaceChild(newSubmitBtn, submitBtn);
        newSubmitBtn.addEventListener('click', () => {
            this.submitMessageEdit(messageId, textarea.value);
        });

        // Show modal
        modal.classList.remove('hidden');
        textarea.focus();
    }

    closeEditMessageModal() {
        const modal = document.getElementById('editMessageModal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    async submitMessageEdit(messageId, newContent) {
        if (!newContent.trim()) {
            this.showNotification('メッセージを入力してください', 'error');
            return;
        }

        if (!this.currentConversationSessionId) {
            this.showNotification('セッションが見つかりません', 'error');
            return;
        }

        try {
            const response = await this.apiFetch(
                `/api/conversations/${this.currentConversationSessionId}/messages/${messageId}`,
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: newContent })
                }
            );

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || '編集に失敗しました');
            }

            const result = await response.json();
            this.closeEditMessageModal();
            this.showNotification('メッセージを編集しました（新しい分岐を作成）', 'success');

            // Show processing indicator for LLM response
            this.showProcessingIndicator('新しい応答を生成中...');

            // Send the edited message to get a new response
            if (this.ws && this.isConnected) {
                this.ws.send(JSON.stringify({
                    type: 'user_message',
                    data: {
                        message: newContent,
                        session_id: this.currentConversationSessionId,
                        is_edit: true,
                        original_message_id: messageId
                    }
                }));
            }

            // Reload messages to show the branching
            await this.loadSessionMessages(this.currentConversationSessionId);

        } catch (error) {
            console.error('Failed to edit message:', error);
            this.showNotification(error.message || '編集に失敗しました', 'error');
        }
    }

    async navigateBranch(messageId, targetBranchIndex) {
        if (!this.currentConversationSessionId) return;

        try {
            // First, get all siblings
            const branchesResponse = await this.apiFetch(
                `/api/conversations/${this.currentConversationSessionId}/messages/${messageId}/branches`
            );

            if (!branchesResponse.ok) {
                throw new Error('分岐の取得に失敗しました');
            }

            const branchesData = await branchesResponse.json();
            const branches = branchesData.branches;

            if (targetBranchIndex < 0 || targetBranchIndex >= branches.length) {
                return;
            }

            const targetBranch = branches[targetBranchIndex];

            // Switch to the target branch
            const switchResponse = await this.apiFetch(
                `/api/conversations/${this.currentConversationSessionId}/messages/${messageId}/switch-branch`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_message_id: targetBranch.id })
                }
            );

            if (!switchResponse.ok) {
                throw new Error('分岐の切り替えに失敗しました');
            }

            // Reload messages
            await this.loadSessionMessages(this.currentConversationSessionId);
            this.showNotification(`分岐 ${targetBranchIndex + 1} に切り替えました`, 'info');

        } catch (error) {
            console.error('Failed to navigate branch:', error);
            this.showNotification(error.message || '分岐の切り替えに失敗しました', 'error');
        }
    }

    async loadSessionMessages(sessionId) {
        try {
            const response = await this.apiFetch(
                `/api/conversations/${sessionId}/active-messages`
            );

            if (response.ok) {
                const data = await response.json();
                if (data.messages) {
                    this.elements.chatMessages.innerHTML = '';

                    // Messages now include branch info from the server
                    for (const msg of data.messages) {
                        const entry = {
                            id: msg.id,
                            type: msg.role,
                            message: msg.content,
                            timestamp: msg.created_at ? new Date(msg.created_at).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' }) : '',
                            branch_count: msg.branch_count || 1,
                            branch_index: msg.branch_index || 0
                        };
                        this.addMessage(entry);
                    }
                    this.scrollToBottom();
                }
            }
        } catch (error) {
            console.error('Failed to load session messages:', error);
        }
    }

    clearChat() {
        this.elements.chatMessages.innerHTML = '';
        this.showNotification('チャット履歴をクリアしました', 'info');
    }

    updateVoiceStatus(status) {
        this.voiceStatus = status;

        if (status.ready) {
            if (status.recording) {
                this.elements.voiceStatusDot.className = 'w-2 h-2 rounded-full bg-red-500 flash-dot';
                this.elements.voiceStatusText.textContent = '録音中...';
            } else {
                this.elements.voiceStatusDot.className = 'w-2 h-2 rounded-full bg-green-500';
                this.elements.voiceStatusText.textContent = '音声認識準備完了';
            }
        } else {
            this.elements.voiceStatusDot.className = 'w-2 h-2 rounded-full bg-yellow-400 pulse-dot';
            this.elements.voiceStatusText.textContent = '音声認識準備中...';
        }

        this.updateMicLevel(status.rms);
    }

    updateMicLevel(rms) {
        const maxRMS = 500;
        const percentage = Math.min((rms / maxRMS) * 100, 100);

        this.elements.micLevelFill.style.width = percentage + '%';
        this.elements.micLevelValue.textContent = Math.round(rms);

        // Update color based on level
        if (percentage > 70) {
            this.elements.micLevelFill.className = 'h-full bg-red-500 transition-all duration-100 rounded-full';
        } else if (percentage > 30) {
            this.elements.micLevelFill.className = 'h-full bg-orange-500 transition-all duration-100 rounded-full';
        } else {
            this.elements.micLevelFill.className = 'h-full bg-green-500 transition-all duration-100 rounded-full';
        }
    }

    handleCharacterSwitch(data) {
        // プルダウンを更新
        const options = this.elements.characterSelect.options;
        for (let i = 0; i < options.length; i++) {
            if (options[i].value === data.new_character) {
                options[i].selected = true;
                break;
            }
        }

        // 通知を表示
        this.showNotification(
            `キャラクターが${data.old_character}から${data.new_character}に切り替わりました`,
            'info'
        );

        // システムメッセージとして履歴に追加
        const systemMessage = {
            type: 'system',
            message: `キャラクターが${data.old_character}から${data.new_character}に切り替わりました`,
            timestamp: data.timestamp
        };
        this.addMessage(systemMessage);

        console.log('Character switched:', data);
    }

    updateConnectionStatus(text, status) {
        this.elements.connectionText.textContent = text;

        if (status === 'connected') {
            this.elements.connectionDot.className = 'w-2 h-2 rounded-full bg-green-500';
        } else {
            this.elements.connectionDot.className = 'w-2 h-2 rounded-full bg-red-500';
        }
    }

    showNotification(message, type = 'info') {
        const notification = document.createElement('div');
        notification.className = `px-4 py-3 rounded-lg text-white font-medium shadow-lg animate-in slide-in-from-right duration-300`;

        switch (type) {
            case 'success':
                notification.classList.add('bg-gradient-to-r', 'from-green-500', 'to-green-600');
                break;
            case 'error':
                notification.classList.add('bg-gradient-to-r', 'from-red-500', 'to-red-600');
                break;
            case 'info':
                notification.classList.add('bg-gradient-to-r', 'from-blue-500', 'to-blue-600');
                break;
        }

        notification.textContent = message;
        this.elements.notifications.appendChild(notification);

        // Remove after 3 seconds
        setTimeout(() => {
            notification.classList.add('animate-out', 'slide-out-to-right');
            setTimeout(() => notification.remove(), 300);
        }, 3000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML.replace(/\n/g, '<br>');
    }

    scrollToBottom() {
        this.elements.chatMessages.scrollTop = this.elements.chatMessages.scrollHeight;
    }

    updateCharCount() {
        const count = this.elements.messageInput.value.length;
        this.elements.charCount.textContent = `${count}/2000`;

        if (count > 1800) {
            this.elements.charCount.className = 'text-red-400 font-semibold';
        } else {
            this.elements.charCount.className = 'text-gray-400';
        }
    }

    adjustTextareaHeight() {
        const textarea = this.elements.messageInput;
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    }

    async startVoiceStatusCheck() {
        setInterval(async () => {
            try {
                if (!this.isAuthenticated) return;
                const response = await this.apiFetch('/api/voice_status');
                if (!response.ok) {
                    throw new Error('音声ステータスの取得に失敗しました');
                }
                const status = await response.json();
                this.updateVoiceStatus(status);
            } catch (error) {
                console.error('Failed to fetch voice status:', error);
            }
        }, 100);
    }

    async switchCharacter(characterName) {
        try {
            this.showNotification(`キャラクターを${characterName}に切り替えています...`, 'info');

            const response = await this.apiFetch(`/api/character/${encodeURIComponent(characterName)}`, {
                method: 'POST'
            });

            if (response.ok) {
                const result = await response.json();
                this.showNotification(result.message, 'success');

                // Clear chat history
                this.elements.chatMessages.innerHTML = '';

                // Add system message
                this.addSystemMessage(`キャラクターが${characterName}に切り替わりました`);
            } else {
                const error = await response.json();
                this.showNotification(`切り替えエラー: ${error.detail}`, 'error');
                // Revert selection
                await this.fetchCharacters();
            }
        } catch (error) {
            console.error('Failed to switch character:', error);
            this.showNotification('キャラクター切り替えに失敗しました', 'error');
            // Revert selection
            await this.fetchCharacters();
        }
    }

    // ── Feedback Modal Methods ──────────────────────────────────────────
    openFeedbackModal(msgData) {
        this.feedbackModalData = msgData;

        // Create modal if it doesn't exist
        let modal = document.getElementById('feedbackModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'feedbackModal';
            modal.className = 'fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4';
            modal.innerHTML = `
                <div class="w-full max-w-md bg-gray-900 border border-gray-800 rounded-2xl shadow-2xl">
                    <div class="p-4 border-b border-gray-800">
                        <div class="flex items-center justify-between">
                            <div>
                                <p class="text-xs text-orange-400 font-semibold">FEEDBACK</p>
                                <h3 class="text-lg font-bold text-white mt-1">回答へのフィードバック</h3>
                            </div>
                            <button id="feedbackModalClose" class="p-2 text-gray-400 hover:text-white transition">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                                </svg>
                            </button>
                        </div>
                    </div>
                    <div class="p-4 space-y-4">
                        <div>
                            <label class="text-xs text-gray-400 block mb-2">対象の回答</label>
                            <div id="feedbackTargetMessage" class="text-sm text-gray-300 bg-gray-800 rounded-lg p-3 max-h-24 overflow-y-auto"></div>
                        </div>
                        <div>
                            <label class="text-xs text-gray-400 block mb-2">問題のカテゴリ</label>
                            <div class="grid grid-cols-2 gap-2">
                                <button class="feedback-category-btn px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-sm text-gray-300 transition" data-category="incorrect">❌ 不正確</button>
                                <button class="feedback-category-btn px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-sm text-gray-300 transition" data-category="incomplete">🔄 不完全</button>
                                <button class="feedback-category-btn px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-sm text-gray-300 transition" data-category="slow">🐢 遅い</button>
                                <button class="feedback-category-btn px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg text-sm text-gray-300 transition" data-category="other">📝 その他</button>
                            </div>
                        </div>
                        <div>
                            <label class="text-xs text-gray-400 block mb-2">コメント（任意）</label>
                            <textarea id="feedbackComment" placeholder="詳細を入力..." 
                                class="w-full px-3 py-2 bg-gray-800 text-gray-100 placeholder-gray-500 border border-gray-700 rounded-lg resize-none focus:outline-none focus:border-orange-500 focus:ring-2 focus:ring-orange-900"
                                rows="3"></textarea>
                        </div>
                    </div>
                    <div class="flex justify-end gap-2 p-4 border-t border-gray-800">
                        <button id="feedbackCancelBtn" class="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-gray-100 rounded-lg text-sm font-medium transition">
                            キャンセル
                        </button>
                        <button id="feedbackSubmitBtn" class="px-4 py-2 bg-orange-600 hover:bg-orange-500 text-white rounded-lg text-sm font-semibold transition" disabled>
                            送信
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            // Event listeners
            document.getElementById('feedbackModalClose').addEventListener('click', () => this.closeFeedbackModal());
            document.getElementById('feedbackCancelBtn').addEventListener('click', () => this.closeFeedbackModal());
            document.getElementById('feedbackSubmitBtn').addEventListener('click', () => this.submitFeedback());
            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeFeedbackModal();
            });

            // Category selection
            modal.querySelectorAll('.feedback-category-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    modal.querySelectorAll('.feedback-category-btn').forEach(b => {
                        b.classList.remove('border-orange-500', 'bg-orange-900/30');
                        b.classList.add('border-gray-700');
                    });
                    btn.classList.remove('border-gray-700');
                    btn.classList.add('border-orange-500', 'bg-orange-900/30');
                    this.feedbackModalData.category = btn.dataset.category;
                    document.getElementById('feedbackSubmitBtn').disabled = false;
                });
            });
        }

        // Populate modal
        document.getElementById('feedbackTargetMessage').textContent = msgData.message.replace(/<br>/g, '\n').replace(/&quot;/g, '"');
        document.getElementById('feedbackComment').value = '';

        // Reset category selection
        modal.querySelectorAll('.feedback-category-btn').forEach(b => {
            b.classList.remove('border-orange-500', 'bg-orange-900/30');
            b.classList.add('border-gray-700');
        });
        document.getElementById('feedbackSubmitBtn').disabled = true;
        this.feedbackModalData.category = null;

        modal.classList.remove('hidden');
    }

    closeFeedbackModal() {
        const modal = document.getElementById('feedbackModal');
        if (modal) {
            modal.classList.add('hidden');
        }
        this.feedbackModalData = null;
    }

    async submitFeedback() {
        if (!this.feedbackModalData || !this.feedbackModalData.category) {
            this.showNotification('カテゴリを選択してください', 'error');
            return;
        }

        const submitBtn = document.getElementById('feedbackSubmitBtn');
        submitBtn.disabled = true;
        submitBtn.textContent = '送信中...';

        try {
            const response = await this.apiFetch('/api/feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: this.feedbackModalData.message,
                    character: this.feedbackModalData.character,
                    category: this.feedbackModalData.category,
                    comment: document.getElementById('feedbackComment').value || null,
                    session_id: this.sessionId || null
                })
            });

            if (response.ok) {
                const result = await response.json();
                this.showNotification(result.message || 'フィードバックを送信しました', 'success');
                this.closeFeedbackModal();
            } else {
                const error = await response.json();
                throw new Error(error.detail || 'フィードバックの送信に失敗しました');
            }
        } catch (error) {
            console.error('Failed to submit feedback:', error);
            this.showNotification(error.message || 'フィードバックの送信に失敗しました', 'error');
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = '送信';
        }
    }

    setupEventListeners() {
        // Send button
        this.elements.sendBtn.addEventListener('click', () => this.sendMessage());

        // Note: New conversation button is handled by conversation-history.js

        // Character select
        this.elements.characterSelect.addEventListener('change', (e) => {
            this.switchCharacter(e.target.value);
        });

        // Message input
        this.elements.messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                if (e.ctrlKey) {
                    // Send message with Ctrl+Enter
                    e.preventDefault();
                    this.sendMessage();
                }
                // Otherwise allow default (new line) for Enter alone
            }
        });

        // Global keyboard shortcuts for LLM mode switching
        document.addEventListener('keydown', (e) => {
            if (e.altKey) {
                if (e.key === '1') {
                    e.preventDefault();
                    this.setLlmMode('fast');
                } else if (e.key === '2') {
                    e.preventDefault();
                    this.setLlmMode('thinking');
                }
            }
        });

        // Global shortcut: focus message input with Ctrl+/
        document.addEventListener('keydown', (event) => {
            if (!this.elements.messageInput) return;
            if (!event.ctrlKey || event.altKey || event.metaKey) return;
            const isSlashKey = event.code === 'Slash' || event.key === '/' || event.key === '?';
            if (!isSlashKey) return;
            event.preventDefault();
            this.elements.messageInput.focus();
            const length = this.elements.messageInput.value.length;
            this.elements.messageInput.setSelectionRange(length, length);
        });

        this.elements.messageInput.addEventListener('input', () => {
            this.updateCharCount();
            this.adjustTextareaHeight();
        });

        // Image input event listeners
        if (this.elements.imageInput) {
            this.elements.imageInput.addEventListener('change', (e) => this.handleImageSelect(e));
        }

        if (this.elements.removeImageBtn) {
            this.elements.removeImageBtn.addEventListener('click', () => this.clearImagePreview());
        }

        // Drag & Drop support for images and documents on message input
        if (this.elements.messageInput) {
            this.elements.messageInput.addEventListener('dragover', (e) => this.handleDragOver(e));
            this.elements.messageInput.addEventListener('dragleave', (e) => this.handleDragLeave(e));
            this.elements.messageInput.addEventListener('drop', (e) => this.handleDrop(e));

            // Clipboard paste support for images
            this.elements.messageInput.addEventListener('paste', (e) => this.handlePaste(e));
        }

        // Drag & Drop support on the chat pane (entire chat area)
        if (this.elements.chatPane) {
            this.elements.chatPane.addEventListener('dragover', (e) => this.handleChatPaneDragOver(e));
            this.elements.chatPane.addEventListener('dragleave', (e) => this.handleChatPaneDragLeave(e));
            this.elements.chatPane.addEventListener('drop', (e) => this.handleDrop(e));
        }

        if (this.elements.commandLauncherBtn) {
            this.elements.commandLauncherBtn.addEventListener('click', () => this.openCommandOverlay());
        }

        if (this.elements.commandOverlayClose) {
            this.elements.commandOverlayClose.addEventListener('click', () => this.closeCommandOverlay());
        }

        if (this.elements.commandOverlayRefresh) {
            this.elements.commandOverlayRefresh.addEventListener('click', () => {
                this.fetchMobileCommands();
                this.fetchCrawlerStatus();
            });
        }

        if (this.elements.crawlerStatusRefresh) {
            this.elements.crawlerStatusRefresh.addEventListener('click', () => this.fetchCrawlerStatus());
        }

        if (this.elements.commandOverlay) {
            this.elements.commandOverlay.addEventListener('click', (event) => {
                if (event.target === this.elements.commandOverlay) {
                    this.closeCommandOverlay();
                }
            });
        }

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && this.commandOverlayVisible) {
                this.closeCommandOverlay();
            }
        });

        if (this.elements.modeButtons && this.elements.modeButtons.length) {
            this.elements.modeButtons.forEach(button => {
                button.addEventListener('click', () => {
                    const mode = button.dataset.modeOption;
                    this.handleModeSwitchClick(mode);
                });
            });
        }

        if (this.elements.loginForm) {
            this.elements.loginForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleLogin();
            });
        }

        // Document management event listeners
        if (this.elements.documentNewBtn) {
            this.elements.documentNewBtn.addEventListener('click', () => this.openDocumentEditor());
        }

        if (this.elements.documentRefreshBtn) {
            this.elements.documentRefreshBtn.addEventListener('click', () => this.fetchDocuments());
        }

        if (this.elements.documentEditorClose) {
            this.elements.documentEditorClose.addEventListener('click', () => this.closeDocumentEditor());
        }

        if (this.elements.documentEditorCancel) {
            this.elements.documentEditorCancel.addEventListener('click', () => this.closeDocumentEditor());
        }

        if (this.elements.documentEditorSave) {
            this.elements.documentEditorSave.addEventListener('click', () => this.saveDocument());
        }

        if (this.elements.documentEditorDelete) {
            this.elements.documentEditorDelete.addEventListener('click', () => this.deleteDocument());
        }

        if (this.elements.documentEditorModal) {
            this.elements.documentEditorModal.addEventListener('click', (event) => {
                if (event.target === this.elements.documentEditorModal) {
                    this.closeDocumentEditor();
                }
            });
        }

        // Office File Upload event listeners
        if (this.elements.documentUploadBtn && this.elements.officeFileInput) {
            this.elements.documentUploadBtn.addEventListener('click', () => {
                this.elements.officeFileInput.click();
            });

            this.elements.officeFileInput.addEventListener('change', (event) => {
                const file = event.target.files[0];
                if (file) {
                    this.uploadOfficeFile(file);
                    event.target.value = '';  // Reset for re-upload
                }
            });
        }

        if (this.elements.officeUploadClose) {
            this.elements.officeUploadClose.addEventListener('click', () => this.hideOfficeUploadResult());
        }

        if (this.elements.officeUploadInsert) {
            this.elements.officeUploadInsert.addEventListener('click', () => this.insertOfficeContentToChat());
        }

        if (this.elements.officeUploadSave) {
            this.elements.officeUploadSave.addEventListener('click', () => this.saveOfficeContentAsDocument());
        }

        // Also close document editor on Escape
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                if (this.elements.mediaViewerModal && !this.elements.mediaViewerModal.classList.contains('hidden')) {
                    this.closeMediaViewer();
                } else if (this.elements.documentEditorModal && !this.elements.documentEditorModal.classList.contains('hidden')) {
                    this.closeDocumentEditor();
                }
            }
            // Arrow key navigation for media viewer
            if (this.elements.mediaViewerModal && !this.elements.mediaViewerModal.classList.contains('hidden')) {
                if (event.key === 'ArrowLeft') {
                    event.preventDefault();
                    this.navigateViewerPrev();
                } else if (event.key === 'ArrowRight') {
                    event.preventDefault();
                    this.navigateViewerNext();
                }
            }
        });

        // User Files event listeners
        if (this.elements.userFileUploadBtn && this.elements.userFileInput) {
            this.elements.userFileUploadBtn.addEventListener('click', () => {
                this.elements.userFileInput.click();
            });

            this.elements.userFileInput.addEventListener('change', (event) => {
                const file = event.target.files[0];
                if (file) {
                    this.uploadUserFile(file);
                    event.target.value = '';  // Reset for re-upload
                }
            });
        }

        if (this.elements.userFileRefreshBtn) {
            this.elements.userFileRefreshBtn.addEventListener('click', () => this.fetchUserFiles());
        }

        // Media browser event listeners
        if (this.elements.mediaBackBtn) {
            this.elements.mediaBackBtn.addEventListener('click', () => this.navigateMediaBack());
        }

        if (this.elements.mediaHomeBtn) {
            this.elements.mediaHomeBtn.addEventListener('click', () => this.navigateMediaHome());
        }

        if (this.elements.mediaGoUpBtn) {
            this.elements.mediaGoUpBtn.addEventListener('click', () => this.navigateMediaUp());
        }

        if (this.elements.mediaAddBookmarkBtn) {
            this.elements.mediaAddBookmarkBtn.addEventListener('click', () => this.addMediaBookmark());
        }

        // Media view mode toggle
        if (this.elements.mediaViewModeThumbnail) {
            this.elements.mediaViewModeThumbnail.addEventListener('click', () => this.setMediaViewMode('thumbnail'));
        }

        if (this.elements.mediaViewModeList) {
            this.elements.mediaViewModeList.addEventListener('click', () => this.setMediaViewMode('list'));
        }

        // Media viewer prev/next buttons
        if (this.elements.mediaViewerClose) {
            this.elements.mediaViewerClose.addEventListener('click', () => this.closeMediaViewer());
        }

        if (this.elements.mediaViewerModal) {
            this.elements.mediaViewerModal.addEventListener('click', (event) => {
                if (event.target === this.elements.mediaViewerModal) {
                    this.closeMediaViewer();
                }
            });
        }

        if (this.elements.mediaViewerPrev) {
            this.elements.mediaViewerPrev.addEventListener('click', (e) => {
                e.stopPropagation();
                this.navigateViewerPrev();
            });
        }

        if (this.elements.mediaViewerNext) {
            this.elements.mediaViewerNext.addEventListener('click', (e) => {
                e.stopPropagation();
                this.navigateViewerNext();
            });
        }

        // External LLM permission modal event listeners
        if (this.elements.permissionApproveBtn) {
            this.elements.permissionApproveBtn.addEventListener('click', () => {
                this.sendExternalLLMPermissionResponse(true);
            });
        }

        if (this.elements.permissionDenyBtn) {
            this.elements.permissionDenyBtn.addEventListener('click', () => {
                this.sendExternalLLMPermissionResponse(false);
            });
        }

        // Settings panel event listeners
        this.setupSettingsEventListeners();

        // User management event listeners
        this.setupUserManagementEventListeners();

        // Listen for conversation changes from conversation-history.js
        document.addEventListener('conversationChanged', (event) => {
            const { sessionId } = event.detail;
            this.setCurrentConversationSessionId(sessionId);
        });
    }

    // ── User Management ──────────────────────────────────────────────────
    setupUserManagementEventListeners() {
        // Password change button
        const openPasswordBtn = document.getElementById('openPasswordChangeBtn');
        if (openPasswordBtn) {
            openPasswordBtn.addEventListener('click', () => this.openPasswordChangeModal());
        }

        // User management button (admin)
        const openUserMgmtBtn = document.getElementById('openUserManagementBtn');
        if (openUserMgmtBtn) {
            openUserMgmtBtn.addEventListener('click', () => this.openUserManagementModal());
        }

        // Project management button
        const openProjectMgrBtn = document.getElementById('openProjectManagerBtn');
        if (openProjectMgrBtn) {
            openProjectMgrBtn.addEventListener('click', () => {
                if (!window.projectManager) {
                    window.projectManager = new ProjectManager();
                }
                window.projectManager.open();
            });
        }

        // Logout button
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) {
            logoutBtn.addEventListener('click', () => this.handleLogout());
        }

        // Password change modal
        const passwordChangeForm = document.getElementById('passwordChangeForm');
        if (passwordChangeForm) {
            passwordChangeForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handlePasswordChange();
            });
        }

        const passwordChangeCancelBtn = document.getElementById('passwordChangeCancelBtn');
        if (passwordChangeCancelBtn) {
            passwordChangeCancelBtn.addEventListener('click', () => this.closePasswordChangeModal());
        }

        // User management modal
        const userMgmtCloseBtn = document.getElementById('userManagementCloseBtn');
        if (userMgmtCloseBtn) {
            userMgmtCloseBtn.addEventListener('click', () => this.closeUserManagementModal());
        }

        const addUserBtn = document.getElementById('addUserBtn');
        if (addUserBtn) {
            addUserBtn.addEventListener('click', () => this.openUserEditModal());
        }

        // User edit modal
        const userEditForm = document.getElementById('userEditForm');
        if (userEditForm) {
            userEditForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handleUserSave();
            });
        }

        const userEditCancelBtn = document.getElementById('userEditCancelBtn');
        if (userEditCancelBtn) {
            userEditCancelBtn.addEventListener('click', () => this.closeUserEditModal());
        }

        // CSV Export/Import buttons
        const exportUsersBtn = document.getElementById('exportUsersBtn');
        if (exportUsersBtn) {
            exportUsersBtn.addEventListener('click', () => this.exportUsersCSV());
        }

        const importUsersBtn = document.getElementById('importUsersBtn');
        const userCsvFileInput = document.getElementById('userCsvFileInput');
        if (importUsersBtn && userCsvFileInput) {
            importUsersBtn.addEventListener('click', () => userCsvFileInput.click());
            userCsvFileInput.addEventListener('change', (e) => {
                const file = e.target.files[0];
                if (file) {
                    this.importUsersCSV(file);
                    e.target.value = '';  // Reset for re-upload
                }
            });
        }
    }

    openPasswordChangeModal() {
        const modal = document.getElementById('passwordChangeModal');
        if (modal) {
            modal.classList.remove('hidden');
            document.getElementById('currentPassword').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('confirmPassword').value = '';
            document.getElementById('passwordChangeError').classList.add('hidden');
            document.getElementById('passwordChangeSuccess').classList.add('hidden');
        }
    }

    closePasswordChangeModal() {
        const modal = document.getElementById('passwordChangeModal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    async handlePasswordChange() {
        const currentPassword = document.getElementById('currentPassword').value;
        const newPassword = document.getElementById('newPassword').value;
        const confirmPassword = document.getElementById('confirmPassword').value;
        const errorEl = document.getElementById('passwordChangeError');
        const successEl = document.getElementById('passwordChangeSuccess');

        errorEl.classList.add('hidden');
        successEl.classList.add('hidden');

        if (!currentPassword || !newPassword) {
            errorEl.textContent = 'すべてのフィールドを入力してください';
            errorEl.classList.remove('hidden');
            return;
        }

        if (newPassword !== confirmPassword) {
            errorEl.textContent = '新しいパスワードが一致しません';
            errorEl.classList.remove('hidden');
            return;
        }

        if (newPassword.length < 6) {
            errorEl.textContent = 'パスワードは6文字以上にしてください';
            errorEl.classList.remove('hidden');
            return;
        }

        try {
            const response = await this.apiFetch('/api/auth/change-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    current_password: currentPassword,
                    new_password: newPassword
                })
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'パスワード変更に失敗しました');
            }

            successEl.textContent = 'パスワードを変更しました';
            successEl.classList.remove('hidden');
            this.showNotification('パスワードを変更しました', 'success');

            setTimeout(() => this.closePasswordChangeModal(), 1500);
        } catch (error) {
            console.error('Password change failed:', error);
            errorEl.textContent = error.message || 'パスワード変更に失敗しました';
            errorEl.classList.remove('hidden');
        }
    }

    openUserManagementModal() {
        const modal = document.getElementById('userManagementModal');
        if (modal) {
            modal.classList.remove('hidden');
            this.fetchUserList();
        }
    }

    closeUserManagementModal() {
        const modal = document.getElementById('userManagementModal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    async fetchUserList() {
        const userList = document.getElementById('userList');
        if (!userList) return;

        userList.innerHTML = '<p class="text-xs text-gray-500 text-center py-4">読み込み中...</p>';

        try {
            const response = await this.apiFetch('/api/users?include_inactive=true');
            if (!response.ok) {
                throw new Error('ユーザー一覧の取得に失敗しました');
            }

            const data = await response.json();
            this.renderUserList(data.users || []);
        } catch (error) {
            console.error('Failed to fetch users:', error);
            userList.innerHTML = '<p class="text-xs text-red-400 text-center py-4">ユーザー一覧の取得に失敗しました</p>';
        }
    }

    renderUserList(users) {
        const userList = document.getElementById('userList');
        if (!userList) return;

        if (!users.length) {
            userList.innerHTML = '<p class="text-xs text-gray-500 text-center py-4">ユーザーがいません</p>';
            return;
        }

        userList.innerHTML = users.map(user => `
            <div class="flex items-center justify-between bg-gray-800/50 rounded-xl p-3 border border-gray-700">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-full bg-gray-700 flex items-center justify-center text-gray-300 font-bold">
                        ${this.escapeHtml((user.display_name || user.username || '?')[0].toUpperCase())}
                    </div>
                    <div>
                        <p class="text-sm font-medium text-gray-100">${this.escapeHtml(user.display_name || user.username)}</p>
                        <p class="text-xs text-gray-400">@${this.escapeHtml(user.username)} • ${user.role === 'admin' ? '管理者' : 'ユーザー'} ${user.is_active ? '' : '(無効)'}</p>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button class="user-edit-btn px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg transition" data-user-id="${user.id}">編集</button>
                    <button class="user-delete-btn px-2 py-1 text-xs bg-red-600/20 hover:bg-red-600/40 text-red-400 rounded-lg transition" data-user-id="${user.id}" data-username="${this.escapeHtml(user.username)}">削除</button>
                </div>
            </div>
        `).join('');

        // Add event listeners
        userList.querySelectorAll('.user-edit-btn').forEach(btn => {
            btn.addEventListener('click', () => this.openUserEditModal(btn.dataset.userId));
        });

        userList.querySelectorAll('.user-delete-btn').forEach(btn => {
            btn.addEventListener('click', () => this.confirmDeleteUser(btn.dataset.userId, btn.dataset.username));
        });
    }

    openUserEditModal(userId = null) {
        const modal = document.getElementById('userEditModal');
        const title = document.getElementById('userEditTitle');
        const usernameInput = document.getElementById('userEditUsername');
        const passwordField = document.getElementById('userEditPasswordField');
        const idInput = document.getElementById('userEditId');
        const errorEl = document.getElementById('userEditError');
        const ragSection = document.getElementById('userRagSection');

        if (!modal) return;

        // Reset form
        document.getElementById('userEditForm').reset();
        errorEl.classList.add('hidden');
        idInput.value = '';

        // RAGセクションをリセット
        if (ragSection) {
            ragSection.classList.add('hidden');
            const ragList = document.getElementById('userRagList');
            if (ragList) ragList.innerHTML = '';
        }

        if (userId) {
            title.textContent = 'ユーザー編集';
            usernameInput.disabled = true;
            this.loadUserForEdit(userId);
        } else {
            title.textContent = '新規ユーザー';
            usernameInput.disabled = false;
            document.getElementById('userEditActive').checked = true;
            document.getElementById('userEditRole').value = 'user';
        }

        modal.classList.remove('hidden');
    }

    async loadUserForEdit(userId) {
        try {
            const response = await this.apiFetch(`/api/users/${userId}`);
            if (!response.ok) throw new Error('ユーザー情報の取得に失敗しました');

            const data = await response.json();
            const user = data.user;

            document.getElementById('userEditId').value = user.id;
            document.getElementById('userEditUsername').value = user.username || '';
            document.getElementById('userEditDisplayName').value = user.display_name || '';
            document.getElementById('userEditEmail').value = user.email || '';
            document.getElementById('userEditRole').value = user.role || 'user';
            document.getElementById('userEditActive').checked = user.is_active !== false;

            // RAGコレクション紐づけセクションを表示・読み込み
            this.loadUserRagCollections(userId);
        } catch (error) {
            console.error('Failed to load user:', error);
            this.showNotification('ユーザー情報の取得に失敗しました', 'error');
        }
    }

    async loadUserRagCollections(userId) {
        const ragSection = document.getElementById('userRagSection');
        const ragList = document.getElementById('userRagList');
        const addSelect = document.getElementById('userRagAddSelect');
        const addPermSelect = document.getElementById('userRagAddPerm');
        const addBtn = document.getElementById('userRagAddBtn');

        if (!ragSection || !ragList) return;

        try {
            // ユーザーの紐づけ済みコレクションと全コレクション一覧を並行取得
            const [linkedRes, allRes] = await Promise.all([
                this.apiFetch(`/api/users/${userId}/rag-collections`),
                this.apiFetch('/api/rag/collections'),
            ]);

            const linkedData = await linkedRes.json();
            const allData = await allRes.json();

            const linkedCollections = linkedData.success ? (linkedData.collections || []) : [];
            const allCollections = allData.success ? (allData.collections || []) : [];
            const linkedIds = new Set(linkedCollections.map(c => c.id));

            // 紐づけ済み一覧を表示
            ragList.innerHTML = linkedCollections.length === 0
                ? '<p class="text-xs text-gray-500">紐づけなし</p>'
                : linkedCollections.map(c => `
                    <div class="flex items-center justify-between gap-2 px-2 py-1 bg-gray-800 rounded-lg">
                        <span class="text-xs text-gray-200 truncate flex-1" title="${this.escapeHtml(c.name)}">${this.escapeHtml(c.name)}</span>
                        <select class="user-rag-perm-select px-1 py-0.5 bg-gray-700 text-gray-200 rounded text-xs border border-gray-600"
                                data-collection-id="${c.id}" data-user-id="${userId}">
                            <option value="read" ${c.permission === 'read' ? 'selected' : ''}>read</option>
                            <option value="write" ${c.permission === 'write' ? 'selected' : ''}>write</option>
                        </select>
                        <button type="button" class="user-rag-unlink-btn text-red-400 hover:text-red-300 text-xs px-1"
                                data-collection-id="${c.id}" data-user-id="${userId}" title="紐づけ解除">✕</button>
                    </div>
                `).join('');

            // 未紐づけコレクションをセレクトに追加
            if (addSelect) {
                const available = allCollections.filter(c => !linkedIds.has(c.id));
                addSelect.innerHTML = '<option value="">-- コレクション選択 --</option>'
                    + available.map(c =>
                        `<option value="${c.id}">${this.escapeHtml(c.name)}</option>`
                    ).join('');
            }

            // イベントリスナー: 紐づけ解除
            ragList.querySelectorAll('.user-rag-unlink-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const colId = btn.dataset.collectionId;
                    const uid = btn.dataset.userId;
                    try {
                        const res = await this.apiFetch(`/api/rag/collections/${colId}/users/${uid}`, { method: 'DELETE' });
                        if (res.ok) {
                            this.loadUserRagCollections(uid);
                        }
                    } catch (e) {
                        console.error('Failed to unlink:', e);
                    }
                });
            });

            // イベントリスナー: permission変更
            ragList.querySelectorAll('.user-rag-perm-select').forEach(sel => {
                sel.addEventListener('change', async () => {
                    const colId = sel.dataset.collectionId;
                    const uid = sel.dataset.userId;
                    try {
                        await this.apiFetch(`/api/rag/collections/${colId}/users`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ user_id: uid, permission: sel.value }),
                        });
                    } catch (e) {
                        console.error('Failed to update permission:', e);
                    }
                });
            });

            // イベントリスナー: 追加ボタン（既存リスナーの重複回避）
            if (addBtn) {
                const newBtn = addBtn.cloneNode(true);
                addBtn.parentNode.replaceChild(newBtn, addBtn);
                newBtn.addEventListener('click', async () => {
                    const colId = addSelect.value;
                    if (!colId) return;
                    try {
                        const res = await this.apiFetch(`/api/rag/collections/${colId}/users`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ user_id: userId, permission: addPermSelect.value }),
                        });
                        if (res.ok) {
                            this.loadUserRagCollections(userId);
                        }
                    } catch (e) {
                        console.error('Failed to link:', e);
                    }
                });
            }

            ragSection.classList.remove('hidden');
        } catch (error) {
            console.error('Failed to load user RAG collections:', error);
        }
    }

    closeUserEditModal() {
        const modal = document.getElementById('userEditModal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    async handleUserSave() {
        const idInput = document.getElementById('userEditId');
        const username = document.getElementById('userEditUsername').value.trim();
        const password = document.getElementById('userEditPassword').value;
        const displayName = document.getElementById('userEditDisplayName').value.trim();
        const email = document.getElementById('userEditEmail').value.trim();
        const role = document.getElementById('userEditRole').value;
        const isActive = document.getElementById('userEditActive').checked;
        const errorEl = document.getElementById('userEditError');

        errorEl.classList.add('hidden');

        const isEdit = !!idInput.value;

        if (!isEdit && !username) {
            errorEl.textContent = 'ユーザー名を入力してください';
            errorEl.classList.remove('hidden');
            return;
        }

        if (!isEdit && !password) {
            errorEl.textContent = 'パスワードを入力してください';
            errorEl.classList.remove('hidden');
            return;
        }

        try {
            let response;
            if (isEdit) {
                // Update user
                const updateData = {
                    display_name: displayName || null,
                    email: email || null,
                    role: role,
                    is_active: isActive
                };

                response = await this.apiFetch(`/api/users/${idInput.value}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updateData)
                });

                // Update password if provided
                if (password) {
                    await this.apiFetch(`/api/users/${idInput.value}/change-password`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ new_password: password })
                    });
                }
            } else {
                // Create user
                response = await this.apiFetch('/api/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username: username,
                        password: password,
                        display_name: displayName || null,
                        email: email || null,
                        role: role
                    })
                });
            }

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || '保存に失敗しました');
            }

            this.showNotification(isEdit ? 'ユーザーを更新しました' : 'ユーザーを作成しました', 'success');
            this.closeUserEditModal();
            this.fetchUserList();
        } catch (error) {
            console.error('Failed to save user:', error);
            errorEl.textContent = error.message || '保存に失敗しました';
            errorEl.classList.remove('hidden');
        }
    }

    async confirmDeleteUser(userId, username) {
        if (!confirm(`ユーザー「${username}」を削除しますか？この操作は取り消せません。`)) {
            return;
        }

        try {
            const response = await this.apiFetch(`/api/users/${userId}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'ユーザーの削除に失敗しました');
            }

            this.showNotification('ユーザーを削除しました', 'success');
            this.fetchUserList();
        } catch (error) {
            console.error('Failed to delete user:', error);
            this.showNotification(error.message || 'ユーザーの削除に失敗しました', 'error');
        }
    }

    async exportUsersCSV() {
        try {
            const response = await this.apiFetch('/api/users/export');

            if (!response.ok) {
                throw new Error('エクスポートに失敗しました');
            }

            // Get filename from Content-Disposition header or use default
            const contentDisposition = response.headers.get('Content-Disposition');
            let filename = 'users.csv';
            if (contentDisposition) {
                const match = contentDisposition.match(/filename=(.+)/);
                if (match) {
                    filename = match[1].replace(/"/g, '');
                }
            }

            // Download the file
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);

            this.showNotification('ユーザーをエクスポートしました', 'success');
        } catch (error) {
            console.error('Failed to export users:', error);
            this.showNotification(error.message || 'エクスポートに失敗しました', 'error');
        }
    }

    async importUsersCSV(file) {
        try {
            const formData = new FormData();
            formData.append('file', file);

            this.showNotification('インポート中...', 'info');

            const response = await fetch('/api/users/import', {
                method: 'POST',
                body: formData,
                credentials: 'include'
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'インポートに失敗しました');
            }

            const result = await response.json();

            // Show result notification
            let message = result.message || `${result.created}件作成, ${result.updated}件更新`;
            if (result.errors && result.errors.length > 0) {
                message += ` (エラー: ${result.errors.length}件)`;
            }

            this.showNotification(message, 'success');

            // Refresh user list
            this.fetchUserList();

            // Show errors in console if any
            if (result.errors && result.errors.length > 0) {
                console.warn('Import errors:', result.errors);
            }
        } catch (error) {
            console.error('Failed to import users:', error);
            this.showNotification(error.message || 'インポートに失敗しました', 'error');
        }
    }

    async handleLogout() {
        if (!confirm('ログアウトしますか？')) {
            return;
        }

        try {
            const response = await this.apiFetch('/api/auth/logout', {
                method: 'POST'
            });

            if (response.ok) {
                this.showNotification('ログアウトしました', 'success');

                // Close command overlay if open
                this.closeCommandOverlay();

                // Show login overlay
                if (this.elements.loginOverlay) {
                    this.elements.loginOverlay.classList.remove('hidden');
                }

                // Clear chat messages
                if (this.elements.chatMessages) {
                    this.elements.chatMessages.innerHTML = '';
                }

                // Clear any stored session data
                this.sessionId = null;
            } else {
                throw new Error('ログアウトに失敗しました');
            }
        } catch (error) {
            console.error('Failed to logout:', error);
            this.showNotification(error.message || 'ログアウトに失敗しました', 'error');
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // MUSIC PLAYER
    // ══════════════════════════════════════════════════════════════════════════

    setupMusicPlayerListeners() {
        const mp = this.musicPlayer;
        const el = this.elements;

        // Audio event listeners
        mp.audio.addEventListener('timeupdate', () => this.updateMusicProgress());
        mp.audio.addEventListener('loadedmetadata', () => this.updateMusicDuration());
        mp.audio.addEventListener('ended', () => this.handleTrackEnded());
        mp.audio.addEventListener('play', () => this.updatePlayPauseButtons(true));
        mp.audio.addEventListener('pause', () => this.updatePlayPauseButtons(false));
        mp.audio.addEventListener('error', (e) => {
            console.error('Audio playback error:', e);
            this.showNotification('音楽の再生に失敗しました', 'error');
        });

        this.initMusicVolume();
        if (el.miniPlayerVolume) {
            el.miniPlayerVolume.addEventListener('input', (event) => this.handleMusicVolumeInput(event));
        }
        if (el.musicPlayerVolume) {
            el.musicPlayerVolume.addEventListener('input', (event) => this.handleMusicVolumeInput(event));
        }

        // Mini player controls
        if (el.miniPlayerPlayPause) {
            el.miniPlayerPlayPause.addEventListener('click', () => this.toggleMusicPlayPause());
        }
        if (el.miniPlayerPrev) {
            el.miniPlayerPrev.addEventListener('click', () => this.playPrevTrack());
        }
        if (el.miniPlayerNext) {
            el.miniPlayerNext.addEventListener('click', () => this.playNextTrack());
        }
        if (el.miniPlayerClose) {
            el.miniPlayerClose.addEventListener('click', () => this.stopMusic());
        }
        if (el.miniPlayerExpand) {
            el.miniPlayerExpand.addEventListener('click', () => this.openMusicPlayerModal());
        }
        if (el.miniPlayerProgress) {
            el.miniPlayerProgress.addEventListener('click', (e) => this.seekMusic(e, el.miniPlayerProgress));
        }

        // Full player controls
        if (el.musicPlayerClose) {
            el.musicPlayerClose.addEventListener('click', () => this.closeMusicPlayerModal());
        }
        if (el.musicPlayerPlayPause) {
            el.musicPlayerPlayPause.addEventListener('click', () => this.toggleMusicPlayPause());
        }
        if (el.musicPlayerPrev) {
            el.musicPlayerPrev.addEventListener('click', () => this.playPrevTrack());
        }
        if (el.musicPlayerNext) {
            el.musicPlayerNext.addEventListener('click', () => this.playNextTrack());
        }
        if (el.musicPlayerShuffle) {
            el.musicPlayerShuffle.addEventListener('click', () => this.toggleShuffle());
        }
        if (el.musicPlayerRepeat) {
            el.musicPlayerRepeat.addEventListener('click', () => this.toggleRepeat());
        }
        if (el.musicPlayerProgress) {
            el.musicPlayerProgress.addEventListener('click', (e) => this.seekMusic(e, el.musicPlayerProgress));
        }
        if (el.musicPlayerPlaylist) {
            el.musicPlayerPlaylist.addEventListener('click', () => this.togglePlaylistPanel());
        }

        this.setupMusicShortcuts();

        // Setup Media Session API for background playback controls
        this.setupMediaSession();
    }

    initMusicVolume() {
        const volume = this.loadMusicVolume();
        this.setMusicVolume(volume);
    }

    loadMusicVolume() {
        const defaultVolume = 0.8;
        try {
            const stored = localStorage.getItem('musicPlayerVolume');
            if (stored === null) return defaultVolume;
            const value = parseFloat(stored);
            if (Number.isNaN(value)) return defaultVolume;
            return Math.max(0, Math.min(1, value));
        } catch (error) {
            return defaultVolume;
        }
    }

    setMusicVolume(volume) {
        const mp = this.musicPlayer;
        const clamped = Math.max(0, Math.min(1, volume));
        mp.volume = clamped;
        mp.audio.volume = clamped;
        const percent = Math.round(clamped * 100);

        if (this.elements.miniPlayerVolume) {
            this.elements.miniPlayerVolume.value = percent;
        }
        if (this.elements.musicPlayerVolume) {
            this.elements.musicPlayerVolume.value = percent;
        }

        try {
            localStorage.setItem('musicPlayerVolume', clamped.toString());
        } catch (error) {
        }
    }

    handleMusicVolumeInput(event) {
        const target = event.target;
        const value = Number(target?.value);
        if (Number.isNaN(value)) return;
        this.setMusicVolume(value / 100);
    }

    setupMusicShortcuts() {
        document.addEventListener('keydown', (event) => {
            if (this.shouldIgnoreMusicShortcut(event)) return;
            if (!this.musicPlayer || this.musicPlayer.playlist.length === 0) return;

            const key = event.key;
            const code = event.code;

            if (code === 'Space' || key === ' ' || key === 'Spacebar') {
                event.preventDefault();
                this.toggleMusicPlayPause();
            } else if (code === 'PageDown' || key === 'PageDown') {
                event.preventDefault();
                this.playNextTrack();
            } else if (code === 'PageUp' || key === 'PageUp') {
                event.preventDefault();
                this.playPrevTrack();
            }
        });
    }

    shouldIgnoreMusicShortcut(event) {
        if (event.defaultPrevented) return true;
        if (event.altKey || event.ctrlKey || event.metaKey) return true;

        const target = event.target;
        if (target?.closest) {
            if (target.closest('input, textarea, select, button, a, [contenteditable="true"]')) {
                return true;
            }
        }

        return false;
    }

    playAudioFile(file, fileList = null, fileIndex = -1) {
        const mp = this.musicPlayer;

        // Set up playlist
        if (fileList) {
            // Filter to audio files only
            mp.playlist = fileList.filter(f => f.type === 'audio');
            const audioIndex = mp.playlist.findIndex(f => f.path === file.path);
            mp.currentIndex = audioIndex >= 0 ? audioIndex : 0;

            // Initialize shuffle order
            mp.originalOrder = mp.playlist.map((_, i) => i);
            this._generateShuffleOrder();
        } else {
            mp.playlist = [file];
            mp.currentIndex = 0;
            mp.originalOrder = [0];
            mp.shuffledOrder = [0];
        }

        this.loadAndPlayTrack(mp.currentIndex);
        this.showMiniPlayer();
    }

    loadAndPlayTrack(index) {
        const mp = this.musicPlayer;
        if (index < 0 || index >= mp.playlist.length) return;

        mp.currentIndex = index;
        const file = mp.playlist[index];

        // Get audio URL (same as video for Android compatibility)
        const audioUrl = this._getAudioUrl(file.path);
        mp.audio.src = audioUrl;
        mp.audio.load();

        mp.audio.play().then(() => {
            mp.isPlaying = true;
            this.updateMusicPlayerUI(file);
            this.updateMediaSessionMetadata(file);
        }).catch(err => {
            console.error('Failed to play audio:', err);
            this.showNotification('再生を開始できませんでした', 'error');
        });
    }

    _getAudioUrl(path) {
        // Unlike video, audio doesn't have Android compatibility issues with HTTPS
        // So we always use the same origin to avoid Mixed Content errors
        // Video uses HTTP port for Android, but audio works fine over HTTPS
        return `/api/media/file?path=${encodeURIComponent(path)}`;
    }

    toggleMusicPlayPause() {
        const mp = this.musicPlayer;
        if (mp.isPlaying) {
            mp.audio.pause();
            mp.isPlaying = false;
        } else {
            mp.audio.play();
            mp.isPlaying = true;
        }
    }

    playNextTrack() {
        const mp = this.musicPlayer;
        if (mp.playlist.length === 0) return;

        let nextIndex;
        if (mp.shuffle) {
            const currentShufflePos = mp.shuffledOrder.indexOf(mp.currentIndex);
            const nextShufflePos = (currentShufflePos + 1) % mp.shuffledOrder.length;
            nextIndex = mp.shuffledOrder[nextShufflePos];
        } else {
            nextIndex = (mp.currentIndex + 1) % mp.playlist.length;
        }

        this.loadAndPlayTrack(nextIndex);
    }

    playPrevTrack() {
        const mp = this.musicPlayer;
        if (mp.playlist.length === 0) return;

        // If current time > 3 seconds, restart current track
        if (mp.audio.currentTime > 3) {
            mp.audio.currentTime = 0;
            return;
        }

        let prevIndex;
        if (mp.shuffle) {
            const currentShufflePos = mp.shuffledOrder.indexOf(mp.currentIndex);
            const prevShufflePos = (currentShufflePos - 1 + mp.shuffledOrder.length) % mp.shuffledOrder.length;
            prevIndex = mp.shuffledOrder[prevShufflePos];
        } else {
            prevIndex = (mp.currentIndex - 1 + mp.playlist.length) % mp.playlist.length;
        }

        this.loadAndPlayTrack(prevIndex);
    }

    handleTrackEnded() {
        const mp = this.musicPlayer;

        if (mp.repeat === 'one') {
            mp.audio.currentTime = 0;
            mp.audio.play();
        } else if (mp.repeat === 'all' || mp.shuffle) {
            this.playNextTrack();
        } else {
            // No repeat - check if more tracks
            const nextIndex = mp.currentIndex + 1;
            if (nextIndex < mp.playlist.length) {
                this.loadAndPlayTrack(nextIndex);
            } else {
                // End of playlist
                mp.isPlaying = false;
                this.updatePlayPauseButtons(false);
            }
        }
    }

    toggleShuffle() {
        const mp = this.musicPlayer;
        mp.shuffle = !mp.shuffle;

        if (mp.shuffle) {
            this._generateShuffleOrder();
        }

        this.updateShuffleButton();
    }

    _generateShuffleOrder() {
        const mp = this.musicPlayer;
        mp.shuffledOrder = [...mp.originalOrder];

        // Fisher-Yates shuffle
        for (let i = mp.shuffledOrder.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [mp.shuffledOrder[i], mp.shuffledOrder[j]] = [mp.shuffledOrder[j], mp.shuffledOrder[i]];
        }

        // Move current track to front
        const currentPos = mp.shuffledOrder.indexOf(mp.currentIndex);
        if (currentPos > 0) {
            mp.shuffledOrder.splice(currentPos, 1);
            mp.shuffledOrder.unshift(mp.currentIndex);
        }
    }

    toggleRepeat() {
        const mp = this.musicPlayer;
        const modes = ['none', 'all', 'one'];
        const currentIdx = modes.indexOf(mp.repeat);
        mp.repeat = modes[(currentIdx + 1) % modes.length];
        this.updateRepeatButton();
    }

    updateShuffleButton() {
        const el = this.elements;
        if (el.musicPlayerShuffle) {
            if (this.musicPlayer.shuffle) {
                el.musicPlayerShuffle.classList.remove('text-gray-400');
                el.musicPlayerShuffle.classList.add('text-emerald-400');
            } else {
                el.musicPlayerShuffle.classList.remove('text-emerald-400');
                el.musicPlayerShuffle.classList.add('text-gray-400');
            }
        }
    }

    updateRepeatButton() {
        const el = this.elements;
        const mp = this.musicPlayer;

        if (el.musicPlayerRepeat) {
            if (mp.repeat === 'none') {
                el.musicPlayerRepeat.classList.remove('text-emerald-400');
                el.musicPlayerRepeat.classList.add('text-gray-400');
            } else {
                el.musicPlayerRepeat.classList.remove('text-gray-400');
                el.musicPlayerRepeat.classList.add('text-emerald-400');
            }
        }

        if (el.musicPlayerRepeatBadge) {
            if (mp.repeat === 'one') {
                el.musicPlayerRepeatBadge.classList.remove('hidden');
                el.musicPlayerRepeatBadge.textContent = '1';
            } else {
                el.musicPlayerRepeatBadge.classList.add('hidden');
            }
        }
    }

    seekMusic(e, progressBar) {
        const rect = progressBar.getBoundingClientRect();
        const percent = (e.clientX - rect.left) / rect.width;
        const mp = this.musicPlayer;
        if (mp.audio.duration) {
            mp.audio.currentTime = percent * mp.audio.duration;
        }
    }

    updateMusicProgress() {
        const mp = this.musicPlayer;
        const current = mp.audio.currentTime;
        const duration = mp.audio.duration || 0;
        const percent = duration > 0 ? (current / duration) * 100 : 0;

        const timeStr = this._formatTime(current);

        // Update mini player
        if (this.elements.miniPlayerProgressFill) {
            this.elements.miniPlayerProgressFill.style.width = `${percent}%`;
        }
        if (this.elements.miniPlayerCurrentTime) {
            this.elements.miniPlayerCurrentTime.textContent = timeStr;
        }

        // Update full player
        if (this.elements.musicPlayerProgressFill) {
            this.elements.musicPlayerProgressFill.style.width = `${percent}%`;
        }
        if (this.elements.musicPlayerCurrentTime) {
            this.elements.musicPlayerCurrentTime.textContent = timeStr;
        }
    }

    updateMusicDuration() {
        const duration = this.musicPlayer.audio.duration || 0;
        const durationStr = this._formatTime(duration);

        if (this.elements.miniPlayerDuration) {
            this.elements.miniPlayerDuration.textContent = durationStr;
        }
        if (this.elements.musicPlayerDuration) {
            this.elements.musicPlayerDuration.textContent = durationStr;
        }
    }

    _formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }

    updatePlayPauseButtons(isPlaying) {
        const el = this.elements;

        // Mini player
        if (el.miniPlayerPlayIcon && el.miniPlayerPauseIcon) {
            el.miniPlayerPlayIcon.classList.toggle('hidden', isPlaying);
            el.miniPlayerPauseIcon.classList.toggle('hidden', !isPlaying);
        }

        // Full player
        if (el.musicPlayerPlayIcon && el.musicPlayerPauseIcon) {
            el.musicPlayerPlayIcon.classList.toggle('hidden', isPlaying);
            el.musicPlayerPauseIcon.classList.toggle('hidden', !isPlaying);
        }
    }

    updateMusicPlayerUI(file) {
        const title = file.name.replace(/\.[^.]+$/, ''); // Remove extension

        // Update mini player
        if (this.elements.miniPlayerTitle) {
            this.elements.miniPlayerTitle.textContent = title;
        }
        if (this.elements.miniPlayerArtist) {
            this.elements.miniPlayerArtist.textContent = file.extension?.slice(1).toUpperCase() || 'Audio';
        }

        // Update full player
        if (this.elements.musicPlayerTitle) {
            this.elements.musicPlayerTitle.textContent = title;
        }
        if (this.elements.musicPlayerArtist) {
            this.elements.musicPlayerArtist.textContent = file.extension?.slice(1).toUpperCase() || 'Audio';
        }

        // Update playlist panel
        this.renderPlaylistPanel();
    }

    renderPlaylistPanel() {
        const container = this.elements.musicPlayerPlaylistItems;
        if (!container) return;

        container.innerHTML = '';
        const mp = this.musicPlayer;

        mp.playlist.forEach((file, index) => {
            const item = document.createElement('div');
            const isCurrent = index === mp.currentIndex;
            item.className = `flex items-center gap-3 p-2 rounded-lg cursor-pointer transition ${isCurrent ? 'bg-emerald-600/30' : 'hover:bg-gray-700/50'}`;

            const title = file.name.replace(/\.[^.]+$/, '');
            item.innerHTML = `
                <div class="w-8 h-8 rounded bg-gray-700 flex items-center justify-center flex-shrink-0">
                    ${isCurrent ? '<svg class="w-4 h-4 text-emerald-400" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>' : '<span class="text-xs text-gray-400">' + (index + 1) + '</span>'}
                </div>
                <div class="flex-1 min-w-0">
                    <p class="text-sm ${isCurrent ? 'text-emerald-400' : 'text-white'} truncate">${this.escapeHtml(title)}</p>
                </div>
            `;

            item.addEventListener('click', () => this.loadAndPlayTrack(index));
            container.appendChild(item);
        });
    }

    togglePlaylistPanel() {
        const panel = this.elements.musicPlayerPlaylistPanel;
        if (panel) {
            panel.classList.toggle('hidden');
        }
    }

    showMiniPlayer() {
        if (this.elements.miniPlayerBar) {
            this.elements.miniPlayerBar.classList.remove('hidden');
        }
    }

    hideMiniPlayer() {
        if (this.elements.miniPlayerBar) {
            this.elements.miniPlayerBar.classList.add('hidden');
        }
    }

    openMusicPlayerModal() {
        if (this.elements.musicPlayerModal) {
            this.elements.musicPlayerModal.classList.remove('hidden');
        }
    }

    closeMusicPlayerModal() {
        if (this.elements.musicPlayerModal) {
            this.elements.musicPlayerModal.classList.add('hidden');
        }
    }

    stopMusic() {
        const mp = this.musicPlayer;
        mp.audio.pause();
        mp.audio.currentTime = 0;
        mp.isPlaying = false;
        mp.playlist = [];
        mp.currentIndex = 0;

        this.hideMiniPlayer();
        this.closeMusicPlayerModal();

        // Clear Media Session
        if ('mediaSession' in navigator) {
            navigator.mediaSession.metadata = null;
        }
    }

    setupMediaSession() {
        if (!('mediaSession' in navigator)) return;

        navigator.mediaSession.setActionHandler('play', () => {
            this.musicPlayer.audio.play();
        });
        navigator.mediaSession.setActionHandler('pause', () => {
            this.musicPlayer.audio.pause();
        });
        navigator.mediaSession.setActionHandler('previoustrack', () => {
            this.playPrevTrack();
        });
        navigator.mediaSession.setActionHandler('nexttrack', () => {
            this.playNextTrack();
        });
        navigator.mediaSession.setActionHandler('seekto', (details) => {
            if (details.seekTime !== undefined) {
                this.musicPlayer.audio.currentTime = details.seekTime;
            }
        });
    }

    updateMediaSessionMetadata(file) {
        if (!('mediaSession' in navigator)) return;

        const title = file.name.replace(/\.[^.]+$/, '');

        navigator.mediaSession.metadata = new MediaMetadata({
            title: title,
            artist: 'AoiTalk Music Player',
            album: 'Local Music'
        });
    }

    // ── Conversation Session Management ─────────────────────────────────────────

    /**
     * Set current conversation session ID
     * Called by conversation-history.js when session is switched
     */
    setCurrentConversationSessionId(sessionId) {
        this.currentConversationSessionId = sessionId;
        console.log(`[ChatClient] Conversation session ID set to: ${sessionId || 'new conversation'}`);
    }

}

// Initialize chat client when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.chatClient = new ChatClient();
});
