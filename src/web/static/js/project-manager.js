/**
 * Project Manager Module for AoiTalk
 * 
 * Provides project CRUD, member management, and join request workflows.
 */

class ProjectManager {
    constructor() {
        this.projects = [];
        this.currentProject = null;
        this.members = [];
        this.joinRequests = [];
        this.isOpen = false;
    }

    // ── API Methods ─────────────────────────────────────────────────────

    async loadProjects() {
        try {
            const response = await fetch('/api/projects');
            const data = await response.json();
            if (response.ok) {
                this.projects = (data.projects || []).map(p => {
                    // Flatten membership.role to top-level for convenience
                    if (p.membership && p.membership.role) {
                        p.role = p.membership.role;
                    }
                    return p;
                });

                // Update header project select
                this.updateHeaderProjectSelect();

                // Update conversation history filter
                if (window.conversationHistoryManager) {
                    window.conversationHistoryManager.renderProjectFilter(this.projects);
                }

                return this.projects;
            } else {
                console.error('Failed to load projects:', data.detail);
                return [];
            }
        } catch (error) {
            console.error('Failed to load projects:', error);
            return [];
        }
    }

    updateHeaderProjectSelect() {
        const projectSelect = document.getElementById('projectSelect');
        if (!projectSelect) return;

        const currentValue = projectSelect.value;

        // Clear existing options except default ones
        while (projectSelect.children.length > 2) {
            projectSelect.removeChild(projectSelect.lastChild);
        }

        // Add project options
        this.projects.forEach(project => {
            const option = document.createElement('option');
            option.value = project.id;
            option.textContent = project.name;
            projectSelect.appendChild(option);
        });

        // Restore selection
        if (currentValue) {
            projectSelect.value = currentValue;
        }
    }

    async createProject(name, description = '', allowJoinRequests = true) {
        try {
            const response = await fetch('/api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    description,
                    allow_join_requests: allowJoinRequests
                })
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadProjects();
                this.showSuccess(`プロジェクト「${name}」を作成しました`);
                return data;
            } else {
                this.showError(data.detail || 'プロジェクトの作成に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to create project:', error);
            this.showError('プロジェクトの作成に失敗しました');
            return null;
        }
    }

    async updateProject(projectId, updates) {
        try {
            const response = await fetch(`/api/projects/${projectId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates)
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadProjects();
                this.showSuccess('プロジェクトを更新しました');
                return data;
            } else {
                this.showError(data.detail || 'プロジェクトの更新に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to update project:', error);
            this.showError('プロジェクトの更新に失敗しました');
            return null;
        }
    }

    async deleteProject(projectId) {
        try {
            const response = await fetch(`/api/projects/${projectId}`, {
                method: 'DELETE'
            });
            if (response.ok) {
                await this.loadProjects();
                this.showSuccess('プロジェクトを削除しました');
                return true;
            } else {
                const data = await response.json();
                this.showError(data.detail || 'プロジェクトの削除に失敗しました');
                return false;
            }
        } catch (error) {
            console.error('Failed to delete project:', error);
            this.showError('プロジェクトの削除に失敗しました');
            return false;
        }
    }

    async loadMembers(projectId) {
        try {
            const response = await fetch(`/api/projects/${projectId}/members`);
            const data = await response.json();
            if (response.ok) {
                this.members = data.members || [];
                return this.members;
            } else {
                console.error('Failed to load members:', data.detail);
                return [];
            }
        } catch (error) {
            console.error('Failed to load members:', error);
            return [];
        }
    }

    async addMember(projectId, userId, role = 'member') {
        try {
            const response = await fetch(`/api/projects/${projectId}/members`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, role })
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadMembers(projectId);
                this.showSuccess('メンバーを追加しました');
                return data;
            } else {
                this.showError(data.detail || 'メンバーの追加に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to add member:', error);
            this.showError('メンバーの追加に失敗しました');
            return null;
        }
    }

    async removeMember(projectId, userId) {
        try {
            const response = await fetch(`/api/projects/${projectId}/members/${userId}`, {
                method: 'DELETE'
            });
            if (response.ok) {
                await this.loadMembers(projectId);
                this.showSuccess('メンバーを削除しました');
                return true;
            } else {
                const data = await response.json();
                this.showError(data.detail || 'メンバーの削除に失敗しました');
                return false;
            }
        } catch (error) {
            console.error('Failed to remove member:', error);
            this.showError('メンバーの削除に失敗しました');
            return false;
        }
    }

    async updateMemberRole(projectId, userId, role) {
        try {
            const response = await fetch(`/api/projects/${projectId}/members/${userId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role })
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadMembers(projectId);
                this.showSuccess('権限を更新しました');
                return data;
            } else {
                this.showError(data.detail || '権限の更新に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to update member role:', error);
            this.showError('権限の更新に失敗しました');
            return null;
        }
    }

    async loadJoinRequests(projectId) {
        try {
            const response = await fetch(`/api/projects/${projectId}/join-requests`);
            const data = await response.json();
            if (response.ok) {
                this.joinRequests = data.requests || [];
                return this.joinRequests;
            } else {
                console.error('Failed to load join requests:', data.detail);
                return [];
            }
        } catch (error) {
            console.error('Failed to load join requests:', error);
            return [];
        }
    }

    async approveJoinRequest(projectId, requestId, role = 'member') {
        try {
            const response = await fetch(`/api/projects/${projectId}/join-requests/${requestId}/approve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role })
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadJoinRequests(projectId);
                await this.loadMembers(projectId);
                this.showSuccess('参加申請を承認しました');
                return data;
            } else {
                this.showError(data.detail || '承認に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to approve join request:', error);
            this.showError('承認に失敗しました');
            return null;
        }
    }

    async rejectJoinRequest(projectId, requestId, reason = '') {
        try {
            const response = await fetch(`/api/projects/${projectId}/join-requests/${requestId}/reject`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason })
            });
            const data = await response.json();
            if (response.ok) {
                await this.loadJoinRequests(projectId);
                this.showSuccess('参加申請を却下しました');
                return data;
            } else {
                this.showError(data.detail || '却下に失敗しました');
                return null;
            }
        } catch (error) {
            console.error('Failed to reject join request:', error);
            this.showError('却下に失敗しました');
            return null;
        }
    }

    // ── UI Methods ──────────────────────────────────────────────────────

    open() {
        this.isOpen = true;
        this.render();
        this.loadProjects().then(() => this.renderProjectList());
        document.getElementById('projectManagerModal')?.classList.remove('hidden');
    }

    close() {
        this.isOpen = false;
        this.currentProject = null;
        document.getElementById('projectManagerModal')?.classList.add('hidden');
    }

    render() {
        const existingModal = document.getElementById('projectManagerModal');
        if (existingModal) return;

        const modal = document.createElement('div');
        modal.id = 'projectManagerModal';
        modal.className = 'fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4 hidden';
        modal.innerHTML = `
            <div class="bg-gray-800 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col">
                <!-- Header -->
                <div class="flex items-center justify-between p-4 border-b border-gray-700">
                    <h2 class="text-lg font-bold text-white flex items-center gap-2">
                        <span>📁</span>
                        <span id="pmTitle">プロジェクト管理</span>
                    </h2>
                    <button id="pmClose" class="p-2 hover:bg-gray-700 rounded-lg transition">
                        <svg class="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                        </svg>
                    </button>
                </div>
                
                <!-- Content -->
                <div id="pmContent" class="flex-1 overflow-y-auto p-4">
                    <!-- Dynamic content -->
                </div>
                
                <!-- Footer -->
                <div id="pmFooter" class="p-4 border-t border-gray-700 flex justify-end gap-2">
                    <!-- Dynamic buttons -->
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        // Close events
        document.getElementById('pmClose')?.addEventListener('click', () => this.close());
        modal.addEventListener('click', (e) => {
            if (e.target === modal) this.close();
        });
    }

    renderProjectList() {
        const content = document.getElementById('pmContent');
        const footer = document.getElementById('pmFooter');
        const title = document.getElementById('pmTitle');
        if (!content || !footer || !title) return;

        title.textContent = 'プロジェクト管理';

        if (this.projects.length === 0) {
            content.innerHTML = `
                <div class="text-center py-8 text-gray-400">
                    <p class="text-4xl mb-4">📂</p>
                    <p>プロジェクトがありません</p>
                    <p class="text-sm mt-2">「新規作成」ボタンからプロジェクトを作成してください</p>
                </div>
            `;
        } else {
            let html = '<div class="space-y-2">';
            this.projects.forEach(project => {
                const roleLabel = project.role === 'owner' ? '👑 オーナー' :
                    project.role === 'admin' ? '🔧 管理者' : '👤 メンバー';
                html += `
                    <div class="bg-gray-700/50 hover:bg-gray-700 rounded-xl p-4 cursor-pointer transition" 
                         data-project-id="${project.id}">
                        <div class="flex items-start justify-between">
                            <div class="flex-1">
                                <h3 class="font-medium text-white">${this.escapeHtml(project.name)}</h3>
                                <p class="text-sm text-gray-400 mt-1">${this.escapeHtml(project.description || '説明なし')}</p>
                                <div class="flex items-center gap-3 mt-2 text-xs text-gray-500">
                                    <span>${roleLabel}</span>
                                    <span>👥 ${project.member_count || 1}人</span>
                                </div>
                            </div>
                            <div class="flex gap-1">
                                ${project.role === 'owner' || project.role === 'admin' ? `
                                    <button class="pm-edit p-2 hover:bg-gray-600 rounded-lg text-gray-400 hover:text-white" 
                                            data-project-id="${project.id}" title="編集">
                                        ✏️
                                    </button>
                                ` : ''}
                                ${project.role === 'owner' ? `
                                    <button class="pm-delete p-2 hover:bg-red-600 rounded-lg text-gray-400 hover:text-white" 
                                            data-project-id="${project.id}" title="削除">
                                        🗑️
                                    </button>
                                ` : ''}
                            </div>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
            content.innerHTML = html;

            // Bind events
            content.querySelectorAll('[data-project-id]').forEach(el => {
                const projectId = el.dataset.projectId;

                // Click on project card (not buttons)
                el.addEventListener('click', (e) => {
                    if (e.target.closest('.pm-edit') || e.target.closest('.pm-delete')) return;
                    this.showProjectDetails(projectId);
                });
            });

            content.querySelectorAll('.pm-edit').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.showEditForm(btn.dataset.projectId);
                });
            });

            content.querySelectorAll('.pm-delete').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (confirm('このプロジェクトを削除しますか？')) {
                        this.deleteProject(btn.dataset.projectId);
                    }
                });
            });
        }

        footer.innerHTML = `
            <button id="pmCreateNew" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-white font-medium transition">
                + 新規作成
            </button>
        `;

        document.getElementById('pmCreateNew')?.addEventListener('click', () => this.showCreateForm());
    }

    showCreateForm() {
        const content = document.getElementById('pmContent');
        const footer = document.getElementById('pmFooter');
        const title = document.getElementById('pmTitle');
        if (!content || !footer || !title) return;

        title.textContent = '新規プロジェクト';

        content.innerHTML = `
            <form id="pmCreateForm" class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">プロジェクト名 *</label>
                    <input type="text" id="pmName" required
                           class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-2 focus:ring-emerald-500 focus:border-transparent"
                           placeholder="例: 新製品開発">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">説明</label>
                    <textarea id="pmDescription" rows="3"
                              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-2 focus:ring-emerald-500 focus:border-transparent"
                              placeholder="プロジェクトの説明（任意）"></textarea>
                </div>
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="pmAllowJoin" checked
                           class="w-4 h-4 rounded bg-gray-700 border-gray-600 text-emerald-500 focus:ring-emerald-500">
                    <label for="pmAllowJoin" class="text-sm text-gray-300">参加申請を許可</label>
                </div>
            </form>
        `;

        footer.innerHTML = `
            <button id="pmBack" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 rounded-lg text-white transition">
                戻る
            </button>
            <button id="pmSubmit" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-white font-medium transition">
                作成
            </button>
        `;

        document.getElementById('pmBack')?.addEventListener('click', () => this.renderProjectList());
        document.getElementById('pmSubmit')?.addEventListener('click', async () => {
            const name = document.getElementById('pmName')?.value?.trim();
            const description = document.getElementById('pmDescription')?.value?.trim();
            const allowJoin = document.getElementById('pmAllowJoin')?.checked;

            if (!name) {
                this.showError('プロジェクト名を入力してください');
                return;
            }

            const result = await this.createProject(name, description, allowJoin);
            if (result) {
                this.renderProjectList();
            }
        });
    }

    showEditForm(projectId) {
        const project = this.projects.find(p => p.id === projectId);
        if (!project) return;

        const content = document.getElementById('pmContent');
        const footer = document.getElementById('pmFooter');
        const title = document.getElementById('pmTitle');
        if (!content || !footer || !title) return;

        title.textContent = 'プロジェクト編集';

        content.innerHTML = `
            <form id="pmEditForm" class="space-y-4">
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">プロジェクト名 *</label>
                    <input type="text" id="pmName" required value="${this.escapeHtml(project.name)}"
                           class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-2 focus:ring-emerald-500 focus:border-transparent">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">説明</label>
                    <textarea id="pmDescription" rows="3"
                              class="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-2 focus:ring-emerald-500 focus:border-transparent">${this.escapeHtml(project.description || '')}</textarea>
                </div>
                <div class="flex items-center gap-2">
                    <input type="checkbox" id="pmAllowJoin" ${project.allow_join_requests ? 'checked' : ''}
                           class="w-4 h-4 rounded bg-gray-700 border-gray-600 text-emerald-500 focus:ring-emerald-500">
                    <label for="pmAllowJoin" class="text-sm text-gray-300">参加申請を許可</label>
                </div>
            </form>
        `;

        footer.innerHTML = `
            <button id="pmBack" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 rounded-lg text-white transition">
                戻る
            </button>
            <button id="pmSubmit" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-white font-medium transition">
                保存
            </button>
        `;

        document.getElementById('pmBack')?.addEventListener('click', () => this.renderProjectList());
        document.getElementById('pmSubmit')?.addEventListener('click', async () => {
            const name = document.getElementById('pmName')?.value?.trim();
            const description = document.getElementById('pmDescription')?.value?.trim();
            const allowJoin = document.getElementById('pmAllowJoin')?.checked;

            if (!name) {
                this.showError('プロジェクト名を入力してください');
                return;
            }

            const result = await this.updateProject(projectId, {
                name,
                description,
                allow_join_requests: allowJoin
            });
            if (result) {
                this.renderProjectList();
            }
        });
    }

    async showProjectDetails(projectId) {
        const project = this.projects.find(p => p.id === projectId);
        if (!project) return;

        this.currentProject = project;
        await this.loadMembers(projectId);

        const isAdmin = project.role === 'owner' || project.role === 'admin';
        if (isAdmin) {
            await this.loadJoinRequests(projectId);
        }

        const content = document.getElementById('pmContent');
        const footer = document.getElementById('pmFooter');
        const title = document.getElementById('pmTitle');
        if (!content || !footer || !title) return;

        title.textContent = project.name;

        let html = `
            <div class="space-y-6">
                <!-- Project Info -->
                <div class="bg-gray-700/30 rounded-xl p-4">
                    <p class="text-gray-300">${this.escapeHtml(project.description || '説明なし')}</p>
                </div>
                
                <!-- Members Section -->
                <div>
                    <h3 class="font-medium text-white mb-3 flex items-center justify-between">
                        <span>👥 メンバー (${this.members.length})</span>
                        ${isAdmin ? `
                            <button id="pmAddMember" class="text-sm px-3 py-1 bg-emerald-600 hover:bg-emerald-500 rounded-lg transition">
                                + 追加
                            </button>
                        ` : ''}
                    </h3>
                    <div class="space-y-2">
        `;

        this.members.forEach(member => {
            const roleLabel = member.role === 'owner' ? '👑 オーナー' :
                member.role === 'admin' ? '🔧 管理者' : '👤 メンバー';
            const canModify = isAdmin && member.role !== 'owner' && member.user_id !== project.owner_id;

            html += `
                <div class="flex items-center justify-between bg-gray-700/50 rounded-lg p-3">
                    <div class="flex items-center gap-3">
                        <span class="w-8 h-8 rounded-full bg-gray-600 flex items-center justify-center text-sm">
                            ${(member.display_name || member.username || '?')[0].toUpperCase()}
                        </span>
                        <div>
                            <div class="text-white">${this.escapeHtml(member.display_name || member.username)}</div>
                            <div class="text-xs text-gray-400">${roleLabel}</div>
                        </div>
                    </div>
                    ${canModify ? `
                        <div class="flex gap-1">
                            <select class="pm-role-select bg-gray-600 border-none rounded text-xs px-2 py-1" 
                                    data-user-id="${member.user_id}">
                                <option value="member" ${member.role === 'member' ? 'selected' : ''}>メンバー</option>
                                <option value="admin" ${member.role === 'admin' ? 'selected' : ''}>管理者</option>
                            </select>
                            <button class="pm-remove-member p-1 hover:bg-red-600 rounded text-gray-400 hover:text-white"
                                    data-user-id="${member.user_id}" title="削除">
                                ✕
                            </button>
                        </div>
                    ` : ''}
                </div>
            `;
        });

        html += '</div></div>';

        // Join Requests Section (admin only)
        if (isAdmin && this.joinRequests.length > 0) {
            html += `
                <div>
                    <h3 class="font-medium text-white mb-3">📨 参加申請 (${this.joinRequests.length})</h3>
                    <div class="space-y-2">
            `;

            this.joinRequests.forEach(request => {
                html += `
                    <div class="flex items-center justify-between bg-yellow-900/20 border border-yellow-700/30 rounded-lg p-3">
                        <div>
                            <div class="text-white">${this.escapeHtml(request.username)}</div>
                            <div class="text-xs text-gray-400">${this.escapeHtml(request.message || 'メッセージなし')}</div>
                        </div>
                        <div class="flex gap-2">
                            <button class="pm-approve px-3 py-1 bg-emerald-600 hover:bg-emerald-500 rounded text-sm"
                                    data-request-id="${request.id}">
                                承認
                            </button>
                            <button class="pm-reject px-3 py-1 bg-red-600 hover:bg-red-500 rounded text-sm"
                                    data-request-id="${request.id}">
                                却下
                            </button>
                        </div>
                    </div>
                `;
            });

            html += '</div></div>';
        }

        // RAG Collections Section
        html += `
            <div>
                <div class="flex items-center justify-between mb-3">
                    <h3 class="font-medium text-white">RAGコレクション</h3>
                    ${isAdmin ? `
                        <div class="flex gap-2">
                            <button id="pmRagLink" class="text-xs px-3 py-1 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-white transition">
                                + 紐付け
                            </button>
                            <button id="pmRagCreate" class="text-xs px-3 py-1 bg-blue-600 hover:bg-blue-500 rounded-lg text-white transition">
                                + 新規作成
                            </button>
                        </div>
                    ` : ''}
                </div>
                <div id="pmRagCollections">
                    <p class="text-gray-400 text-sm">読み込み中...</p>
                </div>
            </div>
        `;

        html += '</div>';
        content.innerHTML = html;

        // Bind events
        document.getElementById('pmAddMember')?.addEventListener('click', () => {
            const userId = prompt('追加するユーザーのIDを入力:');
            if (userId) {
                this.addMember(projectId, userId);
            }
        });

        content.querySelectorAll('.pm-role-select').forEach(select => {
            select.addEventListener('change', (e) => {
                this.updateMemberRole(projectId, e.target.dataset.userId, e.target.value);
            });
        });

        content.querySelectorAll('.pm-remove-member').forEach(btn => {
            btn.addEventListener('click', () => {
                if (confirm('このメンバーを削除しますか？')) {
                    this.removeMember(projectId, btn.dataset.userId);
                }
            });
        });

        content.querySelectorAll('.pm-approve').forEach(btn => {
            btn.addEventListener('click', () => {
                this.approveJoinRequest(projectId, btn.dataset.requestId);
            });
        });

        content.querySelectorAll('.pm-reject').forEach(btn => {
            btn.addEventListener('click', () => {
                const reason = prompt('却下理由（任意）:');
                this.rejectJoinRequest(projectId, btn.dataset.requestId, reason || '');
            });
        });

        // RAG collection section rendering
        const ragContainer = document.getElementById('pmRagCollections');
        if (ragContainer && window.ragCollectionManager) {
            window.ragCollectionManager.renderCollectionSection(projectId, ragContainer, isAdmin);
        }

        document.getElementById('pmRagLink')?.addEventListener('click', () => {
            if (window.ragCollectionManager) {
                window.ragCollectionManager.showLinkModal(projectId, () => {
                    const container = document.getElementById('pmRagCollections');
                    if (container) {
                        window.ragCollectionManager.renderCollectionSection(projectId, container, isAdmin);
                    }
                });
            }
        });

        document.getElementById('pmRagCreate')?.addEventListener('click', () => {
            if (window.ragCollectionManager) {
                window.ragCollectionManager.showCreateModal((newCol) => {
                    // After creation, auto-link to this project if desired
                    if (newCol && newCol.id) {
                        window.ragCollectionManager.linkCollection(projectId, newCol.id).then(() => {
                            const container = document.getElementById('pmRagCollections');
                            if (container) {
                                window.ragCollectionManager.renderCollectionSection(projectId, container, isAdmin);
                            }
                        });
                    }
                });
            }
        });

        footer.innerHTML = `
            <button id="pmBack" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 rounded-lg text-white transition">
                ← 一覧に戻る
            </button>
        `;

        document.getElementById('pmBack')?.addEventListener('click', () => {
            this.currentProject = null;
            this.renderProjectList();
        });
    }

    // ── Utility Methods ─────────────────────────────────────────────────

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
            alert(message);
        }
    }
}

// Export for use
window.ProjectManager = ProjectManager;
