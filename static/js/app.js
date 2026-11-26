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
                const q = remoteSelectorVal ? `?remote_id=${encodeURIComponent(remoteSelectorVal)}` : '';
                const res = await fetch(`/api/inspect/${encodeURIComponent(taskName)}${q}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Aviso', data.error);
                    return;
                }

                document.getElementById('lastSyncLabel').textContent = data.last_sync
                    ? `Última sync: ${new Date(data.last_sync).toLocaleString()}`
                    : 'Sin sincronizaciones previas';

                if (!localActiveDriveRoot) {
                    document.getElementById('localPathSubtitle').textContent = data.task.path;
                    initLocalExplorer(data.local_items || []);
                }

                const rId = data.remote_root_id || remoteSelectorVal || 'ROOT';
                const rLabel = String(rId) === 'ROOT' ? '/ (Raíz de FileJump)' : `${data.task.name} (#${rId})`;
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
                    await inspectSelectedTask();
                    fetchUserStorageInfo();
                    showNotice('Sincronización Completa', `Se subieron ${data.uploaded} de ${data.total} archivo(s) pendientes.`);
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

            const payload = {
                env: {
                    FILEJUMP_API_KEY: document.getElementById('envApiKey').value.trim(),
                    FILEJUMP_BASE_URL: document.getElementById('envBaseUrl').value.trim(),
                    FILEJUMP_UPLOAD_URL: document.getElementById('envUploadUrl').value.trim()
                },
                config: {
                    max_workers: Number(document.getElementById('cfgWorkers').value || 4),
                    min_impact_percentage: Number(document.getElementById('cfgThreshold').value || 40),
                    use_high_speed_upload: document.getElementById('cfgHighSpeed').checked,
                    tasks
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

        // Inicialización
        refreshStatusAndLogs();
        syncCachedRemoteIds();
        loadRemoteMusicFavorites();
        fetchLocalDrives(true);
        loadInitialConfig();
        setInterval(refreshStatusAndLogs, 4000);
