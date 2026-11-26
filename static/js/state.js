let currentConfig = { tasks: [] };
        let wasConnected = false;
        let lastProfileInfo = null;
        let currentPreviewList = [];
        let currentPreviewIndex = -1;
        let currentPreviewSource = 'local';

        // Estado del Reproductor Musical Flotante
        const AUDIO_EXTS = ['mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg'];
        let musicPlaylist = [];
        let musicPlayOrder = []; // Índices de reproducción (secuencial o barajado)
        let musicOrderPos = -1;  // Posición actual dentro de musicPlayOrder
        let musicSource = 'local';
        let musicShuffle = false;
        let musicDockMode = 'normal'; // 'minimized' | 'normal' | 'maximized'
        let musicPlaylistSearchQuery = '';
        let currentCoverDataUrl = null;
        let currentCoverFilename = 'caratula.jpg';
        const prefetchedRemoteIds = new Set();
        let favoriteTracksMap = {};
        let favoritesSyncInProgress = false;
        let audioCtx = null;
        let eqFilters = [];
        let analyserNode = null;
        let eqInitialized = false;
        let lastNonZeroVolume = 85;
        let visualizerEnabled = true;
        let visualizerStyle = 'bars'; // 'bars' | 'mirror' | 'wave' (SVG de bajo impacto GPU)
        let visualizerRafId = null;
        let lastVisualizerFrameTime = 0;
        let svgVisBars = [];
        let svgVisPolyline = null;
        let svgVisGradStops = [];
        let svgVisInitialized = false;
        let beatHueSmooth = 225;
        let beatEnergySmooth = 0;
        const SVG_BARS_COUNT = 32;
        const VISUALIZER_FPS_INTERVAL = 1000 / 32; // 32 FPS ágiles con < 4% GPU
        const EQ_BANDS_CONFIG = [
            { freq: 60, type: 'lowshelf' },
            { freq: 230, type: 'peaking' },
            { freq: 910, type: 'peaking' },
            { freq: 3600, type: 'peaking' },
            { freq: 14000, type: 'highshelf' }
        ];
        const EQ_PRESETS = {
            flat: [0, 0, 0, 0, 0],
            bass: [7, 4, 0, -1, 1],
            electronic: [6, 3, -2, 3, 6],
            pop: [-1, 2, 4, 2, -1],
            rock: [5, 2, -2, 3, 5],
            vocal: [-2, -1, 4, 5, 2]
        };

        // Estado de los Exploradores Estilo Windows (Árbol Lazy + Contenido)
        let localNodes = new Map();
        let localSelectedPath = '';
        let localActiveDriveRoot = '';
        let remoteNodes = new Map();
        let remoteBaseRootId = 'ROOT';
        let remoteSelectedKey = 'ROOT';
        let activeContextTarget = null;
        let renamePendingTarget = null;

        // Estado de la Ventana Flotante de Transferencias (Carga / Descarga)
        let transferDockMode = 'normal'; // 'minimized' | 'normal' | 'maximized'
        let activeTransferPollTimer = null;

        // Estado de la Ventana Flotante de Escaneo de Audio para Playlist
        let activeAudioScanPollTimer = null;
        let audioScanDockClosedByUser = false;

        // Estado de Ordenamiento por Explorador ('name' | 'size' | 'mtime' | 'status')
        const explorerSortState = {
            local: { field: 'name', dir: 'asc' },
            remote: { field: 'name', dir: 'asc' }
        };

        function updateSortHeaderIcons(source) {
            const state = explorerSortState[source];
            const activeColor = source === 'local' ? 'text-sky-400' : 'text-indigo-400';
            ['name', 'size', 'mtime', 'status'].forEach(col => {
                const iconEl = document.getElementById(`sortIcon_${source}_${col}`);
                if (!iconEl) return;
                if (state.field === col) {
                    iconEl.textContent = state.dir === 'asc' ? '↑' : '↓';
                    iconEl.className = `${activeColor} font-bold text-[11px]`;
                } else {
                    iconEl.textContent = '⇅';
                    iconEl.className = 'text-slate-500 text-[11px]';
                }
            });
        }

        function sortExplorerItems(items, source) {
            const state = explorerSortState[source];
            const mult = state.dir === 'asc' ? 1 : -1;
            const statusOrder = { modified: 1, new: 2, remote_only: 3, local_only: 4, synced: 5 };

            return [...items].sort((a, b) => {
                // Mantener carpetas separadas arriba de los archivos
                if (a.type !== b.type) {
                    return a.type === 'folder' ? -1 : 1;
                }
                if (state.field === 'size') {
                    const diff = (Number(a.size || 0) - Number(b.size || 0));
                    if (diff !== 0) return diff * mult;
                } else if (state.field === 'mtime') {
                    const diff = (Number(a.mtime || 0) - Number(b.mtime || 0));
                    if (diff !== 0) return diff * mult;
                } else if (state.field === 'status') {
                    const sa = statusOrder[a.sync_status] || 99;
                    const sb = statusOrder[b.sync_status] || 99;
                    if (sa !== sb) return (sa - sb) * mult;
                    if (source === 'remote' && a.id !== b.id) {
                        return (Number(a.id || 0) - Number(b.id || 0)) * mult;
                    }
                }
                return (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' }) * mult;
            });
        }

        function toggleExplorerSort(source, field) {
            const state = explorerSortState[source];
            if (state.field === field) {
                state.dir = state.dir === 'asc' ? 'desc' : 'asc';
            } else {
                state.field = field;
                state.dir = (field === 'size' || field === 'mtime') ? 'desc' : 'asc';
            }
            updateSortHeaderIcons(source);
            if (source === 'local') {
                renderLocalContentsPanel();
            } else {
                renderRemoteContentsPanel();
            }
        }

        function formatBytes(bytes) {
            const num = Number(bytes || 0);
            if (num === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(num) / Math.log(k));
            return `${parseFloat((num / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
        }

        function getStatusBadge(status) {
            const badges = {
                synced: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/20">Sincronizado</span>',
                new: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-sky-500/15 text-sky-400 border border-sky-500/20">Nuevo</span>',
                modified: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/15 text-amber-400 border border-amber-500/20">Modificado</span>',
                remote_only: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-purple-500/15 text-purple-400 border border-purple-500/20">Solo Remoto</span>',
                local_only: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-700/50 text-slate-300 border border-slate-600/40">Disco Local</span>'
            };
            return badges[status] || '';
        }

        function showNotice(title, message, onConfirm = null) {
            document.getElementById('noticeTitle').textContent = title;
            document.getElementById('noticeMessage').textContent = message;
            const actions = document.getElementById('noticeActions');
            actions.innerHTML = '';

            if (onConfirm) {
                const cancelBtn = document.createElement('button');
                cancelBtn.className = 'px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300';
                cancelBtn.textContent = 'Cancelar';
                cancelBtn.onclick = closeNoticeModal;
                actions.appendChild(cancelBtn);

                const confirmBtn = document.createElement('button');
                confirmBtn.className = 'px-3.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white';
                confirmBtn.textContent = 'Confirmar y Forzar';
                confirmBtn.onclick = () => { closeNoticeModal(); onConfirm(); };
                actions.appendChild(confirmBtn);
            } else {
                const okBtn = document.createElement('button');
                okBtn.className = 'px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white';
                okBtn.textContent = 'Entendido';
                okBtn.onclick = closeNoticeModal;
                actions.appendChild(okBtn);
            }

            document.getElementById('noticeModal').classList.remove('hidden');
            document.getElementById('noticeModal').classList.add('flex');
        }

        function closeNoticeModal() {
            document.getElementById('noticeModal').classList.add('hidden');
            document.getElementById('noticeModal').classList.remove('flex');
        }


async function fetchUserStorageInfo() {
            const widget = document.getElementById('userStorageWidget');
            if (!widget) return;
            try {
                const res = await fetch('/api/user/profile');
                const data = await res.json();
                if (!data.connected) {
                    widget.classList.add('hidden');
                    return;
                }
                lastProfileInfo = data;
                document.getElementById('headerUserName').textContent = data.user_name || 'Cuenta FileJump';
                const emailEl = document.getElementById('headerUserEmail');
                if (emailEl) {
                    emailEl.textContent = data.user_email ? `(${data.user_email})` : '';
                }
                const avatarWrap = document.getElementById('headerUserAvatarWrap');
                if (avatarWrap) {
                    avatarWrap.innerHTML = data.user_avatar
                        ? `<img src="${escapeAttr(data.user_avatar)}" alt="Avatar" class="w-full h-full object-cover">`
                        : '👤';
                }
                document.getElementById('headerStorageUsed').textContent = formatBytes(data.used_bytes || 0);
                document.getElementById('headerStorageFree').textContent = data.total_bytes > 0 ? formatBytes(data.free_bytes || 0) : 'N/D';
                document.getElementById('headerStorageTotal').textContent = data.total_bytes > 0 ? formatBytes(data.total_bytes) : 'N/D';

                const bar = document.getElementById('headerStorageBar');
                const pct = data.total_bytes > 0 ? Math.min(100, Math.max(2, data.used_pct || 0)) : 8;
                bar.style.width = `${pct}%`;
                if ((data.used_pct || 0) >= 90) {
                    bar.className = 'h-full bg-gradient-to-r from-amber-500 to-rose-500 rounded-full transition-all duration-500';
                } else {
                    bar.className = 'h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-purple-500 rounded-full transition-all duration-500';
                }
                widget.classList.remove('hidden');
            } catch (e) {
                widget.classList.add('hidden');
            }
        }

        function openAccountQuotaModal() {
            const modal = document.getElementById('accountQuotaModal');
            if (!modal) return;
            const info = lastProfileInfo || {};
            document.getElementById('quotaUserNameInput').value = info.user_name || '';
            document.getElementById('readonlyModalAccountName').textContent = info.user_name || 'Cuenta FileJump';
            document.getElementById('readonlyModalAccountEmail').textContent = info.user_email || 'Sin correo asociado';
            document.getElementById('readonlyModalAccountPlan').textContent = info.user_role || 'Cuenta Activa';
            const modalAvatar = document.getElementById('modalUserAvatarWrap');
            if (modalAvatar) {
                modalAvatar.innerHTML = info.user_avatar
                    ? `<img src="${escapeAttr(info.user_avatar)}" alt="Avatar" class="w-full h-full object-cover">`
                    : '👤';
            }
            document.getElementById('readonlyModalUsed').textContent = formatBytes(info.used_bytes || 0);
            document.getElementById('readonlyModalFree').textContent = info.total_bytes > 0 ? formatBytes(info.free_bytes || 0) : 'N/D';
            document.getElementById('readonlyModalTotal').textContent = info.total_bytes > 0 ? formatBytes(info.total_bytes) : 'N/D';
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function closeAccountQuotaModal() {
            const modal = document.getElementById('accountQuotaModal');
            if (!modal) return;
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        async function saveAccountQuota() {
            const userName = document.getElementById('quotaUserNameInput').value.trim();
            await fetch('/api/user/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_name: userName })
            });
            closeAccountQuotaModal();
            await fetchUserStorageInfo();
        }

        async function refreshStatusAndLogs() {
            const dot = document.getElementById('apiStatusDot');
            const txt = document.getElementById('apiStatusText');
            try {
                const res = await fetch('/api/status');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (data.connected) {
                    dot.className = 'w-2 h-2 rounded-full bg-emerald-400';
                    txt.textContent = `Conectado (${data.masked_key})`;
                    if (!wasConnected) {
                        wasConnected = true;
                        fetchUserStorageInfo();
                        fetchRemoteDirectories(true);
                        loadRemoteMusicFavorites();
                    }
                } else {
                    wasConnected = false;
                    const widget = document.getElementById('userStorageWidget');
                    if (widget) widget.classList.add('hidden');
                    dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                    txt.textContent = data.has_key ? 'Error de Autenticación' : 'Sin API Key (.env)';
                }

                const consoleEl = document.getElementById('logConsole');
                if (data.logs && data.logs.length > 0) {
                    consoleEl.innerHTML = data.logs.map(l => {
                        const color = l.level === 'error' ? 'text-rose-400' : (l.level === 'success' ? 'text-emerald-400' : (l.level === 'warning' ? 'text-amber-400' : 'text-slate-300'));
                        return `<div class="${color}"><span class="text-slate-500">[${l.time}]</span> ${l.message}</div>`;
                    }).join('');
                    consoleEl.scrollTop = consoleEl.scrollHeight;
                }
            } catch (e) {
                wasConnected = false;
                if (dot) dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                if (txt) txt.textContent = 'Sin conexión con servidor local';
            }
        }

        async function fetchRemoteDirectories(autoBrowseIfNoTask = false) {
            const selector = document.getElementById('remoteDirSelector');
            if (!selector) return;
            try {
                const res = await fetch('/api/remote/directories');
                const data = await res.json();
                if (!data.connected) return;

                const prevVal = selector.value;
                selector.innerHTML = `
                    <option value="ROOT">/ (Raíz de FileJump)</option>
                    <option value="">Auto (Carpeta de Tarea)</option>
                `;
                (data.directories || []).forEach(dir => {
                    const opt = document.createElement('option');
                    opt.value = String(dir.id);
                    opt.textContent = `📁 ${dir.name} (#${dir.id})`;
                    selector.appendChild(opt);
                });

                if (prevVal && Array.from(selector.options).some(o => o.value === prevVal)) {
                    selector.value = prevVal;
                    if (autoBrowseIfNoTask && prevVal === 'ROOT') {
                        inspectSelectedTask('ROOT');
                    }
                } else {
                    selector.value = 'ROOT';
                    inspectSelectedTask('ROOT');
                }
            } catch (e) {}
        }

        function onRemoteDirectoryChange() {
            const selectedDirId = document.getElementById('remoteDirSelector').value;
            inspectSelectedTask(selectedDirId);
        }

        async function fetchLocalDrives(autoSelectFirst = false) {
            const selector = document.getElementById('localDriveSelector');
            if (!selector) return;
            try {
                const res = await fetch('/api/local/drives');
                const data = await res.json();
                const drives = data.drives || [];
                const prevVal = selector.value;

                selector.innerHTML = '<option value="">Auto (Carpeta de Tarea)</option>';
                drives.forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.id;
                    opt.textContent = `💽 ${d.name}`;
                    selector.appendChild(opt);
                });

                if (prevVal && Array.from(selector.options).some(o => o.value === prevVal)) {
                    selector.value = prevVal;
                    if (autoSelectFirst && prevVal) {
                        await onLocalDriveChange();
                    }
                } else if (autoSelectFirst && !document.getElementById('taskSelector').value && drives.length > 0) {
                    selector.value = drives[0].id;
                    await onLocalDriveChange();
                }
            } catch (e) {}
        }

        async function onLocalDriveChange() {
            const selector = document.getElementById('localDriveSelector');
            if (!selector) return;
            const drivePath = selector.value;
            localActiveDriveRoot = drivePath;

            if (!drivePath) {
                const taskName = document.getElementById('taskSelector').value;
                if (taskName) {
                    inspectSelectedTask();
                } else {
                    document.getElementById('localPathSubtitle').textContent = 'Sin ruta seleccionada';
                    initLocalExplorer([]);
                }
                return;
            }

            try {
                const res = await fetch(`/api/local/browse?root_path=${encodeURIComponent(drivePath)}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Aviso', data.error);
                    return;
                }
                document.getElementById('localPathSubtitle').textContent = drivePath;
                initLocalExplorer(data.items || []);
                const rootNode = localNodes.get('');
                if (rootNode) {
                    rootNode.name = drivePath;
                    rootNode.full_path = drivePath;
                    renderLocalTreePanel();
                }
            } catch (e) {
                showNotice('Error', 'No se pudo explorar la unidad local seleccionada.');
            }
        }

        async function syncCachedRemoteIds() {
            try {
                const res = await fetch('/api/cache/status');
                const data = await res.json();
                (data.cached_ids || []).forEach(id => prefetchedRemoteIds.add(String(id)));
            } catch (e) {}
        }
