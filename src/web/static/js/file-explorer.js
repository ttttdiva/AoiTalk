/**
 * File Explorer Module for AoiTalk
 * 
 * Provides a modern file manager UI with directory navigation,
 * file operations, and preview capabilities.
 */

class FileExplorer {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.currentPath = "";
        this.selectedItems = new Set();
        this.viewMode = "grid"; // "grid" | "list"
        this.directoryTree = null;
        this.currentContents = { directories: [], files: [] };
        this.bookmarks = [];

        // Storage context support
        this.storageContexts = [];
        this.currentStorageContext = { type: 'personal', id: null };

        // Admin mode - allows navigating outside storage context
        this.isAdmin = false;
        this.isOutsideContext = false;  // True when admin is browsing system paths

        this.init();
    }

    async init() {
        this.render();
        await this.loadStorageContexts();
        await this.loadBookmarks();
        await this.loadDirectory("");
        await this.loadTree();
    }

    // ── Storage Context Methods ─────────────────────────────────────────

    async loadStorageContexts() {
        try {
            const response = await fetch('/api/storage/contexts');
            const data = await response.json();
            if (data.success) {
                this.storageContexts = data.contexts || [];
                this.currentStorageContext = data.current_context || { type: 'personal', id: null };
                this.isAdmin = data.is_admin || false;
                this.renderContextSelector();
            }
        } catch (error) {
            console.error('Failed to load storage contexts:', error);
            // Fallback - still usable without project support
        }
    }

    async switchStorageContext(contextType, contextId = null) {
        this.currentStorageContext = { type: contextType, id: contextId };
        this.currentPath = "";
        this.isOutsideContext = false;  // Reset when switching context
        this.renderContextSelector();
        await this.loadDirectory("");
        await this.loadTree();
    }

    renderContextSelector() {
        const container = document.getElementById('feContextSelector');
        if (!container) return;

        // Always show selector for admins (to access system), or if multiple contexts
        if (this.storageContexts.length <= 1 && !this.isAdmin) {
            container.innerHTML = '';
            return;
        }

        let html = '<select id="feContextSelect" class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs">';

        // Add system browsing option for admins
        if (this.isAdmin) {
            const systemSelected = this.isOutsideContext ? 'selected' : '';
            html += `<option value="__system__:" ${systemSelected}>🖥️ システム全体</option>`;
        }

        this.storageContexts.forEach(ctx => {
            const selected = (!this.isOutsideContext &&
                ctx.type === this.currentStorageContext.type &&
                ctx.id === this.currentStorageContext.id) ? 'selected' : '';
            html += `<option value="${ctx.type}:${ctx.id || ''}" ${selected}>${ctx.icon || '📁'} ${ctx.name}</option>`;
        });
        html += '</select>';
        container.innerHTML = html;

        document.getElementById('feContextSelect')?.addEventListener('change', (e) => {
            const [type, id] = e.target.value.split(':');
            if (type === '__system__') {
                this.enterSystemMode('');
            } else {
                this.isOutsideContext = false;
                this.switchStorageContext(type, id || null);
            }
        });
    }

    // ── Path Conversion Methods ──────────────────────────────────────────

    /**
     * Get storage path prefix for current context
     * @returns {string} Path prefix like "_users/user_xxx" or "_projects/project_xxx"
     */
    getStoragePrefix() {
        // When admin is outside context, no prefix
        if (this.isOutsideContext) {
            return '';
        }
        const ctx = this.currentStorageContext;
        if (ctx.type === 'personal' && ctx.id) {
            return `_users/user_${ctx.id}`;
        } else if (ctx.type === 'project' && ctx.id) {
            return `_projects/project_${ctx.id}`;
        }
        return ''; // legacy/default - no prefix
    }

    /**
     * Convert relative display path to full storage path
     * @param {string} relativePath - Path relative to context root (e.g., "documents/file.txt")
     * @returns {string} Full storage path (e.g., "_users/user_xxx/documents/file.txt")
     */
    toStoragePath(relativePath) {
        // When outside context mode, path is absolute or special
        if (this.isOutsideContext) {
            return relativePath;
        }
        const prefix = this.getStoragePrefix();
        if (!prefix) return relativePath;
        if (!relativePath) return prefix;
        return `${prefix}/${relativePath}`;
    }

    /**
     * Convert full storage path to relative display path
     * @param {string} fullPath - Full storage path
     * @returns {string} Relative path for display
     */
    fromStoragePath(fullPath) {
        // When outside context mode, return as-is
        if (this.isOutsideContext) {
            return fullPath || '';
        }
        const prefix = this.getStoragePrefix();
        if (!prefix || !fullPath) return fullPath || '';
        if (fullPath.startsWith(prefix + '/')) {
            return fullPath.slice(prefix.length + 1);
        } else if (fullPath === prefix) {
            return '';
        }
        return fullPath;
    }

    /**
     * Admin: Enter system browsing mode (outside storage context)
     * @param {string} path - Optional absolute path to navigate to
     */
    async enterSystemMode(path = '') {
        if (!this.isAdmin) return;
        this.isOutsideContext = true;
        this.currentPath = path;
        await this.loadDirectory(path);
        await this.loadTree();
        this.renderContextSelector();
    }

    /**
     * Admin: Return to storage context mode
     */
    async exitSystemMode() {
        this.isOutsideContext = false;
        this.currentPath = '';
        await this.loadDirectory('');
        await this.loadTree();
        this.renderContextSelector();
    }

    // ── API Methods ─────────────────────────────────────────────────────

    async loadTree() {
        try {
            const prefix = this.getStoragePrefix();
            const url = prefix
                ? `/api/explorer/tree?root=${encodeURIComponent(prefix)}`
                : '/api/explorer/tree';
            const response = await fetch(url);
            const data = await response.json();
            if (data.success) {
                this.directoryTree = data.tree;
                this.renderSidebar();
            }
        } catch (error) {
            console.error('Failed to load tree:', error);
        }
    }

    async loadBookmarks() {
        try {
            const response = await fetch('/api/explorer/bookmarks');
            const data = await response.json();
            if (data.success) {
                this.bookmarks = data.bookmarks || [];
                this.renderBookmarks();
            }
        } catch (error) {
            console.error('Failed to load bookmarks:', error);
        }
    }

    async addBookmark(name, path, icon = "📁") {
        try {
            const response = await fetch('/api/explorer/bookmarks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, path, icon })
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess('ブックマークを追加しました');
                await this.loadBookmarks();
            } else {
                this.showError(data.error || 'Failed to add bookmark');
            }
        } catch (error) {
            console.error('Failed to add bookmark:', error);
            this.showError('ブックマークの追加に失敗しました');
        }
    }

    async removeBookmark(path) {
        try {
            const response = await fetch('/api/explorer/bookmarks', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path })
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess('ブックマークを削除しました');
                await this.loadBookmarks();
            } else {
                this.showError(data.error || 'Failed to remove bookmark');
            }
        } catch (error) {
            console.error('Failed to remove bookmark:', error);
            this.showError('ブックマークの削除に失敗しました');
        }
    }

    isCurrentPathBookmarked() {
        return this.bookmarks.some(bm => bm.path === this.currentPath);
    }

    async loadDirectory(path) {
        try {
            // Convert relative path to full storage path
            const storagePath = this.toStoragePath(path);
            const response = await fetch(`/api/explorer/list?path=${encodeURIComponent(storagePath)}`);
            const data = await response.json();
            if (data.success) {
                // Convert response paths back to relative display paths
                this.currentPath = this.fromStoragePath(data.current_path);

                // Convert directory/file paths for display
                const prefix = this.getStoragePrefix();
                this.currentContents = {
                    directories: (data.directories || []).map(dir => ({
                        ...dir,
                        path: this.fromStoragePath(dir.path)
                    })),
                    files: (data.files || []).map(file => ({
                        ...file,
                        path: this.fromStoragePath(file.path)
                    }))
                };

                // Handle parent path conversion
                const parentStoragePath = data.parent_path;
                if (this.isOutsideContext) {
                    // In system mode, always use API response directly
                    this.parentPath = data.parent_path;
                    this.canGoUp = data.can_go_up;
                } else if (parentStoragePath !== null && prefix) {
                    // Check if parent is at or below storage root
                    if (parentStoragePath === prefix || parentStoragePath.startsWith(prefix + '/')) {
                        this.parentPath = this.fromStoragePath(parentStoragePath);
                        this.canGoUp = true;
                    } else {
                        // Parent is outside storage context
                        if (this.isAdmin) {
                            // Admin can go up into system mode
                            this.parentPath = parentStoragePath;
                            this.canGoUp = true;
                            // Mark as "will enter system mode" for navigation
                            this._pendingSystemMode = true;
                        } else {
                            // Regular user can't go up
                            this.parentPath = null;
                            this.canGoUp = false;
                        }
                    }
                } else {
                    this.parentPath = data.parent_path;
                    this.canGoUp = data.can_go_up;
                }

                this.selectedItems.clear();
                this.renderContent();
                this.renderBreadcrumb();
            } else {
                this.showError(data.error || 'Failed to load directory');
            }
        } catch (error) {
            console.error('Failed to load directory:', error);
            this.showError('ディレクトリの読み込みに失敗しました');
        }
    }

    async createDirectory(name) {
        try {
            // Convert to storage path for API call
            const storagePath = this.toStoragePath(this.currentPath);
            const response = await fetch('/api/explorer/mkdir', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: storagePath, name })
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess(`フォルダ「${name}」を作成しました`);
                await this.loadDirectory(this.currentPath);
                await this.loadTree();
            } else {
                this.showError(data.error || 'Failed to create directory');
            }
        } catch (error) {
            console.error('Failed to create directory:', error);
            this.showError('フォルダの作成に失敗しました');
        }
    }

    async uploadFile(file) {
        try {
            // Convert to storage path for API call
            const storagePath = this.toStoragePath(this.currentPath);
            const formData = new FormData();
            formData.append('file', file);
            formData.append('path', storagePath);

            const response = await fetch(`/api/explorer/upload?path=${encodeURIComponent(storagePath)}`, {
                method: 'POST',
                body: formData
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess(`ファイル「${file.name}」をアップロードしました`);
                await this.loadDirectory(this.currentPath);
            } else {
                this.showError(data.error || 'Failed to upload file');
            }
        } catch (error) {
            console.error('Failed to upload file:', error);
            this.showError('アップロードに失敗しました');
        }
    }

    async deleteItem(path) {
        try {
            // Convert to storage path for API call
            const storagePath = this.toStoragePath(path);
            const response = await fetch(`/api/explorer/delete?path=${encodeURIComponent(storagePath)}`, {
                method: 'DELETE'
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess(data.message);
                await this.loadDirectory(this.currentPath);
                await this.loadTree();
            } else {
                this.showError(data.error || 'Failed to delete');
            }
        } catch (error) {
            console.error('Failed to delete:', error);
            this.showError('削除に失敗しました');
        }
    }

    async renameItem(path, newName) {
        try {
            // Convert to storage path for API call
            const storagePath = this.toStoragePath(path);
            const response = await fetch('/api/explorer/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: storagePath, new_name: newName })
            });
            const data = await response.json();
            if (data.success) {
                this.showSuccess(data.message);
                await this.loadDirectory(this.currentPath);
                await this.loadTree();
            } else {
                this.showError(data.error || 'Failed to rename');
            }
        } catch (error) {
            console.error('Failed to rename:', error);
            this.showError('名前変更に失敗しました');
        }
    }

    async getPreview(path) {
        try {
            // Convert to storage path for API call
            const storagePath = this.toStoragePath(path);
            const response = await fetch(`/api/explorer/preview?path=${encodeURIComponent(storagePath)}`);
            return await response.json();
        } catch (error) {
            console.error('Failed to get preview:', error);
            return { success: false, error: 'Preview failed' };
        }
    }

    downloadFile(path) {
        // Convert to storage path for API call
        const storagePath = this.toStoragePath(path);
        window.open(`/api/explorer/download?path=${encodeURIComponent(storagePath)}`, '_blank');
    }

    // ── Render Methods ──────────────────────────────────────────────────

    render() {
        this.container.innerHTML = `
            <div class="file-explorer flex flex-col h-full text-sm">
                <!-- Compact Toolbar -->
                <div class="fe-toolbar flex flex-wrap items-center gap-1 p-2 bg-gray-800/50 border-b border-gray-700">
                    <button id="feGoUp" class="fe-btn p-1.5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40" title="上へ">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 10l7-7m0 0l7 7m-7-7v18"/>
                        </svg>
                    </button>
                    <button id="feRefresh" class="fe-btn p-1.5 rounded bg-gray-700 hover:bg-gray-600" title="更新">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                        </svg>
                    </button>
                    <button id="feNewFolder" class="fe-btn p-1.5 rounded bg-emerald-700 hover:bg-emerald-600" title="新規フォルダ">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 13h6m-3-3v6m-9 1V7a2 2 0 012-2h6l2 2h6a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>
                        </svg>
                    </button>
                    <label class="fe-btn p-1.5 rounded bg-blue-700 hover:bg-blue-600 cursor-pointer" title="アップロード">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/>
                        </svg>
                        <input type="file" id="feUploadInput" class="hidden" multiple>
                    </label>
                    <button id="feBookmarkToggle" class="fe-btn p-1.5 rounded bg-yellow-700 hover:bg-yellow-600" title="ブックマーク追加/削除">
                        <span class="text-sm">⭐</span>
                    </button>
                    <div class="w-px h-4 bg-gray-600 mx-1"></div>
                    <button id="feGitSync" class="fe-btn p-1.5 rounded bg-purple-700 hover:bg-purple-600 relative" title="Git同期（コミット）">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/>
                        </svg>
                        <span id="feGitBadge" class="hidden absolute -top-1 -right-1 w-2 h-2 bg-orange-500 rounded-full"></span>
                    </button>
                    <button id="feGitHistory" class="fe-btn p-1.5 rounded bg-purple-700 hover:bg-purple-600" title="コミット履歴">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
                        </svg>
                    </button>
                    <div id="feContextSelector" class="ml-2"></div>
                    <div class="flex-1"></div>
                    <div class="flex bg-gray-700 rounded p-0.5">
                        <button id="feViewGrid" class="p-1 rounded text-emerald-300 bg-emerald-900/50" title="グリッド">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/>
                            </svg>
                        </button>
                        <button id="feViewList" class="p-1 rounded text-gray-400 hover:text-emerald-300" title="リスト">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16"/>
                            </svg>
                        </button>
                    </div>
                </div>
                
                <!-- Bookmarks Bar -->
                <div id="feBookmarks" class="flex flex-wrap gap-1 px-2 py-1 bg-gray-800/30 border-b border-gray-700/50 text-xs empty:hidden"></div>
                
                <!-- Breadcrumb -->
                <div id="feBreadcrumb" class="px-2 py-1.5 text-xs text-gray-400 border-b border-gray-700/50 truncate bg-gray-800/30"></div>
                
                <!-- Content Area -->
                <div id="feContent" class="flex-1 overflow-y-auto p-2"></div>
                
                <!-- Preview Panel -->
                <div id="fePreview" class="hidden border-t border-gray-700 p-2 bg-gray-800/50 max-h-32 overflow-y-auto">
                    <div class="flex items-center justify-between mb-1">
                        <span id="fePreviewTitle" class="text-xs font-medium text-gray-300 truncate"></span>
                        <button id="fePreviewClose" class="text-gray-400 hover:text-white text-sm">&times;</button>
                    </div>
                    <div id="fePreviewContent" class="text-xs text-gray-400"></div>
                </div>
            </div>
        `;

        this.bindEvents();
    }

    bindEvents() {
        // Toolbar buttons
        document.getElementById('feGoUp')?.addEventListener('click', () => {
            if (this.canGoUp && this.parentPath !== null) {
                // Check if we need to enter system mode (admin going above storage root)
                if (this._pendingSystemMode) {
                    this._pendingSystemMode = false;
                    this.isOutsideContext = true;
                    this.loadDirectory(this.parentPath);
                    this.loadTree();
                    this.renderContextSelector();
                } else {
                    this.loadDirectory(this.parentPath);
                }
            }
        });

        document.getElementById('feRefresh')?.addEventListener('click', () => {
            this.loadDirectory(this.currentPath);
        });

        document.getElementById('feNewFolder')?.addEventListener('click', () => {
            const name = prompt('フォルダ名を入力:');
            if (name) this.createDirectory(name);
        });

        document.getElementById('feUploadInput')?.addEventListener('change', (e) => {
            const files = e.target.files;
            if (files) {
                Array.from(files).forEach(file => this.uploadFile(file));
            }
            e.target.value = '';
        });

        // Bookmark toggle
        document.getElementById('feBookmarkToggle')?.addEventListener('click', () => {
            if (this.isCurrentPathBookmarked()) {
                if (confirm('このフォルダのブックマークを削除しますか？')) {
                    this.removeBookmark(this.currentPath);
                }
            } else {
                const name = prompt('ブックマーク名を入力:', this.currentPath.split('/').pop() || 'Workspace');
                if (name) {
                    this.addBookmark(name, this.currentPath);
                }
            }
        });

        // View mode toggle
        document.getElementById('feViewGrid')?.addEventListener('click', () => {
            this.viewMode = 'grid';
            this.updateViewModeButtons();
            this.renderContent();
        });

        document.getElementById('feViewList')?.addEventListener('click', () => {
            this.viewMode = 'list';
            this.updateViewModeButtons();
            this.renderContent();
        });

        // Preview close
        document.getElementById('fePreviewClose')?.addEventListener('click', () => {
            document.getElementById('fePreview').classList.add('hidden');
        });

        // Git buttons
        document.getElementById('feGitSync')?.addEventListener('click', () => {
            this.showGitCommitModal();
        });

        document.getElementById('feGitHistory')?.addEventListener('click', () => {
            this.showGitHistoryModal();
        });

        // Check git status on load and after directory changes
        this.updateGitStatus();

        // Drag and drop
        const content = document.getElementById('feContent');
        content?.addEventListener('dragover', (e) => {
            e.preventDefault();
            content.classList.add('bg-emerald-900/20');
        });
        content?.addEventListener('dragleave', () => {
            content.classList.remove('bg-emerald-900/20');
        });
        content?.addEventListener('drop', (e) => {
            e.preventDefault();
            content.classList.remove('bg-emerald-900/20');
            const files = e.dataTransfer?.files;
            if (files) {
                Array.from(files).forEach(file => this.uploadFile(file));
            }
        });
    }

    updateViewModeButtons() {
        const gridBtn = document.getElementById('feViewGrid');
        const listBtn = document.getElementById('feViewList');

        if (this.viewMode === 'grid') {
            gridBtn?.classList.add('text-emerald-300', 'bg-emerald-900/50');
            gridBtn?.classList.remove('text-gray-400');
            listBtn?.classList.remove('text-emerald-300', 'bg-emerald-900/50');
            listBtn?.classList.add('text-gray-400');
        } else {
            listBtn?.classList.add('text-emerald-300', 'bg-emerald-900/50');
            listBtn?.classList.remove('text-gray-400');
            gridBtn?.classList.remove('text-emerald-300', 'bg-emerald-900/50');
            gridBtn?.classList.add('text-gray-400');
        }
    }

    renderBreadcrumb() {
        const container = document.getElementById('feBreadcrumb');
        if (!container) return;

        let rootLabel, rootIcon;

        if (this.isOutsideContext) {
            // System mode - show current path as-is
            rootLabel = 'システム';
            rootIcon = '🖥️';
        } else {
            // Context mode - show context name
            const currentCtx = this.storageContexts.find(
                ctx => ctx.type === this.currentStorageContext.type &&
                    ctx.id === this.currentStorageContext.id
            );
            rootLabel = currentCtx?.name || 'Workspace';
            rootIcon = currentCtx?.icon || '📁';
        }

        const parts = this.currentPath ? this.currentPath.split('/').filter(p => p) : [];
        let html = `<span class="cursor-pointer hover:text-emerald-300" data-path="">${rootIcon} ${rootLabel}</span>`;

        let accPath = '';
        parts.forEach((part, i) => {
            accPath += (accPath ? '/' : '') + part;
            html += ` / <span class="cursor-pointer hover:text-emerald-300" data-path="${accPath}">${part}</span>`;
        });

        container.innerHTML = html;

        // Add click handlers
        container.querySelectorAll('span[data-path]').forEach(el => {
            el.addEventListener('click', () => {
                this.loadDirectory(el.dataset.path || '');
            });
        });

        // Update go up button
        const goUpBtn = document.getElementById('feGoUp');
        if (goUpBtn) {
            goUpBtn.disabled = !this.canGoUp;
        }

        // Update bookmark button state
        this.updateBookmarkButton();
    }

    renderBookmarks() {
        const container = document.getElementById('feBookmarks');
        if (!container) return;

        if (this.bookmarks.length === 0) {
            container.innerHTML = '';
            return;
        }

        let html = '';
        this.bookmarks.forEach(bm => {
            html += `
                <button class="fe-bookmark flex items-center gap-1 px-2 py-1 rounded bg-gray-700/50 hover:bg-yellow-700/50 cursor-pointer transition" 
                        data-path="${bm.path}" title="${bm.path || 'Workspace'}">
                    <span>${bm.icon || '📁'}</span>
                    <span class="truncate max-w-20">${bm.name}</span>
                </button>
            `;
        });

        container.innerHTML = html;

        // Add click handlers
        container.querySelectorAll('.fe-bookmark').forEach(el => {
            el.addEventListener('click', () => {
                this.loadDirectory(el.dataset.path || '');
            });
        });
    }

    updateBookmarkButton() {
        const btn = document.getElementById('feBookmarkToggle');
        if (!btn) return;

        if (this.isCurrentPathBookmarked()) {
            btn.classList.remove('bg-yellow-700', 'hover:bg-yellow-600');
            btn.classList.add('bg-yellow-500', 'hover:bg-yellow-400');
            btn.title = 'ブックマーク削除';
        } else {
            btn.classList.remove('bg-yellow-500', 'hover:bg-yellow-400');
            btn.classList.add('bg-yellow-700', 'hover:bg-yellow-600');
            btn.title = 'ブックマーク追加';
        }
    }

    renderSidebar() {
        const container = document.getElementById('feTree');
        if (!container || !this.directoryTree) return;

        const renderNode = (node, depth = 0) => {
            const isActive = node.path === this.currentPath;
            const paddingLeft = depth * 12;

            let html = `
                <div class="fe-tree-item flex items-center gap-1 py-1 px-2 rounded cursor-pointer text-sm ${isActive ? 'bg-emerald-900/50 text-emerald-300' : 'hover:bg-gray-700 text-gray-300'}" 
                     style="padding-left: ${paddingLeft}px" data-path="${node.path}">
                    <span class="text-xs">📁</span>
                    <span class="truncate">${node.name}</span>
                </div>
            `;

            if (node.children && node.children.length > 0) {
                node.children.forEach(child => {
                    html += renderNode(child, depth + 1);
                });
            }

            return html;
        };

        container.innerHTML = renderNode(this.directoryTree);

        container.querySelectorAll('.fe-tree-item').forEach(el => {
            el.addEventListener('click', () => {
                this.loadDirectory(el.dataset.path || '');
            });
        });
    }

    renderContent() {
        const container = document.getElementById('feContent');
        if (!container) return;

        const { directories, files } = this.currentContents;

        if (directories.length === 0 && files.length === 0) {
            container.innerHTML = `
                <div class="flex flex-col items-center justify-center h-full text-gray-500">
                    <svg class="w-12 h-12 mb-2 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z"/>
                    </svg>
                    <p class="text-sm">空のフォルダ</p>
                    <p class="text-xs mt-1">ファイルをドラッグ&ドロップでアップロード</p>
                </div>
            `;
            return;
        }

        if (this.viewMode === 'grid') {
            this.renderGridView(container, directories, files);
        } else {
            this.renderListView(container, directories, files);
        }
    }

    renderGridView(container, directories, files) {
        let html = '<div class="grid grid-cols-2 gap-2">';

        // Directories
        directories.forEach(dir => {
            html += `
                <div class="fe-item fe-folder group relative bg-gray-700/50 hover:bg-gray-600/50 rounded-xl p-3 cursor-pointer transition" 
                     data-path="${dir.path}" data-type="directory">
                    <div class="text-3xl mb-2 text-center">📁</div>
                    <div class="text-sm text-gray-200 truncate text-center" title="${dir.name}">${dir.name}</div>
                    <div class="text-xs text-gray-500 text-center">${dir.item_count || 0} items</div>
                    <div class="fe-actions hidden group-hover:flex absolute top-1 right-1 gap-1">
                        <button class="fe-action-rename p-1 rounded bg-gray-800/80 hover:bg-gray-700 text-xs" title="名前変更">✏️</button>
                        <button class="fe-action-delete p-1 rounded bg-gray-800/80 hover:bg-red-600 text-xs" title="削除">🗑️</button>
                    </div>
                </div>
            `;
        });

        // Files
        files.forEach(file => {
            html += `
                <div class="fe-item fe-file group relative bg-gray-700/50 hover:bg-gray-600/50 rounded-xl p-3 cursor-pointer transition" 
                     data-path="${file.path}" data-type="file">
                    <div class="text-3xl mb-2 text-center">${file.icon || '📄'}</div>
                    <div class="text-sm text-gray-200 truncate text-center" title="${file.name}">${file.name}</div>
                    <div class="text-xs text-gray-500 text-center">${file.size_display}</div>
                    <div class="fe-actions hidden group-hover:flex absolute top-1 right-1 gap-1">
                        <button class="fe-action-download p-1 rounded bg-gray-800/80 hover:bg-blue-600 text-xs" title="ダウンロード">⬇️</button>
                        <button class="fe-action-rename p-1 rounded bg-gray-800/80 hover:bg-gray-700 text-xs" title="名前変更">✏️</button>
                        <button class="fe-action-delete p-1 rounded bg-gray-800/80 hover:bg-red-600 text-xs" title="削除">🗑️</button>
                    </div>
                </div>
            `;
        });

        html += '</div>';
        container.innerHTML = html;
        this.bindContentEvents();
    }

    renderListView(container, directories, files) {
        let html = '<div class="space-y-1">';

        // Header
        html += `
            <div class="grid grid-cols-12 gap-2 px-3 py-2 text-xs text-gray-500 uppercase font-semibold border-b border-gray-700">
                <div class="col-span-6">名前</div>
                <div class="col-span-2">サイズ</div>
                <div class="col-span-3">更新日時</div>
                <div class="col-span-1"></div>
            </div>
        `;

        // Directories
        directories.forEach(dir => {
            html += `
                <div class="fe-item fe-folder grid grid-cols-12 gap-2 px-3 py-2 rounded-lg hover:bg-gray-700/50 cursor-pointer items-center group" 
                     data-path="${dir.path}" data-type="directory">
                    <div class="col-span-6 flex items-center gap-2 truncate">
                        <span>📁</span>
                        <span class="text-gray-200 truncate">${dir.name}</span>
                    </div>
                    <div class="col-span-2 text-xs text-gray-500">${dir.item_count || 0} items</div>
                    <div class="col-span-3 text-xs text-gray-500">${this.formatDate(dir.modified_at)}</div>
                    <div class="col-span-1 flex gap-1 opacity-0 group-hover:opacity-100">
                        <button class="fe-action-rename p-1 rounded hover:bg-gray-600 text-xs" title="名前変更">✏️</button>
                        <button class="fe-action-delete p-1 rounded hover:bg-red-600 text-xs" title="削除">🗑️</button>
                    </div>
                </div>
            `;
        });

        // Files
        files.forEach(file => {
            html += `
                <div class="fe-item fe-file grid grid-cols-12 gap-2 px-3 py-2 rounded-lg hover:bg-gray-700/50 cursor-pointer items-center group" 
                     data-path="${file.path}" data-type="file">
                    <div class="col-span-6 flex items-center gap-2 truncate">
                        <span>${file.icon || '📄'}</span>
                        <span class="text-gray-200 truncate">${file.name}</span>
                    </div>
                    <div class="col-span-2 text-xs text-gray-500">${file.size_display}</div>
                    <div class="col-span-3 text-xs text-gray-500">${this.formatDate(file.modified_at)}</div>
                    <div class="col-span-1 flex gap-1 opacity-0 group-hover:opacity-100">
                        <button class="fe-action-download p-1 rounded hover:bg-blue-600 text-xs" title="ダウンロード">⬇️</button>
                        <button class="fe-action-rename p-1 rounded hover:bg-gray-600 text-xs" title="名前変更">✏️</button>
                        <button class="fe-action-delete p-1 rounded hover:bg-red-600 text-xs" title="削除">🗑️</button>
                    </div>
                </div>
            `;
        });

        html += '</div>';
        container.innerHTML = html;
        this.bindContentEvents();
    }

    bindContentEvents() {
        // Click on item
        document.querySelectorAll('.fe-item').forEach(el => {
            el.addEventListener('click', async (e) => {
                // Ignore if clicking action buttons
                if (e.target.closest('.fe-actions') || e.target.closest('button')) return;

                const path = el.dataset.path;
                const type = el.dataset.type;

                if (type === 'directory') {
                    this.loadDirectory(path);
                } else {
                    // Show preview
                    await this.showPreview(path);
                }
            });

            // Double-click to download file
            el.addEventListener('dblclick', (e) => {
                const path = el.dataset.path;
                const type = el.dataset.type;
                if (type === 'file') {
                    this.downloadFile(path);
                }
            });
        });

        // Action buttons
        document.querySelectorAll('.fe-action-download').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const path = btn.closest('.fe-item')?.dataset.path;
                if (path) this.downloadFile(path);
            });
        });

        document.querySelectorAll('.fe-action-rename').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const item = btn.closest('.fe-item');
                const path = item?.dataset.path;
                if (path) {
                    const currentName = path.split('/').pop();
                    const newName = prompt('新しい名前:', currentName);
                    if (newName && newName !== currentName) {
                        this.renameItem(path, newName);
                    }
                }
            });
        });

        document.querySelectorAll('.fe-action-delete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const path = btn.closest('.fe-item')?.dataset.path;
                if (path && confirm('削除しますか？')) {
                    this.deleteItem(path);
                }
            });
        });
    }

    async showPreview(path) {
        const previewPanel = document.getElementById('fePreview');
        const previewTitle = document.getElementById('fePreviewTitle');
        const previewContent = document.getElementById('fePreviewContent');

        if (!previewPanel || !previewTitle || !previewContent) return;

        const data = await this.getPreview(path);

        if (!data.success) {
            previewContent.innerHTML = `<span class="text-red-400">${data.error || 'Preview failed'}</span>`;
            previewPanel.classList.remove('hidden');
            return;
        }

        previewTitle.textContent = path.split('/').pop() || path;

        if (data.type === 'text') {
            previewContent.innerHTML = `<pre class="whitespace-pre-wrap font-mono text-gray-300 max-h-32 overflow-y-auto">${this.escapeHtml(data.content)}</pre>`;
        } else if (data.type === 'image') {
            previewContent.innerHTML = `<img src="${data.data_url}" class="max-h-32 rounded" alt="preview">`;
        } else if (data.type === 'office') {
            if (data.content) {
                previewContent.innerHTML = `<pre class="whitespace-pre-wrap text-gray-300 max-h-32 overflow-y-auto">${this.escapeHtml(data.content)}</pre>`;
            } else {
                previewContent.innerHTML = `<span class="text-gray-500">${data.message || 'Preview not available'}</span>`;
            }
        } else {
            previewContent.innerHTML = `<span class="text-gray-500">${data.message || 'Preview not available'}</span>`;
        }

        previewPanel.classList.remove('hidden');
    }

    // ── Utility Methods ─────────────────────────────────────────────────

    formatDate(isoString) {
        if (!isoString) return '-';
        const date = new Date(isoString);
        return date.toLocaleDateString('ja-JP', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    }

    escapeHtml(text) {
        if (!text) return '';
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    showSuccess(message) {
        // Use existing notification system if available
        if (window.showNotification) {
            window.showNotification(message, 'success');
        } else {
            console.log('✅', message);
        }
    }

    showError(message) {
        if (window.showNotification) {
            window.showNotification(message, 'error');
        } else {
            console.error('❌', message);
        }
    }

    // ── Git Methods ─────────────────────────────────────────────────────

    getGitContext() {
        // Build git API payload based on current storage context
        if (this.isOutsideContext) {
            return null; // Git not available in system mode
        }
        const ctx = this.currentStorageContext;
        if (ctx.type === 'personal') {
            return { storage_context: 'user', context_id: null };
        } else if (ctx.type === 'project') {
            return { storage_context: 'project', context_id: ctx.id };
        }
        return { storage_context: 'user', context_id: null };
    }

    async updateGitStatus() {
        const gitContext = this.getGitContext();
        if (!gitContext) {
            document.getElementById('feGitBadge')?.classList.add('hidden');
            return;
        }

        try {
            const response = await fetch('/api/git/status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(gitContext)
            });
            const data = await response.json();

            const badge = document.getElementById('feGitBadge');
            if (badge) {
                if (data.has_changes) {
                    badge.classList.remove('hidden');
                } else {
                    badge.classList.add('hidden');
                }
            }
        } catch (error) {
            console.error('Failed to get git status:', error);
        }
    }

    async showGitCommitModal() {
        const gitContext = this.getGitContext();
        if (!gitContext) {
            this.showError('システムモードではGit操作は使用できません');
            return;
        }

        // Get current status first
        let statusText = '';
        try {
            const response = await fetch('/api/git/status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(gitContext)
            });
            const data = await response.json();

            if (!data.is_repo) {
                statusText = '<p class="text-yellow-400">リポジトリがまだ初期化されていません。初回コミットで自動作成されます。</p>';
            } else if (!data.has_changes) {
                statusText = '<p class="text-gray-400">変更はありません</p>';
            } else {
                const changes = [
                    ...data.staged.map(f => `<span class="text-green-400">+ ${f}</span>`),
                    ...data.modified.map(f => `<span class="text-yellow-400">M ${f}</span>`),
                    ...data.untracked.map(f => `<span class="text-gray-400">? ${f}</span>`)
                ];
                statusText = changes.length > 0
                    ? changes.slice(0, 10).join('<br>') + (changes.length > 10 ? `<br><span class="text-gray-500">...他 ${changes.length - 10} ファイル</span>` : '')
                    : '<p class="text-gray-400">変更あり</p>';
            }
        } catch (error) {
            statusText = '<p class="text-red-400">ステータス取得に失敗</p>';
        }

        // Show modal
        const modal = document.createElement('div');
        modal.id = 'feGitCommitModal';
        modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
        modal.innerHTML = `
            <div class="bg-gray-800 rounded-xl p-6 w-full max-w-md shadow-2xl border border-gray-700">
                <h3 class="text-lg font-bold text-white mb-4 flex items-center gap-2">
                    <svg class="w-5 h-5 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/>
                    </svg>
                    変更をコミット
                </h3>
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-1">変更内容:</label>
                    <div class="bg-gray-900 rounded-lg p-3 text-xs font-mono max-h-32 overflow-y-auto">
                        ${statusText}
                    </div>
                </div>
                <div class="mb-4">
                    <label class="block text-sm text-gray-400 mb-1">コミットメッセージ:</label>
                    <input type="text" id="feGitCommitMessage" 
                           class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-purple-500"
                           placeholder="変更内容の説明を入力..."
                           value="${new Date().toLocaleDateString('ja-JP')} 更新">
                </div>
                <div class="flex gap-2">
                    <button id="feGitCommitCancel" class="flex-1 px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium text-gray-200">
                        キャンセル
                    </button>
                    <button id="feGitCommitSubmit" class="flex-1 px-4 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium text-white">
                        コミット
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);

        // Event handlers
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.remove();
        });
        document.getElementById('feGitCommitCancel').addEventListener('click', () => modal.remove());
        document.getElementById('feGitCommitSubmit').addEventListener('click', async () => {
            const message = document.getElementById('feGitCommitMessage').value.trim();
            if (!message) {
                this.showError('コミットメッセージを入力してください');
                return;
            }
            await this.gitCommit(message);
            modal.remove();
        });

        // Focus input
        document.getElementById('feGitCommitMessage').focus();
        document.getElementById('feGitCommitMessage').select();
    }

    async gitCommit(message) {
        const gitContext = this.getGitContext();
        if (!gitContext) return;

        try {
            const response = await fetch('/api/git/commit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...gitContext, message })
            });
            const data = await response.json();

            if (data.success) {
                this.showSuccess(data.message);
                this.updateGitStatus();
            } else {
                this.showError(data.message || 'コミットに失敗しました');
            }
        } catch (error) {
            console.error('Git commit failed:', error);
            this.showError('コミットに失敗しました');
        }
    }

    async showGitHistoryModal() {
        const gitContext = this.getGitContext();
        if (!gitContext) {
            this.showError('システムモードではGit操作は使用できません');
            return;
        }

        // Get commit history
        let historyHtml = '';
        try {
            const response = await fetch('/api/git/log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...gitContext, limit: 20 })
            });
            const data = await response.json();

            if (data.commits && data.commits.length > 0) {
                historyHtml = data.commits.map(commit => `
                    <div class="flex items-start gap-3 p-2 rounded hover:bg-gray-700/50">
                        <div class="w-2 h-2 mt-1.5 rounded-full bg-purple-400 flex-shrink-0"></div>
                        <div class="flex-1 min-w-0">
                            <div class="text-sm text-gray-200 truncate">${this.escapeHtml(commit.message)}</div>
                            <div class="text-xs text-gray-500">${commit.hash_short} • ${this.formatGitDate(commit.date)}</div>
                        </div>
                    </div>
                `).join('');
            } else {
                historyHtml = '<p class="text-gray-500 text-center py-4">コミット履歴がありません</p>';
            }
        } catch (error) {
            historyHtml = '<p class="text-red-400 text-center py-4">履歴の取得に失敗しました</p>';
        }

        // Show modal
        const modal = document.createElement('div');
        modal.id = 'feGitHistoryModal';
        modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
        modal.innerHTML = `
            <div class="bg-gray-800 rounded-xl p-6 w-full max-w-lg shadow-2xl border border-gray-700">
                <h3 class="text-lg font-bold text-white mb-4 flex items-center gap-2">
                    <svg class="w-5 h-5 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                    コミット履歴
                </h3>
                <div class="bg-gray-900 rounded-lg max-h-80 overflow-y-auto">
                    ${historyHtml}
                </div>
                <div class="mt-4">
                    <button id="feGitHistoryClose" class="w-full px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm font-medium text-gray-200">
                        閉じる
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(modal);

        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.remove();
        });
        document.getElementById('feGitHistoryClose').addEventListener('click', () => modal.remove());
    }

    formatGitDate(dateStr) {
        if (!dateStr) return '';
        try {
            const date = new Date(dateStr);
            return date.toLocaleDateString('ja-JP', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        } catch {
            return dateStr;
        }
    }
}

// Export for use
window.FileExplorer = FileExplorer;
