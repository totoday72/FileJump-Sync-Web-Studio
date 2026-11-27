// =========================================================================
        // INSPECCIÓN DE TAREAS, SINCRONIZACIÓN Y CONFIGURACIÓN (.ENV + CACHÉ)
        // =========================================================================
        async function loadInitialConfig() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                currentConfig = data.config || { tasks: [] };
                const sel = document.getElementById('taskSelector');
                const prev = sel.value;
                sel.innerHTML = '<option value="">-- Selecciona o crea una tarea --</option>';
                (currentConfig.tasks || []).forEach(t => {
                    const opt = document.createElement('option');
                    opt.value = t.name;
                    opt.textContent = `${t.name} (${t.path})`;
                    sel.appendChild(opt);
                });
                if (prev && Array.from(sel.options).some(o => o.value === prev)) {
                    sel.value = prev;
                }
            } catch (e) {}
        }

        async function inspectSelectedTask(overrideRemoteId = '') {
            const taskName = document.getElementById('taskSelector').value;
            const remoteSelectorVal = overrideRemoteId || document.getElementById('remoteDirSelector').value || 'ROOT';

            if (!taskName) {
                document.getElementById('lastSyncLabel').textContent = '';
                if (remoteSelectorVal) {
                    try {
                        const res = await fetch(`/api/remote/browse?remote_id=${encodeURIComponent(remoteSelectorVal)}`);
                        const data = await res.json();
                        const label = remoteSelectorVal === 'ROOT' ? '/ (Raíz de FileJump)' : `Carpeta #${remoteSelectorVal}`;
                        initRemoteExplorer(data.remote_items || [], remoteSelectorVal, label);
                    } catch (e) {}
                }
                return;
            }

            try {
                const taskObj = (currentConfig.tasks || []).find(t => t.name === taskName);
                const preferredRemoteId = overrideRemoteId || (taskObj && taskObj.remote_folder_id ? String(taskObj.remote_folder_id) : '');
                const q = preferredRemoteId ? `?remote_id=${encodeURIComponent(preferredRemoteId)}` : '';
                const res = await fetch(`/api/inspect/${encodeURIComponent(taskName)}${q}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Aviso', data.error);
                    return;
                }

                const dirMapLabel = {
                    local_to_remote: '➡ Local → FileJump',
                    remote_to_local: '⬅ FileJump → Local',
                    bidirectional: '⇄ Bidireccional'
                };
                const dirBadge = dirMapLabel[(data.task && data.task.sync_direction) || 'local_to_remote'] || '➡ Local → FileJump';
                const syncTimeText = data.last_sync
                    ? `Última sync: ${new Date(data.last_sync).toLocaleString()}`
                    : 'Sin sincronizaciones previas';
                document.getElementById('lastSyncLabel').textContent = `${dirBadge} • ${syncTimeText}`;

                // Cambiar el explorador local automáticamente a la carpeta de la tarea seleccionada
                const driveSel = document.getElementById('localDriveSelector');
                if (driveSel) {
                    driveSel.value = '';
                }
                localActiveDriveRoot = '';
                document.getElementById('localPathSubtitle').textContent = data.task.path;
                initLocalExplorer(data.local_items || []);

                // Cambiar el explorador remoto automáticamente a la carpeta destino de la tarea
                const rId = String(data.remote_root_id || preferredRemoteId || 'ROOT');
                const rLabel = (data.task && data.task.remote_path)
                    ? data.task.remote_path
                    : (rId === 'ROOT' ? '/ (Raíz de FileJump)' : `${data.task.name} (#${rId})`);
                initRemoteExplorer(data.remote_items || [], rId, rLabel);
            } catch (e) {
                showNotice('Error', 'No se pudo inspeccionar la tarea seleccionada.');
            }
        }

        async function triggerSync(force = false) {
            const taskName = document.getElementById('taskSelector').value;
            if (!taskName) {
                showNotice('Selecciona una tarea', 'Elige primero una tarea activa antes de sincronizar.');
                return;
            }
            const btn = document.getElementById('syncBtn');
            btn.disabled = true;
            btn.classList.add('opacity-60');
            try {
                const res = await fetch(`/api/sync/${encodeURIComponent(taskName)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force })
                });
                const data = await res.json();
                if (data.requires_confirmation) {
                    showNotice('Confirmación de Seguridad', data.message, () => triggerSync(true));
                } else if (data.success) {
                    const prevLocalRel = localSelectedPath;
                    const prevRemoteKey = remoteSelectedKey;
                    await inspectSelectedTask();
                    if (prevLocalRel) {
                        await selectLocalFolder(prevLocalRel);
                    }
                    if (prevRemoteKey && prevRemoteKey !== remoteBaseRootId && remoteNodes.has(String(prevRemoteKey))) {
                        await selectRemoteNode(prevRemoteKey);
                    }
                    fetchUserStorageInfo();
                    const up = data.uploaded || 0;
                    const dn = data.downloaded || 0;
                    const tot = data.total || (up + dn);
                    let detailMsg = `Se subieron ${up} de ${tot} archivo(s) pendientes.`;
                    if (data.direction === 'remote_to_local') {
                        detailMsg = `Se descargaron ${dn} de ${tot} archivo(s) desde FileJump hacia Local.`;
                    } else if (data.direction === 'bidirectional') {
                        detailMsg = `Sincronización bidireccional completada: ${up} subido(s) y ${dn} descargado(s).`;
                    }
                    showNotice('Sincronización Completa', detailMsg);
                } else {
                    showNotice('Error de Sincronización', data.error || 'Ocurrió un problema durante la sincronización.');
                }
            } catch (e) {
                showNotice('Error de Red', 'No se pudo comunicar con el servidor local.');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-60');
            }
        }

        async function refreshCacheUsageDisplay() {
            const label = document.getElementById('cacheUsageSummary');
            if (!label) return;
            try {
                const res = await fetch('/api/cache/clear');
                const data = await res.json();
                label.textContent = `${data.file_count || 0} archivo(s) en caché • ${formatBytes(data.total_bytes || 0)} ocupados en disco`;
            } catch (e) {
                label.textContent = 'No se pudo calcular el tamaño de la caché.';
            }
        }

        function confirmAndClearLocalCache() {
            showNotice(
                'Eliminar Caché Definitivamente',
                'Se eliminarán permanentemente todos los archivos almacenados en temp_cache sin pasar por la Papelera de Reciclaje. ¿Continuar?',
                async () => {
                    const res = await fetch('/api/cache/clear', { method: 'POST' });
                    const data = await res.json();
                    if (data.success) {
                        prefetchedRemoteIds.clear();
                        if (musicSource === 'remote' && musicPlaylist.length > 0) {
                            renderMusicPlaylist();
                        }
                        await refreshCacheUsageDisplay();
                        showNotice('Caché Eliminada', `Se borraron definitivamente ${data.deleted_count} archivo(s) (${formatBytes(data.freed_bytes)} liberados).`);
                    }
                }
            );
        }

        async function openSettingsModal() {
            const modal = document.getElementById('settingsModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            refreshCacheUsageDisplay();

            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                currentConfig = data.config || { tasks: [] };
                document.getElementById('envApiKey').value = '';
                document.getElementById('envBaseUrl').value = data.env.FILEJUMP_BASE_URL || 'https://filejump.com/api';
                document.getElementById('envUploadUrl').value = data.env.FILEJUMP_UPLOAD_URL || 'https://uploads.filejump.com/api/upload';
                document.getElementById('cfgWorkers').value = currentConfig.max_workers || 4;
                document.getElementById('cfgThreshold').value = currentConfig.min_impact_percentage || 40;
                document.getElementById('cfgHighSpeed').checked = currentConfig.use_high_speed_upload !== false;

                const container = document.getElementById('tasksListContainer');
                container.innerHTML = '';
                (currentConfig.tasks || []).forEach(t => addTaskRow(t.name, t.path, t.min_impact_percentage));
            } catch (e) {}
        }

        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.add('hidden');
            document.getElementById('settingsModal').classList.remove('flex');
        }

        function addTaskRow(name = '', path = '', threshold = 40) {
            const container = document.getElementById('tasksListContainer');
            const row = document.createElement('div');
            row.className = 'task-row grid grid-cols-1 sm:grid-cols-12 gap-2 bg-slate-950 p-2.5 rounded-xl border border-slate-800 items-center';
            row.innerHTML = `
                <input type="text" placeholder="Nombre tarea" value="${escapeAttr(name)}" class="task-name sm:col-span-3 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                <input type="text" placeholder="Ruta local (ej. C:\\MisArchivos)" value="${escapeAttr(path)}" class="task-path sm:col-span-6 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200">
                <input type="number" min="1" max="100" value="${threshold}" title="Umbral %" class="task-threshold sm:col-span-2 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                <button type="button" onclick="this.parentElement.remove()" class="sm:col-span-1 text-rose-400 hover:text-rose-300 text-sm font-bold text-center" title="Quitar">✕</button>
            `;
            container.appendChild(row);
        }

        async function saveSettings() {
            const tasks = [];
            document.querySelectorAll('#tasksListContainer .task-row').forEach(row => {
                const name = row.querySelector('.task-name').value.trim();
                const path = row.querySelector('.task-path').value.trim();
                const min_impact_percentage = Number(row.querySelector('.task-threshold').value || 40);
                if (name && path) {
                    tasks.push({ name, path, min_impact_percentage });
                }
            });

            const existingTasksMap = new Map((currentConfig.tasks || []).map(t => [t.name, t]));
            const mergedTasks = tasks.map(t => {
                const prev = existingTasksMap.get(t.name) || {};
                return {
                    ...prev,
                    ...t
                };
            });

            const payload = {
                env: {
                    FILEJUMP_API_KEY: document.getElementById('envApiKey').value.trim(),
                    FILEJUMP_BASE_URL: document.getElementById('envBaseUrl').value.trim(),
                    FILEJUMP_UPLOAD_URL: document.getElementById('envUploadUrl').value.trim()
                },
                config: {
                    ...currentConfig,
                    max_workers: Number(document.getElementById('cfgWorkers').value || 4),
                    min_impact_percentage: Number(document.getElementById('cfgThreshold').value || 40),
                    use_high_speed_upload: document.getElementById('cfgHighSpeed').checked,
                    tasks: mergedTasks
                }
            };

            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            closeSettingsModal();
            await loadInitialConfig();
            wasConnected = false;
            refreshStatusAndLogs();
        }


        // =========================================================================
        // GESTIÓN DE PERFILES DE SINCRONIZACIÓN (MODAL DEDICADO)
        // =========================================================================
        let activeProfileRowEl = null;

        const SYNC_DIRECTIONS = {
            local_to_remote: {
                arrow: '➡',
                title: 'Respaldo Local → FileJump (Subir cambios locales a la nube)',
                badgeClass: 'bg-emerald-600/20 border-emerald-500/50 text-emerald-300 hover:bg-emerald-600/35'
            },
            remote_to_local: {
                arrow: '⬅',
                title: 'Respaldo FileJump → Local (Descargar cambios de la nube al equipo)',
                badgeClass: 'bg-sky-600/20 border-sky-500/50 text-sky-300 hover:bg-sky-600/35'
            },
            bidirectional: {
                arrow: '⇄',
                title: 'Sincronización Bidireccional (Local ⇄ FileJump según fecha más reciente)',
                badgeClass: 'bg-amber-600/20 border-amber-500/50 text-amber-300 hover:bg-amber-600/35'
            }
        };

        const SYNC_DIRECTION_ORDER = ['local_to_remote', 'remote_to_local', 'bidirectional'];

        function cycleProfileDirection(btnEl) {
            const currentDir = btnEl.dataset.direction || 'local_to_remote';
            const nextIdx = (SYNC_DIRECTION_ORDER.indexOf(currentDir) + 1) % SYNC_DIRECTION_ORDER.length;
            const nextDir = SYNC_DIRECTION_ORDER[nextIdx];
            const meta = SYNC_DIRECTIONS[nextDir];

            btnEl.dataset.direction = nextDir;
            btnEl.title = meta.title;
            btnEl.innerHTML = `<span class="text-base leading-none font-bold">${meta.arrow}</span>`;
            btnEl.className = `profile-direction lg:col-span-1 h-[34px] rounded-lg border flex items-center justify-center transition-colors ${meta.badgeClass}`;
        }

        function setActiveProfileRow(rowEl) {
            document.querySelectorAll('#syncProfilesListContainer .profile-row').forEach(r => {
                r.classList.remove('ring-1', 'ring-indigo-500', 'border-indigo-500/60');
            });
            activeProfileRowEl = rowEl;
            if (rowEl) {
                rowEl.classList.add('ring-1', 'ring-indigo-500', 'border-indigo-500/60');
                const pName = rowEl.querySelector('.profile-name')?.value.trim() || 'Perfil seleccionado';
                const hint = document.getElementById('selectedProfileTargetHint');
                if (hint) hint.textContent = `Destino de captura: "${pName}" (o haz clic en + Nuevo Perfil).`;
            }
        }

        function updateSyncProfilesCount() {
            const rows = document.querySelectorAll('#syncProfilesListContainer .profile-row');
            const lbl = document.getElementById('syncProfilesCountLabel');
            if (lbl) lbl.textContent = `${rows.length} perfil(es) configurado(s)`;
            if (rows.length === 0) {
                activeProfileRowEl = null;
                const hint = document.getElementById('selectedProfileTargetHint');
                if (hint) hint.textContent = 'Haz clic en los botones de captura para crear un perfil con las carpetas abiertas.';
            }
        }

        function getActiveExplorerLocalSelection() {
            const baseRoot = (typeof localActiveDriveRoot !== 'undefined' && localActiveDriveRoot)
                ? localActiveDriveRoot
                : (document.getElementById('localPathSubtitle')?.textContent || '').trim();
            const relSel = (typeof localSelectedPath !== 'undefined' && localSelectedPath)
                ? localSelectedPath.replace(/^[/\\]+|[/\\]+$/g, '')
                : '';

            if (!baseRoot) return { fullPath: '', folderName: '' };

            const isWindowsDrive = /^[A-Za-z]:[\\/]?$/.test(baseRoot) || baseRoot.includes('\\');
            const sep = isWindowsDrive ? '\\' : '/';
            const cleanBase = baseRoot.replace(/[/\\]+$/, '');
            const normalizedRel = relSel ? relSel.split('/').filter(Boolean).join(sep) : '';

            let fullPath = '';
            if (normalizedRel) {
                fullPath = `${cleanBase}${sep}${normalizedRel}`;
            } else {
                fullPath = /^[A-Za-z]:$/.test(cleanBase) ? `${cleanBase}\\` : (cleanBase || '/');
            }

            const parts = fullPath.replace(/[/\\]+$/, '').split(/[/\\]/).filter(Boolean);
            const folderName = parts.length > 0 ? parts[parts.length - 1].replace(':', '') : 'Raiz_Local';
            return { fullPath, folderName };
        }

        function getActiveExplorerRemoteSelection() {
            if (typeof remoteNodes !== 'undefined' && typeof remoteSelectedKey !== 'undefined') {
                const node = remoteNodes.get(String(remoteSelectedKey));
                if (node) {
                    const chain = [];
                    let cursor = node;
                    while (cursor) {
                        chain.unshift(cursor);
                        cursor = cursor.parent_key ? remoteNodes.get(String(cursor.parent_key)) : null;
                    }
                    const pathDisplay = chain.map((n, idx) => {
                        if (idx === 0 && String(n.id) === 'ROOT') return '';
                        return n.name;
                    }).filter(Boolean).join('/');
                    const remotePath = pathDisplay ? `/${pathDisplay}` : '/ (Raíz de FileJump)';
                    const cleanFolderName = String(node.id) === 'ROOT' ? 'FileJump_Raiz' : node.name;
                    return {
                        remoteId: String(node.id || 'ROOT'),
                        remotePath,
                        folderName: cleanFolderName
                    };
                }
            }
            const selEl = document.getElementById('remoteDirSelector');
            const fallbackId = selEl ? (selEl.value || 'ROOT') : 'ROOT';
            const fallbackText = selEl && selEl.selectedOptions[0] ? selEl.selectedOptions[0].textContent.trim() : '/ (Raíz de FileJump)';
            return {
                remoteId: fallbackId,
                remotePath: fallbackText,
                folderName: fallbackId === 'ROOT' ? 'FileJump_Raiz' : fallbackText
            };
        }

        function addSyncProfileRow(name = '', localPath = '', direction = 'local_to_remote', remotePath = '', remoteId = 'ROOT', threshold = 40) {
            const container = document.getElementById('syncProfilesListContainer');
            if (!container) return null;

            const emptyHint = container.querySelector('.empty-profiles-hint');
            if (emptyHint) emptyHint.remove();

            const dirKey = SYNC_DIRECTIONS[direction] ? direction : 'local_to_remote';
            const dirMeta = SYNC_DIRECTIONS[dirKey];

            const row = document.createElement('div');
            row.className = 'profile-row grid grid-cols-1 lg:grid-cols-12 gap-2 bg-slate-950 p-2.5 rounded-xl border border-slate-800 items-center cursor-pointer transition-all';
            row.onclick = (e) => {
                if (!e.target.closest('button')) {
                    setActiveProfileRow(row);
                }
            };

            row.innerHTML = `
                <input type="text" placeholder="Nombre perfil" value="${escapeAttr(name)}" class="profile-name lg:col-span-2 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none">
                <div class="lg:col-span-4 flex items-center gap-1">
                    <input type="text" placeholder="Ruta local (ej. H:\\Mis Documentos\\VACACIONES)" value="${escapeAttr(localPath)}" class="profile-path w-full bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:border-emerald-500 focus:outline-none">
                    <button type="button" onclick="event.stopPropagation(); setActiveProfileRow(this.closest('.profile-row')); captureCurrentLocalIntoProfile(true);" class="px-2 py-1.5 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/35 border border-emerald-500/40 text-emerald-300 text-xs shrink-0" title="Leer carpeta seleccionada en Explorador Local">📂</button>
                </div>
                <button type="button" data-direction="${dirKey}" onclick="event.stopPropagation(); cycleProfileDirection(this);" title="${escapeAttr(dirMeta.title)}" class="profile-direction lg:col-span-1 h-[34px] rounded-lg border flex items-center justify-center transition-colors ${dirMeta.badgeClass}">
                    <span class="text-base leading-none font-bold">${dirMeta.arrow}</span>
                </button>
                <div class="lg:col-span-3 flex items-center gap-1">
                    <input type="text" placeholder="/ (Raíz de FileJump)" value="${escapeAttr(remotePath || '/ (Raíz de FileJump)')}" data-remote-id="${escapeAttr(remoteId || 'ROOT')}" readonly class="profile-remote-path w-full bg-slate-900/90 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-sky-300 focus:border-sky-500 focus:outline-none cursor-default" title="Usa el botón ☁️ para capturar la carpeta seleccionada en Nube FileJump">
                    <button type="button" onclick="event.stopPropagation(); setActiveProfileRow(this.closest('.profile-row')); captureCurrentRemoteIntoProfile(true);" class="px-2 py-1.5 rounded-lg bg-sky-600/20 hover:bg-sky-600/35 border border-sky-500/40 text-sky-300 text-xs shrink-0" title="Leer carpeta seleccionada en Explorador Remoto">☁️</button>
                </div>
                <input type="number" min="1" max="100" value="${threshold}" title="Umbral máximo de cambios (%) antes de pedir confirmación" class="profile-threshold lg:col-span-1 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-white text-center focus:border-indigo-500 focus:outline-none">
                <button type="button" onclick="event.stopPropagation(); removeSyncProfileRow(this);" class="lg:col-span-1 text-rose-400 hover:text-rose-300 text-sm font-bold text-center py-1" title="Eliminar perfil">✕</button>
            `;
            container.appendChild(row);
            setActiveProfileRow(row);
            updateSyncProfilesCount();
            return row;
        }

        function captureCurrentLocalIntoProfile(forceTargetActive = false) {
            const localSel = getActiveExplorerLocalSelection();
            const remoteSel = getActiveExplorerRemoteSelection();
            let targetRow = activeProfileRowEl && document.body.contains(activeProfileRowEl) ? activeProfileRowEl : null;

            if (!targetRow && !forceTargetActive) {
                const allRows = document.querySelectorAll('#syncProfilesListContainer .profile-row');
                if (allRows.length > 0) {
                    targetRow = allRows[allRows.length - 1];
                }
            }

            if (!targetRow) {
                targetRow = addSyncProfileRow(
                    localSel.folderName || 'Nuevo_Perfil',
                    localSel.fullPath,
                    'local_to_remote',
                    remoteSel.remotePath,
                    remoteSel.remoteId,
                    currentConfig.min_impact_percentage || 40
                );
                return;
            }

            setActiveProfileRow(targetRow);
            const pathInput = targetRow.querySelector('.profile-path');
            const nameInput = targetRow.querySelector('.profile-name');
            if (pathInput && localSel.fullPath) {
                pathInput.value = localSel.fullPath;
            }
            if (nameInput && (!nameInput.value.trim() || nameInput.value.trim() === 'Nuevo_Perfil') && localSel.folderName) {
                nameInput.value = localSel.folderName;
            }
        }

        function captureCurrentRemoteIntoProfile(forceTargetActive = false) {
            const remoteSel = getActiveExplorerRemoteSelection();
            const localSel = getActiveExplorerLocalSelection();
            let targetRow = activeProfileRowEl && document.body.contains(activeProfileRowEl) ? activeProfileRowEl : null;

            if (!targetRow && !forceTargetActive) {
                const allRows = document.querySelectorAll('#syncProfilesListContainer .profile-row');
                if (allRows.length > 0) {
                    targetRow = allRows[allRows.length - 1];
                }
            }

            if (!targetRow) {
                targetRow = addSyncProfileRow(
                    remoteSel.folderName || localSel.folderName || 'Nuevo_Perfil',
                    localSel.fullPath,
                    'local_to_remote',
                    remoteSel.remotePath,
                    remoteSel.remoteId,
                    currentConfig.min_impact_percentage || 40
                );
                return;
            }

            setActiveProfileRow(targetRow);
            const remInput = targetRow.querySelector('.profile-remote-path');
            const nameInput = targetRow.querySelector('.profile-name');
            if (remInput) {
                remInput.value = remoteSel.remotePath;
                remInput.dataset.remoteId = remoteSel.remoteId;
            }
            if (nameInput && !nameInput.value.trim() && remoteSel.folderName) {
                nameInput.value = remoteSel.folderName;
            }
        }

        async function openSyncProfilesModal() {
            const modal = document.getElementById('syncProfilesModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                currentConfig = data.config || { tasks: [] };
            } catch (e) {}

            const container = document.getElementById('syncProfilesListContainer');
            container.innerHTML = '';
            activeProfileRowEl = null;
            const tasks = currentConfig.tasks || [];
            if (tasks.length === 0) {
                container.innerHTML = '<div class="empty-profiles-hint text-center py-6 text-xs text-slate-400 bg-slate-950/50 border border-dashed border-slate-800 rounded-xl">No hay perfiles configurados aún. Haz clic en <span class="text-emerald-400 font-semibold">+ Usar Carpeta Local Actual</span>, <span class="text-sky-400 font-semibold">+ Usar Carpeta Remota Actual</span> o <span class="text-indigo-400 font-semibold">+ Nuevo Perfil</span>.</div>';
            } else {
                tasks.forEach(t => addSyncProfileRow(
                    t.name,
                    t.path,
                    t.sync_direction || 'local_to_remote',
                    t.remote_path || (t.remote_folder_id && t.remote_folder_id !== 'ROOT' ? `Carpeta #${t.remote_folder_id}` : '/ (Raíz de FileJump)'),
                    t.remote_folder_id || 'ROOT',
                    t.min_impact_percentage || 40
                ));
            }
            updateSyncProfilesCount();
        }

        function closeSyncProfilesModal() {
            const modal = document.getElementById('syncProfilesModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }


        async function removeSyncProfileRow(btnEl) {
            const row = btnEl.closest('.profile-row');
            if (!row) return;
            const removedName = row.querySelector('.profile-name') ? row.querySelector('.profile-name').value.trim() : '';
            if (activeProfileRowEl === row) activeProfileRowEl = null;
            row.remove();
            updateSyncProfilesCount();

            // Si el perfil eliminado ya existía guardado en currentConfig.tasks, actualizar inmediatamente local + remoto
            if (removedName && Array.isArray(currentConfig.tasks) && currentConfig.tasks.some(t => t.name === removedName)) {
                const remainingTasks = [];
                document.querySelectorAll('#syncProfilesListContainer .profile-row').forEach(r => {
                    const name = r.querySelector('.profile-name').value.trim();
                    const path = r.querySelector('.profile-path').value.trim();
                    const dirBtn = r.querySelector('.profile-direction');
                    const sync_direction = dirBtn ? (dirBtn.dataset.direction || 'local_to_remote') : 'local_to_remote';
                    const remInput = r.querySelector('.profile-remote-path');
                    const remote_path = remInput ? remInput.value.trim() : '/ (Raíz de FileJump)';
                    const remote_folder_id = remInput ? (remInput.dataset.remoteId || 'ROOT') : 'ROOT';
                    const min_impact_percentage = Number(r.querySelector('.profile-threshold').value || 40);
                    if (name && path) {
                        remainingTasks.push({ name, path, sync_direction, remote_path, remote_folder_id, min_impact_percentage });
                    }
                });

                const updatedConfig = {
                    ...currentConfig,
                    tasks: remainingTasks
                };

                try {
                    const res = await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ config: updatedConfig })
                    });
                    const data = await res.json();
                    if (res.ok && data.success) {
                        currentConfig = updatedConfig;
                        renderTaskSelector();
                    }
                } catch (e) {}
            }
        }

        async function saveSyncProfiles() {
            const tasks = [];
            const seenNames = new Set();
            let duplicateName = null;

            document.querySelectorAll('#syncProfilesListContainer .profile-row').forEach(row => {
                const name = row.querySelector('.profile-name').value.trim();
                const path = row.querySelector('.profile-path').value.trim();
                const dirBtn = row.querySelector('.profile-direction');
                const sync_direction = dirBtn ? (dirBtn.dataset.direction || 'local_to_remote') : 'local_to_remote';
                const remInput = row.querySelector('.profile-remote-path');
                const remote_path = remInput ? remInput.value.trim() : '/ (Raíz de FileJump)';
                const remote_folder_id = remInput ? (remInput.dataset.remoteId || 'ROOT') : 'ROOT';
                const min_impact_percentage = Number(row.querySelector('.profile-threshold').value || 40);

                if (name && path) {
                    if (seenNames.has(name.toLowerCase())) {
                        duplicateName = name;
                    }
                    seenNames.add(name.toLowerCase());
                    tasks.push({
                        name,
                        path,
                        sync_direction,
                        remote_path,
                        remote_folder_id,
                        min_impact_percentage
                    });
                }
            });

            if (duplicateName) {
                showNotice('Nombre Duplicado', `Ya existe un perfil llamado "${duplicateName}". Usa un nombre único para cada perfil.`);
                return;
            }

            const updatedConfig = {
                ...currentConfig,
                max_workers: currentConfig.max_workers || 4,
                min_impact_percentage: currentConfig.min_impact_percentage || 40,
                use_high_speed_upload: currentConfig.use_high_speed_upload !== false,
                tasks
            };

            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: updatedConfig })
                });
                const data = await res.json();
                if (!data.success) {
                    showNotice('Error', data.error || 'No se pudieron guardar los perfiles de sincronización.');
                    return;
                }

                closeSyncProfilesModal(); if (data.remote_message) { showNotice('Perfiles Actualizados', data.remote_message); }
                await loadInitialConfig();

                const sel = document.getElementById('taskSelector');
                if (tasks.length > 0 && (!sel.value || !tasks.some(t => t.name === sel.value))) {
                    sel.value = tasks[0].name;
                }
                if (sel.value) {
                    const savedTask = tasks.find(t => t.name === sel.value);
                    await inspectSelectedTask(savedTask && savedTask.remote_folder_id ? String(savedTask.remote_folder_id) : '');
                }
                refreshStatusAndLogs();
            } catch (e) {
                showNotice('Error de Red', 'No se pudo guardar la configuración de perfiles.');
            }
        }


        // =========================================================================
        // VISTA PREVIA DE COMPARACIÓN DETALLADA (BARRA PROGRESO, FILTROS Y ACCIONES)
        // =========================================================================
        let comparePreviewRows = [];
        let comparePreviewFilter = 'all';
        let comparePreviewMaximized = false;
        let comparePollTimer = null;
        let compareReadOnlyMode = false;

        function toggleCompareModalMaximize() {
            const win = document.getElementById('comparePreviewWindow');
            const btn = document.getElementById('compareMaximizeBtn');
            comparePreviewMaximized = !comparePreviewMaximized;
            if (comparePreviewMaximized) {
                win.classList.remove('max-w-7xl', 'h-[90vh]', 'rounded-2xl');
                win.classList.add('max-w-full', 'w-screen', 'h-screen', 'rounded-none');
                if (btn) btn.textContent = '🗗 Restaurar';
            } else {
                win.classList.add('max-w-7xl', 'h-[90vh]', 'rounded-2xl');
                win.classList.remove('max-w-full', 'w-screen', 'h-screen', 'rounded-none');
                if (btn) btn.textContent = '⛶ Maximizar';
            }
        }

        function closeComparePreviewModal() {
            if (comparePollTimer) {
                clearInterval(comparePollTimer);
                comparePollTimer = null;
            }
            const modal = document.getElementById('comparePreviewModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        function setCompareFilter(filterKey) {
            comparePreviewFilter = filterKey;
            ['all', 'changed', 'deleted'].forEach(k => {
                const btn = document.getElementById(`cmpFilter_${k}`);
                if (!btn) return;
                if (k === filterKey) {
                    btn.className = 'px-3 py-1.5 rounded-xl text-xs font-semibold bg-indigo-600 text-white border border-indigo-500 transition';
                } else {
                    btn.className = 'px-3 py-1.5 rounded-xl text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition';
                }
            });
            renderComparePreviewTable();
        }

        function updateCompareCounters() {
            const allCount = comparePreviewRows.length;
            const changedCount = comparePreviewRows.filter(r => r.category === 'changed').length;
            const deletedCount = comparePreviewRows.filter(r => r.category === 'deleted').length;
            const upCount = comparePreviewRows.filter(r => r.action === '->').length;
            const dnCount = comparePreviewRows.filter(r => r.action === '<-').length;
            const delCount = comparePreviewRows.filter(r => r.action === 'del_remote' || r.action === 'del_local').length;

            const elAll = document.getElementById('cmpCount_all');
            const elChg = document.getElementById('cmpCount_changed');
            const elDel = document.getElementById('cmpCount_deleted');
            const elUp = document.getElementById('cmpSummaryUpload');
            const elDn = document.getElementById('cmpSummaryDownload');
            const elRm = document.getElementById('cmpSummaryDelete');

            if (elAll) elAll.textContent = allCount;
            if (elChg) elChg.textContent = changedCount;
            if (elDel) elDel.textContent = deletedCount;
            if (elUp) elUp.textContent = upCount;
            if (elDn) elDn.textContent = dnCount;
            if (elRm) elRm.textContent = delCount;
        }

        function toggleCompareRowDirection(rowIdx) {
            if (compareReadOnlyMode) return;
            const row = comparePreviewRows[rowIdx];
            if (!row) return;

            let cycle = [];
            if (row.local && row.remote) {
                cycle = ['->', '<-'];
            } else if (row.local && !row.remote) {
                cycle = ['->', 'del_local', '='];
            } else if (!row.local && row.remote) {
                cycle = ['<-', 'del_remote', '='];
            }

            if (cycle.length === 0) return;
            const curIdx = cycle.indexOf(row.action);
            const nextAction = curIdx === -1 ? cycle[0] : cycle[(curIdx + 1) % cycle.length];
            row.action = nextAction;

            const customReasons = {
                '->': 'Manual: Copiar de Local hacia FileJump',
                '<-': 'Manual: Copiar de FileJump hacia Local',
                'del_remote': 'Manual: Eliminar archivo/carpeta en FileJump',
                'del_local': 'Manual: Eliminar archivo/carpeta en Local',
                '=': 'Manual: Omitir / Sin cambios'
            };
            row.reason = customReasons[nextAction] || row.reason;

            updateCompareCounters();
            renderComparePreviewTable();
        }

        function renderComparePreviewTable() {
            const tbody = document.getElementById('comparePreviewTableBody');
            if (!tbody) return;

            const filtered = comparePreviewRows
                .map((row, originalIdx) => ({ row, originalIdx }))
                .filter(({ row }) => {
                    if (comparePreviewFilter === 'changed') return row.category === 'changed';
                    if (comparePreviewFilter === 'deleted') return row.category === 'deleted';
                    return true;
                });

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="py-12 text-center text-slate-500">No hay elementos en esta categoría de filtro.</td></tr>';
                return;
            }

            tbody.innerHTML = filtered.map(({ row, originalIdx }) => {
                const iconType = row.type === 'folder' ? '📁' : '📄';
                const loc = row.local;
                const rem = row.remote;

                const locNameHtml = loc
                    ? `<div class="flex items-center gap-1.5 min-w-0" title="${escapeAttr(row.rel_path)}"><span>${iconType}</span><span class="truncate text-slate-200 font-medium">${escapeAttr(row.rel_path)}</span></div>`
                    : `<span class="text-slate-600 italic">— No existe en local —</span>`;
                const locDateHtml = loc ? `<span class="text-slate-300 font-mono text-[11px]">${escapeAttr(loc.mtime_str)}</span>` : '<span class="text-slate-600">-</span>';
                const locSizeHtml = loc ? (row.type === 'folder' ? '<span class="text-slate-500">Carpeta</span>' : `<span class="text-slate-300">${formatBytes(loc.size)}</span>`) : '<span class="text-slate-600">-</span>';

                const remNameHtml = rem
                    ? `<div class="flex items-center gap-1.5 min-w-0" title="${escapeAttr(row.rel_path)}"><span>${iconType}</span><span class="truncate text-slate-200 font-medium">${escapeAttr(row.rel_path)}</span></div>`
                    : `<span class="text-slate-600 italic">— No existe en nube —</span>`;
                const remDateHtml = rem ? `<span class="text-slate-300 font-mono text-[11px]">${escapeAttr(rem.mtime_str)}</span>` : '<span class="text-slate-600">-</span>';
                const remSizeHtml = rem ? (row.type === 'folder' ? '<span class="text-slate-500">Carpeta</span>' : `<span class="text-slate-300">${formatBytes(rem.size)}</span>`) : '<span class="text-slate-600">-</span>';

                const isIdenticalLocked = (row.category === 'equal' && row.action === '=' && row.local && row.remote);
                const isLocked = isIdenticalLocked || compareReadOnlyMode;
                const disabledAttr = isLocked ? 'disabled' : '';
                const lockCursorClass = compareReadOnlyMode ? 'opacity-75 cursor-not-allowed' : '';
                const lockTooltipSuffix = compareReadOnlyMode ? ' (Bloqueado en sincronización directa)' : ' — Clic para cambiar acción';
                let actionBtnHtml = '';

                if (isIdenticalLocked) {
                    actionBtnHtml = `<span title="${escapeAttr(row.reason)}" class="inline-flex items-center justify-center w-11 h-7 rounded-lg bg-slate-800/90 border border-slate-700 text-slate-400 font-bold select-none">=</span>`;
                } else if (row.action === '->') {
                    actionBtnHtml = `<button type="button" ${disabledAttr} onclick="toggleCompareRowDirection(${originalIdx})" title="${escapeAttr(row.reason + lockTooltipSuffix)}" class="inline-flex items-center justify-center w-11 h-7 rounded-lg bg-emerald-600/25 ${compareReadOnlyMode ? '' : 'hover:bg-emerald-600/40'} border border-emerald-500/50 text-emerald-300 font-bold transition ${lockCursorClass}">-&gt;</button>`;
                } else if (row.action === '<-') {
                    actionBtnHtml = `<button type="button" ${disabledAttr} onclick="toggleCompareRowDirection(${originalIdx})" title="${escapeAttr(row.reason + lockTooltipSuffix)}" class="inline-flex items-center justify-center w-11 h-7 rounded-lg bg-sky-600/25 ${compareReadOnlyMode ? '' : 'hover:bg-sky-600/40'} border border-sky-500/50 text-sky-300 font-bold transition ${lockCursorClass}">&lt;-</button>`;
                } else if (row.action === 'del_remote') {
                    actionBtnHtml = `<button type="button" ${disabledAttr} onclick="toggleCompareRowDirection(${originalIdx})" title="${escapeAttr(row.reason + lockTooltipSuffix)}" class="inline-flex items-center justify-center px-2 h-7 rounded-lg bg-rose-600/25 ${compareReadOnlyMode ? '' : 'hover:bg-rose-600/40'} border border-rose-500/50 text-rose-300 text-[11px] font-bold transition ${lockCursorClass}">🗑️ Nube</button>`;
                } else if (row.action === 'del_local') {
                    actionBtnHtml = `<button type="button" ${disabledAttr} onclick="toggleCompareRowDirection(${originalIdx})" title="${escapeAttr(row.reason + lockTooltipSuffix)}" class="inline-flex items-center justify-center px-2 h-7 rounded-lg bg-rose-600/25 ${compareReadOnlyMode ? '' : 'hover:bg-rose-600/40'} border border-rose-500/50 text-rose-300 text-[11px] font-bold transition ${lockCursorClass}">🗑️ Local</button>`;
                } else {
                    actionBtnHtml = `<button type="button" ${disabledAttr} onclick="toggleCompareRowDirection(${originalIdx})" title="${escapeAttr(row.reason + lockTooltipSuffix)}" class="inline-flex items-center justify-center w-11 h-7 rounded-lg bg-slate-800 ${compareReadOnlyMode ? '' : 'hover:bg-slate-700'} border border-slate-600 text-slate-300 font-bold transition ${lockCursorClass}">=</button>`;
                }

                const rowBg = (row.action === 'del_remote' || row.action === 'del_local' || row.category === 'deleted')
                    ? 'bg-rose-950/15 hover:bg-rose-950/25'
                    : (row.category === 'changed' ? 'bg-amber-950/10 hover:bg-slate-800/60' : 'hover:bg-slate-800/40');

                return `
                    <tr class="${rowBg} transition-colors">
                        <td class="py-2 px-3 max-w-[260px]">${locNameHtml}</td>
                        <td class="py-2 px-2.5 whitespace-nowrap">${locDateHtml}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">${locSizeHtml}</td>
                        <td class="py-2 px-2 text-center">${actionBtnHtml}</td>
                        <td class="py-2 px-3 max-w-[260px]">${remNameHtml}</td>
                        <td class="py-2 px-2.5 whitespace-nowrap">${remDateHtml}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">${remSizeHtml}</td>
                    </tr>
                `;
            }).join('');
        }

        async function openComparePreviewModal(autoExecuteDirect = false) {
            const taskName = document.getElementById('taskSelector').value;
            if (!taskName) {
                showNotice('Selecciona una tarea', 'Elige primero una tarea activa antes de continuar.');
                return;
            }

            compareReadOnlyMode = (autoExecuteDirect === true);
            const modal = document.getElementById('comparePreviewModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            comparePreviewRows = [];
            comparePreviewFilter = 'all';
            setCompareFilter('all');
            updateCompareCounters();

            const execBtn = document.getElementById('executeCompareSyncBtn');
            const footerHint = document.getElementById('compareModalFooterHint');
            if (compareReadOnlyMode) {
                document.getElementById('compareModalTitle').textContent = `Sincronización Directa • ${taskName}`;
                document.getElementById('compareModalSubtitle').textContent = 'Ejecución automática sin espera según las reglas del perfil (controles bloqueados)';
                if (footerHint) {
                    footerHint.innerHTML = '<span class="text-amber-300 font-medium">🔒 Sincronización directa automática:</span> Los controles de dirección están bloqueados y se ejecutará sin pausa al finalizar el análisis.';
                }
                if (execBtn) {
                    execBtn.disabled = true;
                    execBtn.classList.add('opacity-50', 'cursor-not-allowed');
                }
            } else {
                document.getElementById('compareModalTitle').textContent = `Vista Previa de Comparación • ${taskName}`;
                document.getElementById('compareModalSubtitle').textContent = 'Regla activa según dirección del perfil (puedes ajustar cada fila manualmente antes de sincronizar)';
                if (footerHint) {
                    footerHint.innerHTML = 'Haz clic en el botón central (<code class="text-emerald-300">-&gt;</code>, <code class="text-sky-300">&lt;-</code>, <code class="text-rose-300">🗑️</code> o <code class="text-slate-300">=</code>) de cada fila para elegir exactamente qué acción ejecutar.';
                }
                if (execBtn) {
                    execBtn.disabled = false;
                    execBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                }
            }

            document.getElementById('compareProgressSection').classList.remove('hidden');
            document.getElementById('compareProgressSpinner').classList.remove('hidden');
            document.getElementById('compareProgressPhase').textContent = 'Verificando archivos...';
            document.getElementById('compareProgressFile').textContent = 'Iniciando escaneo...';
            document.getElementById('compareProgressPercent').textContent = '0%';
            document.getElementById('compareProgressBar').style.width = '5%';
            document.getElementById('comparePreviewTableBody').innerHTML = '<tr><td colspan="7" class="py-12 text-center text-slate-400">Analizando carpetas locales y en FileJump...</td></tr>';

            try {
                const startRes = await fetch(`/api/compare/start/${encodeURIComponent(taskName)}`, { method: 'POST' });
                const startData = await startRes.json();
                if (!startRes.ok || startData.error) {
                    closeComparePreviewModal();
                    showNotice('Error al Comparar', startData.error || 'No se pudo iniciar la comparación.');
                    return;
                }

                const jobId = startData.job_id;
                if (comparePollTimer) clearInterval(comparePollTimer);

                let isHandlingCompletion = false;
                comparePollTimer = setInterval(async () => {
                    if (isHandlingCompletion) return;
                    try {
                        const stRes = await fetch(`/api/compare/status/${encodeURIComponent(jobId)}`);
                        const job = await stRes.json();
                        if (!stRes.ok || job.error) {
                            clearInterval(comparePollTimer);
                            comparePollTimer = null;
                            document.getElementById('compareProgressPhase').textContent = 'Error en verificación';
                            document.getElementById('compareProgressFile').textContent = job.error || 'Error desconocido';
                            return;
                        }

                        const phaseNames = {
                            local: 'Escaneando Local:',
                            remote: 'Escaneando FileJump:',
                            compare: 'Aplicando reglas del perfil:'
                        };
                        document.getElementById('compareProgressPhase').textContent = phaseNames[job.phase] || 'Verificando:';
                        document.getElementById('compareProgressFile').textContent = `${job.current_file || ''} (${job.checked_count || 0} verificados)`;
                        document.getElementById('compareProgressPercent').textContent = `${job.percent || 0}%`;
                        document.getElementById('compareProgressBar').style.width = `${job.percent || 0}%`;

                        if (job.status === 'completed' && job.result) {
                            isHandlingCompletion = true;
                            clearInterval(comparePollTimer);
                            comparePollTimer = null;
                            comparePreviewRows = job.result.rows || [];
                            updateCompareCounters();
                            renderComparePreviewTable();

                            if (compareReadOnlyMode) {
                                const actionable = comparePreviewRows.filter(r => r.action === '->' || r.action === '<-' || r.action === 'del_remote' || r.action === 'del_local');
                                if (actionable.length === 0) {
                                    document.getElementById('compareProgressSpinner').classList.add('hidden');
                                    document.getElementById('compareProgressPhase').textContent = 'Todo sincronizado:';
                                    document.getElementById('compareProgressFile').textContent = 'No hay diferencias pendientes por sincronizar.';
                                    await inspectSelectedTask();
                                } else {
                                    document.getElementById('compareProgressPhase').textContent = 'Sincronizando automáticamente...';
                                    document.getElementById('compareProgressFile').textContent = `Ejecutando ${actionable.length} acción(es) sin espera...`;
                                    await executeComparePreviewSync(true);
                                }
                            } else {
                                document.getElementById('compareProgressSpinner').classList.add('hidden');
                                document.getElementById('compareProgressPhase').textContent = 'Comparación lista:';
                            }
                        }
                    } catch (err) {}
                }, 300);
            } catch (e) {
                closeComparePreviewModal();
                showNotice('Error de Red', 'No se pudo iniciar la comparación.');
            }
        }

        async function executeComparePreviewSync(fromAutoDirect = false) {
            const taskName = document.getElementById('taskSelector').value;
            if (!taskName) return;

            const actionable = comparePreviewRows.filter(r => r.action === '->' || r.action === '<-' || r.action === 'del_remote' || r.action === 'del_local');
            if (actionable.length === 0) {
                if (!fromAutoDirect) {
                    showNotice('Sin acciones pendientes', 'Todos los archivos están marcados como "=" (sin cambios).');
                }
                return;
            }

            const btn = document.getElementById('executeCompareSyncBtn');
            const mainBtn = document.getElementById('syncBtn');
            if (btn) {
                btn.disabled = true;
                btn.classList.add('opacity-50', 'cursor-not-allowed');
            }
            if (mainBtn) {
                mainBtn.disabled = true;
                mainBtn.classList.add('opacity-50', 'cursor-not-allowed');
            }

            document.getElementById('compareProgressSpinner').classList.remove('hidden');
            document.getElementById('compareProgressPhase').textContent = 'Ejecutando sincronización...';
            document.getElementById('compareProgressFile').textContent = `Procesando ${actionable.length} operación(es)...`;

            try {
                const res = await fetch('/api/compare/execute', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        task_name: taskName,
                        items: actionable
                    })
                });
                const data = await res.json();
                if (data.success) {
                    const prevLocalRel = localSelectedPath;
                    const prevRemoteKey = remoteSelectedKey;
                    closeComparePreviewModal();
                    await inspectSelectedTask();
                    if (prevLocalRel) {
                        await selectLocalFolder(prevLocalRel);
                    }
                    if (prevRemoteKey && prevRemoteKey !== remoteBaseRootId && remoteNodes.has(String(prevRemoteKey))) {
                        await selectRemoteNode(prevRemoteKey);
                    }
                    fetchUserStorageInfo();
                    showNotice(
                        'Sincronización Completada',
                        `Acciones ejecutadas: ${data.uploaded || 0} subida(s) (->), ${data.downloaded || 0} descarga(s) (<-) y ${data.deleted || 0} eliminación(es).`
                    );
                } else {
                    document.getElementById('compareProgressSpinner').classList.add('hidden');
                    showNotice('Error de Sincronización', data.error || 'No se pudo completar la sincronización.');
                }
            } catch (e) {
                document.getElementById('compareProgressSpinner').classList.add('hidden');
                showNotice('Error de Red', 'Error de comunicación con el servidor local.');
            } finally {
                if (btn && !compareReadOnlyMode) {
                    btn.disabled = false;
                    btn.classList.remove('opacity-50', 'cursor-not-allowed');
                }
                if (mainBtn) {
                    mainBtn.disabled = false;
                    mainBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                }
            }
        }

        // Inicialización
        refreshStatusAndLogs();
        syncCachedRemoteIds();
        loadRemoteMusicFavorites();
        fetchLocalDrives(true);
        loadInitialConfig();
        setInterval(refreshStatusAndLogs, 4000);
