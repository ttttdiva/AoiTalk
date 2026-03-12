/**
 * RAG Collection Manager - Manages vector DB collections and their project linkages.
 */
class RagCollectionManager {
    constructor() {
        this.collections = [];
        this._pollingTimers = {};
    }

    // ── API Methods ─────────────────────────────────────────────────────

    async loadCollections() {
        try {
            const res = await fetch('/api/rag/collections');
            const data = await res.json();
            if (data.success) {
                this.collections = data.collections || [];
            }
            return this.collections;
        } catch (e) {
            console.error('Failed to load RAG collections:', e);
            return [];
        }
    }

    async createCollection(name, description, sourceDirectory, includePatterns, autoIndex) {
        try {
            const res = await fetch('/api/rag/collections', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    description: description || null,
                    source_directory: sourceDirectory,
                    include_patterns: includePatterns || null,
                    auto_index: autoIndex !== false
                })
            });
            return await res.json();
        } catch (e) {
            console.error('Failed to create collection:', e);
            return { success: false, error: e.message };
        }
    }

    async deleteCollection(collectionId) {
        try {
            const res = await fetch(`/api/rag/collections/${collectionId}`, { method: 'DELETE' });
            return await res.json();
        } catch (e) {
            console.error('Failed to delete collection:', e);
            return { success: false };
        }
    }

    async startIndexing(collectionId, clearExisting = false) {
        try {
            const res = await fetch(`/api/rag/collections/${collectionId}/index`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clear_existing: clearExisting })
            });
            return await res.json();
        } catch (e) {
            console.error('Failed to start indexing:', e);
            return { success: false };
        }
    }

    async getIndexStatus(collectionId) {
        try {
            const res = await fetch(`/api/rag/collections/${collectionId}/index/status`);
            return await res.json();
        } catch (e) {
            return null;
        }
    }

    async loadProjectCollections(projectId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/rag-collections`);
            const data = await res.json();
            return data.success ? (data.collections || []) : [];
        } catch (e) {
            console.error('Failed to load project collections:', e);
            return [];
        }
    }

    async linkCollection(projectId, collectionId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/rag-collections`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ collection_id: collectionId })
            });
            return await res.json();
        } catch (e) {
            console.error('Failed to link collection:', e);
            return { success: false };
        }
    }

    async unlinkCollection(projectId, collectionId) {
        try {
            const res = await fetch(`/api/projects/${projectId}/rag-collections/${collectionId}`, {
                method: 'DELETE'
            });
            return await res.json();
        } catch (e) {
            console.error('Failed to unlink collection:', e);
            return { success: false };
        }
    }

    async discoverCollections() {
        try {
            const res = await fetch('/api/rag/collections/discover');
            const data = await res.json();
            return data.success ? (data.unregistered || []) : [];
        } catch (e) {
            console.error('Failed to discover collections:', e);
            return [];
        }
    }

    async importCollection(qdrantName, displayName) {
        try {
            const res = await fetch('/api/rag/collections/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ qdrant_name: qdrantName, display_name: displayName })
            });
            return await res.json();
        } catch (e) {
            console.error('Failed to import collection:', e);
            return { success: false };
        }
    }

    // ── UI: Project detail RAG section ───────────────────────────────────

    async renderCollectionSection(projectId, containerEl, isAdmin) {
        const linked = await this.loadProjectCollections(projectId);

        let html = '';
        if (linked.length === 0) {
            html += '<p class="text-gray-400 text-sm">紐付けられたコレクションはありません</p>';
        } else {
            html += '<div class="space-y-2">';
            linked.forEach(col => {
                const statusBadge = this._statusBadge(col.status);
                const pts = col.points_count || 0;
                const srcDir = col.source_directory || '(未設定)';
                const lastIdx = col.last_indexed_at
                    ? new Date(col.last_indexed_at).toLocaleString('ja-JP')
                    : '未実行';

                html += `
                    <div class="bg-gray-700/50 rounded-lg p-3" data-col-id="${col.id}">
                        <div class="flex items-center justify-between mb-1">
                            <span class="text-white font-medium">${this._esc(col.name)}</span>
                            <div class="flex items-center gap-2">
                                ${statusBadge}
                                ${isAdmin ? `
                                    <button class="rcm-reindex text-xs px-2 py-0.5 bg-blue-600 hover:bg-blue-500 rounded transition"
                                            data-col-id="${col.id}" title="再インデックス">再構築</button>
                                    <button class="rcm-unlink text-xs px-2 py-0.5 bg-red-600/70 hover:bg-red-500 rounded transition"
                                            data-col-id="${col.id}" title="紐付け解除">解除</button>
                                ` : ''}
                            </div>
                        </div>
                        <div class="text-xs text-gray-400 space-y-0.5">
                            <div>${pts.toLocaleString()} docs | ${this._esc(srcDir)}</div>
                            <div>最終インデックス: ${lastIdx}</div>
                        </div>
                        <div class="rcm-status-area" data-col-id="${col.id}"></div>
                    </div>
                `;
            });
            html += '</div>';
        }

        containerEl.innerHTML = html;

        // Bind events
        containerEl.querySelectorAll('.rcm-unlink').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm('このコレクションの紐付けを解除しますか？')) return;
                await this.unlinkCollection(projectId, btn.dataset.colId);
                await this.renderCollectionSection(projectId, containerEl, isAdmin);
            });
        });

        containerEl.querySelectorAll('.rcm-reindex').forEach(btn => {
            btn.addEventListener('click', async () => {
                const clear = confirm('既存インデックスをクリアして再構築しますか？\n（キャンセルで差分更新）');
                await this.startIndexing(btn.dataset.colId, clear);
                this._pollStatus(btn.dataset.colId, containerEl.querySelector(
                    `.rcm-status-area[data-col-id="${btn.dataset.colId}"]`
                ));
            });
        });

        // Start polling for any indexing collections
        linked.filter(c => c.status === 'indexing').forEach(col => {
            const area = containerEl.querySelector(`.rcm-status-area[data-col-id="${col.id}"]`);
            if (area) this._pollStatus(col.id, area);
        });
    }

    // ── UI: Link collection modal ────────────────────────────────────────

    async showLinkModal(projectId, onDone) {
        // Load registered collections and discover unregistered Qdrant collections in parallel
        const [_, linked, unregistered] = await Promise.all([
            this.loadCollections(),
            this.loadProjectCollections(projectId),
            this.discoverCollections(),
        ]);
        const linkedIds = new Set(linked.map(c => c.id));
        const available = this.collections.filter(c => !linkedIds.has(c.id));

        // Build registered section HTML
        let registeredHtml = '';
        if (available.length > 0) {
            registeredHtml = available.map(col => `
                <div class="flex items-center justify-between bg-gray-700/50 rounded-lg p-3 mb-2">
                    <div>
                        <div class="text-white">${this._esc(col.name)}</div>
                        <div class="text-xs text-gray-400">
                            ${this._statusBadge(col.status)} ${(col.points_count || 0).toLocaleString()} docs
                            | ${this._esc(col.source_directory || '')}
                        </div>
                    </div>
                    <button class="rcm-do-link text-sm px-3 py-1 bg-emerald-600 hover:bg-emerald-500 rounded-lg transition"
                            data-col-id="${col.id}">紐付け</button>
                </div>
            `).join('');
        } else {
            registeredHtml = '<p class="text-gray-500 text-sm">登録済みの未紐付けコレクションはありません</p>';
        }

        // Build unregistered section HTML
        let unregisteredHtml = '';
        if (unregistered.length > 0) {
            unregisteredHtml = unregistered.map(col => `
                <div class="flex items-center justify-between bg-gray-700/50 rounded-lg p-3 mb-2 border border-dashed border-violet-500/40">
                    <div>
                        <div class="text-violet-300">${this._esc(col.name)}</div>
                        <div class="text-xs text-gray-400">
                            ${(col.points_count || 0).toLocaleString()} points | ${this._esc(col.status || 'unknown')}
                        </div>
                    </div>
                    <button class="rcm-import-link text-sm px-3 py-1 bg-violet-600 hover:bg-violet-500 rounded-lg transition whitespace-nowrap"
                            data-qdrant-name="${this._esc(col.name)}">インポート&amp;紐付け</button>
                </div>
            `).join('');
        }

        const overlay = document.createElement('div');
        overlay.className = 'fixed inset-0 bg-black/70 z-[60] flex items-center justify-center';
        overlay.innerHTML = `
            <div class="bg-gray-800 rounded-2xl w-full max-w-lg mx-4 max-h-[70vh] flex flex-col">
                <div class="p-4 border-b border-gray-700 flex items-center justify-between">
                    <h3 class="text-white font-medium">RAGコレクションを紐付け</h3>
                    <button id="rcmLinkClose" class="text-gray-400 hover:text-white text-xl">&times;</button>
                </div>
                <div class="p-4 overflow-y-auto flex-1">
                    <p class="text-xs text-gray-400 font-semibold uppercase tracking-wide mb-2">登録済みコレクション</p>
                    ${registeredHtml}

                    ${unregisteredHtml ? `
                        <div class="border-t border-gray-700 my-3"></div>
                        <p class="text-xs text-violet-400 font-semibold uppercase tracking-wide mb-2">未登録のQdrantコレクション</p>
                        ${unregisteredHtml}
                    ` : ''}
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        overlay.querySelector('#rcmLinkClose').addEventListener('click', () => {
            overlay.remove();
        });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) overlay.remove();
        });

        // Bind link buttons for registered collections
        overlay.querySelectorAll('.rcm-do-link').forEach(btn => {
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                btn.textContent = '...';
                await this.linkCollection(projectId, btn.dataset.colId);
                overlay.remove();
                if (onDone) onDone();
            });
        });

        // Bind import+link buttons for unregistered Qdrant collections
        overlay.querySelectorAll('.rcm-import-link').forEach(btn => {
            btn.addEventListener('click', async () => {
                const qdrantName = btn.dataset.qdrantName;
                btn.disabled = true;
                btn.textContent = '処理中...';

                // Import (register in DB)
                const importResult = await this.importCollection(qdrantName, qdrantName);
                if (!importResult.success) {
                    btn.textContent = 'エラー';
                    btn.classList.replace('bg-violet-600', 'bg-red-600');
                    return;
                }

                // Link to project
                await this.linkCollection(projectId, importResult.collection.id);
                overlay.remove();
                if (onDone) onDone();
            });
        });
    }

    // ── UI: Create collection modal ──────────────────────────────────────

    showCreateModal(onDone) {
        const overlay = document.createElement('div');
        overlay.className = 'fixed inset-0 bg-black/70 z-[60] flex items-center justify-center';
        overlay.innerHTML = `
            <div class="bg-gray-800 rounded-2xl w-full max-w-lg mx-4">
                <div class="p-4 border-b border-gray-700 flex items-center justify-between">
                    <h3 class="text-white font-medium">RAGコレクション新規作成</h3>
                    <button id="rcmCreateClose" class="text-gray-400 hover:text-white text-xl">&times;</button>
                </div>
                <div class="p-4 space-y-4">
                    <div>
                        <label class="block text-sm text-gray-300 mb-1">名前 *</label>
                        <input id="rcmName" type="text" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white"
                               placeholder="例: 社内マニュアル">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-300 mb-1">説明</label>
                        <input id="rcmDesc" type="text" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white"
                               placeholder="任意の説明">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-300 mb-1">ソースディレクトリ *</label>
                        <input id="rcmDir" type="text" class="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white"
                               placeholder="例: C:/docs/manuals">
                    </div>
                    <div class="flex items-center gap-2">
                        <input id="rcmAutoIndex" type="checkbox" checked class="rounded">
                        <label for="rcmAutoIndex" class="text-sm text-gray-300">作成後にインデックスを自動開始</label>
                    </div>
                </div>
                <div class="p-4 border-t border-gray-700 flex justify-end gap-2">
                    <button id="rcmCreateCancel" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 rounded-lg text-white transition">
                        キャンセル
                    </button>
                    <button id="rcmCreateSubmit" class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-white transition">
                        作成
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('#rcmCreateClose').addEventListener('click', close);
        overlay.querySelector('#rcmCreateCancel').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        overlay.querySelector('#rcmCreateSubmit').addEventListener('click', async () => {
            const name = overlay.querySelector('#rcmName').value.trim();
            const dir = overlay.querySelector('#rcmDir').value.trim();
            if (!name || !dir) {
                alert('名前とソースディレクトリは必須です');
                return;
            }
            const desc = overlay.querySelector('#rcmDesc').value.trim();
            const autoIndex = overlay.querySelector('#rcmAutoIndex').checked;

            overlay.querySelector('#rcmCreateSubmit').disabled = true;
            overlay.querySelector('#rcmCreateSubmit').textContent = '作成中...';

            const result = await this.createCollection(name, desc, dir, null, autoIndex);
            close();
            if (result.success && onDone) onDone(result.collection);
        });
    }

    // ── Helpers ──────────────────────────────────────────────────────────

    _statusBadge(status) {
        const map = {
            ready: '<span class="text-emerald-400 text-xs">● ready</span>',
            indexing: '<span class="text-yellow-400 text-xs animate-pulse">● indexing</span>',
            error: '<span class="text-red-400 text-xs">● error</span>',
            empty: '<span class="text-gray-500 text-xs">● empty</span>',
        };
        return map[status] || `<span class="text-gray-500 text-xs">● ${status}</span>`;
    }

    _esc(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    _pollStatus(collectionId, statusAreaEl) {
        if (!statusAreaEl) return;
        // Clear existing timer
        if (this._pollingTimers[collectionId]) {
            clearInterval(this._pollingTimers[collectionId]);
        }

        statusAreaEl.innerHTML = '<div class="text-yellow-400 text-xs mt-1 animate-pulse">インデックス作成中...</div>';

        this._pollingTimers[collectionId] = setInterval(async () => {
            const res = await this.getIndexStatus(collectionId);
            if (!res || !res.task) return;

            const t = res.task;
            if (t.status === 'completed' || t.status === 'ready') {
                statusAreaEl.innerHTML = '<div class="text-emerald-400 text-xs mt-1">インデックス完了</div>';
                clearInterval(this._pollingTimers[collectionId]);
                delete this._pollingTimers[collectionId];
            } else if (t.status === 'error') {
                statusAreaEl.innerHTML = `<div class="text-red-400 text-xs mt-1">エラー: ${this._esc(t.error_message || '不明')}</div>`;
                clearInterval(this._pollingTimers[collectionId]);
                delete this._pollingTimers[collectionId];
            } else if (t.status === 'running') {
                const info = t.files_processed ? `${t.files_processed} files / ${t.total_chunks} chunks` : '';
                statusAreaEl.innerHTML = `<div class="text-yellow-400 text-xs mt-1 animate-pulse">インデックス作成中... ${info}</div>`;
            }
        }, 3000);
    }
}

// Global instance
window.ragCollectionManager = new RagCollectionManager();
