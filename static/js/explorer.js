// =========================================================================
        // EXPLORADORES DUALES (ÁRBOL JERÁRQUICO + CONTENIDO + MENÚ CONTEXTUAL)
        // =========================================================================
        function isAudioFilename(name) {
            const ext = (name || '').split('.').pop().toLowerCase();
            return AUDIO_EXTS.includes(ext);
        }

        function getCurrentFolderFiles(source) {
            if (source === 'local') {
                const node = localNodes.get(localSelectedPath || '');
                return (node && Array.isArray(node.children)) ? node.children.filter(i => i.type === 'file') : [];
            } else {
                const node = remoteNodes.get(String(remoteSelectedKey || 'ROOT'));
                return (node && Array.isArray(node.children)) ? node.children.filter(i => i.type === 'file') : [];
            }
        }

        function escapeAttr(str) {
            return String(str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function initLocalExplorer(rootItems) {
            localNodes.clear();
            localSelectedPath = '';
            localNodes.set('', {
                rel_path: '',
                name: 'Raíz Local',
                loaded: true,
                expanded: true,
                children: rootItems || []
            });
            renderLocalTreePanel();
            renderLocalContentsPanel();
        }

        function renderLocalTreePanel() {
            const treeEl = document.getElementById('localFolderTree');
            if (!treeEl) return;
            const rootNode = localNodes.get('');
            if (!rootNode) {
                treeEl.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildNodeHtml(relPath, depth) {
                const node = localNodes.get(relPath);
                if (!node) return '';
                const isSelected = localSelectedPath === relPath;
                const subfolders = (node.children || []).filter(c => c.type === 'folder');
                const padLeft = depth * 12 + 4;
                const canExpand = relPath === '' || node.has_children || subfolders.length > 0 || !node.loaded;
                const toggleSymbol = !canExpand ? '·' : (node.expanded ? '−' : '+');

                let html = `
                    <div oncontextmenu="openExplorerContextMenu(event, 'local', { type: 'folder', rel_path: '${escapeAttr(relPath)}', name: '${escapeAttr(node.name)}', full_path: '${escapeAttr(node.full_path || '')}' })"
                         class="flex items-center gap-1 py-1 pr-2 rounded-lg cursor-pointer transition ${isSelected ? 'bg-sky-600/25 text-white font-semibold border border-sky-500/30' : 'text-slate-300 hover:bg-slate-800/70'}"
                         style="padding-left: ${padLeft}px;">
                        <button type="button" onclick="event.stopPropagation(); toggleLocalTreeNode('${escapeAttr(relPath)}')"
                                class="w-4 h-4 rounded bg-slate-800/90 hover:bg-slate-700 text-[10px] font-mono text-slate-300 flex items-center justify-center shrink-0">${toggleSymbol}</button>
                        <div onclick="selectLocalFolder('${escapeAttr(relPath)}')" class="flex items-center gap-1.5 min-w-0 flex-1 truncate">
                            <span class="shrink-0">${relPath === '' ? '🏠' : '📁'}</span>
                            <span class="truncate">${escapeAttr(node.name)}</span>
                        </div>
                    </div>`;

                if (node.expanded && subfolders.length > 0) {
                    subfolders.forEach(sf => {
                        if (!localNodes.has(sf.rel_path)) {
                            localNodes.set(sf.rel_path, {
                                rel_path: sf.rel_path,
                                name: sf.name,
                                full_path: sf.full_path,
                                has_children: sf.has_children,
                                loaded: false,
                                expanded: false,
                                children: []
                            });
                        }
                        html += buildNodeHtml(sf.rel_path, depth + 1);
                    });
                }
                return html;
            }

            treeEl.innerHTML = buildNodeHtml('', 0);
        }

        async function fetchLocalNodeChildren(relPath) {
            const node = localNodes.get(relPath);
            if (!node) return;
            const taskName = document.getElementById('taskSelector').value || '';
            const query = localActiveDriveRoot
                ? `root_path=${encodeURIComponent(localActiveDriveRoot)}&prefix=${encodeURIComponent(relPath)}`
                : `task=${encodeURIComponent(taskName)}&prefix=${encodeURIComponent(relPath)}`;
            const res = await fetch(`/api/local/children?${query}`);
            const data = await res.json();
            node.children = data.items || [];
            node.loaded = true;
            node.children.filter(c => c.type === 'folder').forEach(sf => {
                if (!localNodes.has(sf.rel_path)) {
                    localNodes.set(sf.rel_path, {
                        rel_path: sf.rel_path,
                        name: sf.name,
                        full_path: sf.full_path,
                        has_children: sf.has_children,
                        loaded: false,
                        expanded: false,
                        children: []
                    });
                }
            });
        }

        async function toggleLocalTreeNode(relPath) {
            const node = localNodes.get(relPath);
            if (!node) return;
            if (!node.loaded) {
                await fetchLocalNodeChildren(relPath);
            }
            node.expanded = !node.expanded;
            renderLocalTreePanel();
        }

        async function selectLocalFolder(relPath) {
            localSelectedPath = relPath || '';
            // Expandir ancestros en el árbol
            if (localSelectedPath) {
                const parts = localSelectedPath.split('/');
                let acc = '';
                for (let i = 0; i < parts.length; i++) {
                    acc = acc ? `${acc}/${parts[i]}` : parts[i];
                    const pNode = localNodes.get(acc);
                    if (pNode) pNode.expanded = true;
                }
            }
            const rootNode = localNodes.get('');
            if (rootNode) rootNode.expanded = true;

            const targetNode = localNodes.get(localSelectedPath);
            if (targetNode) {
                if (!targetNode.loaded) {
                    await fetchLocalNodeChildren(localSelectedPath);
                }
                targetNode.expanded = true;
            }
            renderLocalTreePanel();
            renderLocalContentsPanel();
        }

        function renderLocalContentsPanel() {
            const tbody = document.getElementById('localTableBody');
            const badge = document.getElementById('localCountBadge');
            const bcBar = document.getElementById('localBreadcrumbBar');
            const node = localNodes.get(localSelectedPath || '');

            // Breadcrumbs
            let bcHtml = `<button onclick="selectLocalFolder('')" class="hover:text-sky-400 font-medium flex items-center gap-1 shrink-0"><span>🏠 Raíz</span></button>`;
            if (localSelectedPath) {
                const parts = localSelectedPath.split('/');
                let acc = '';
                parts.forEach((part, idx) => {
                    acc = acc ? `${acc}/${part}` : part;
                    const isLast = idx === parts.length - 1;
                    bcHtml += `<span class="text-slate-600">/</span><button onclick="selectLocalFolder('${escapeAttr(acc)}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-sky-400'} truncate max-w-[160px]">${escapeAttr(part)}</button>`;
                });
            }
            bcBar.innerHTML = bcHtml;

            if (!node) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Sin contenido disponible</td></tr>';
                badge.textContent = '0 elementos';
                return;
            }

            const items = sortExplorerItems(node.children || [], 'local');
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Carpeta vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const icon = isFolder ? '📁' : (isAudioFilename(item.name) ? '🎵' : '📄');
                const metaJson = escapeAttr(JSON.stringify(item));
                const dblClick = isFolder
                    ? `selectLocalFolder('${escapeAttr(item.rel_path)}')`
                    : `openExplorerFile('local', ${metaJson})`;

                return `
                    <tr ondblclick="${dblClick}"
                        oncontextmenu="openExplorerContextMenu(event, 'local', ${metaJson})"
                        class="hover:bg-slate-800/65 cursor-pointer transition">
                        <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[240px] truncate" title="${escapeAttr(item.name)}">
                            <span class="mr-1.5">${icon}</span><span>${escapeAttr(item.name)}</span>
                        </td>
                        <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                        <td class="py-2 px-2.5 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeAttr(item.mtime_str || '-')}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">${getStatusBadge(item.sync_status)}</td>
                    </tr>`;
            }).join('');
        }

        function initRemoteExplorer(rootItems, rootId = 'ROOT', rootLabel = '/ (Raíz de FileJump)') {
            remoteNodes.clear();
            remoteBaseRootId = String(rootId || 'ROOT');
            remoteSelectedKey = remoteBaseRootId;
            remoteNodes.set(remoteBaseRootId, {
                key: remoteBaseRootId,
                id: remoteBaseRootId,
                rel_path: '',
                name: rootLabel,
                loaded: true,
                expanded: true,
                children: rootItems || []
            });
            renderRemoteTreePanel();
            renderRemoteContentsPanel();
        }

        function renderRemoteTreePanel() {
            const treeEl = document.getElementById('remoteFolderTree');
            if (!treeEl) return;
            const rootNode = remoteNodes.get(remoteBaseRootId);
            if (!rootNode) {
                treeEl.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildNodeHtml(nodeKey, depth) {
                const node = remoteNodes.get(String(nodeKey));
                if (!node) return '';
                const isSelected = String(remoteSelectedKey) === String(nodeKey);
                const subfolders = (node.children || []).filter(c => c.type === 'folder');
                const padLeft = depth * 12 + 4;
                const toggleSymbol = node.expanded ? '−' : '+';

                let html = `
                    <div oncontextmenu="openExplorerContextMenu(event, 'remote', { type: 'folder', id: '${escapeAttr(node.id)}', rel_path: '${escapeAttr(node.rel_path || '')}', name: '${escapeAttr(node.name)}' })"
                         class="flex items-center gap-1 py-1 pr-2 rounded-lg cursor-pointer transition ${isSelected ? 'bg-indigo-600/25 text-white font-semibold border border-indigo-500/30' : 'text-slate-300 hover:bg-slate-800/70'}"
                         style="padding-left: ${padLeft}px;">
                        <button type="button" onclick="event.stopPropagation(); toggleRemoteTreeNode('${escapeAttr(nodeKey)}')"
                                class="w-4 h-4 rounded bg-slate-800/90 hover:bg-slate-700 text-[10px] font-mono text-slate-300 flex items-center justify-center shrink-0">${toggleSymbol}</button>
                        <div onclick="selectRemoteNode('${escapeAttr(nodeKey)}')" class="flex items-center gap-1.5 min-w-0 flex-1 truncate">
                            <span class="shrink-0">${String(nodeKey) === String(remoteBaseRootId) ? '☁️' : '📁'}</span>
                            <span class="truncate">${escapeAttr(node.name)}</span>
                        </div>
                    </div>`;

                if (node.expanded && subfolders.length > 0) {
                    subfolders.forEach(sf => {
                        const childKey = String(sf.id);
                        if (!remoteNodes.has(childKey)) {
                            remoteNodes.set(childKey, {
                                key: childKey,
                                id: sf.id,
                                parent_key: String(nodeKey),
                                rel_path: sf.rel_path,
                                name: sf.name,
                                loaded: false,
                                expanded: false,
                                children: []
                            });
                        }
                        html += buildNodeHtml(childKey, depth + 1);
                    });
                }
                return html;
            }

            treeEl.innerHTML = buildNodeHtml(remoteBaseRootId, 0);
        }

        async function fetchRemoteNodeChildren(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            const taskName = document.getElementById('taskSelector').value || '';
            const res = await fetch(`/api/remote/children?parent_id=${encodeURIComponent(node.id)}&prefix=${encodeURIComponent(node.rel_path || '')}&task=${encodeURIComponent(taskName)}`);
            const data = await res.json();
            node.children = data.items || [];
            node.loaded = true;
            node.children.filter(c => c.type === 'folder').forEach(sf => {
                const cKey = String(sf.id);
                if (!remoteNodes.has(cKey)) {
                    remoteNodes.set(cKey, {
                        key: cKey,
                        id: sf.id,
                        parent_key: String(nodeKey),
                        rel_path: sf.rel_path,
                        name: sf.name,
                        loaded: false,
                        expanded: false,
                        children: []
                    });
                }
            });
        }

        async function toggleRemoteTreeNode(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            if (!node.loaded) {
                await fetchRemoteNodeChildren(nodeKey);
            }
            node.expanded = !node.expanded;
            renderRemoteTreePanel();
        }

        async function selectRemoteNode(nodeKey) {
            remoteSelectedKey = String(nodeKey || remoteBaseRootId);
            const node = remoteNodes.get(remoteSelectedKey);
            if (!node) return;

            // Expandir cadena de padres
            let cursor = node;
            while (cursor) {
                cursor.expanded = true;
                cursor = cursor.parent_key ? remoteNodes.get(String(cursor.parent_key)) : null;
            }

            if (!node.loaded) {
                await fetchRemoteNodeChildren(remoteSelectedKey);
            }
            node.expanded = true;
            renderRemoteTreePanel();
            renderRemoteContentsPanel();
        }

        function renderRemoteContentsPanel() {
            const tbody = document.getElementById('remoteTableBody');
            const badge = document.getElementById('remoteCountBadge');
            const bcBar = document.getElementById('remoteBreadcrumbBar');
            const subLabel = document.getElementById('remotePathSubtitle');
            const dirSelector = document.getElementById('remoteDirSelector');
            const node = remoteNodes.get(String(remoteSelectedKey));

            // Construir cadena de ancestros para breadcrumbs y subtítulo activo
            const chain = [];
            let cursor = node;
            while (cursor) {
                chain.unshift(cursor);
                cursor = cursor.parent_key ? remoteNodes.get(String(cursor.parent_key)) : null;
            }

            if (node) {
                const pathDisplay = chain.map(n => n.name).join(' / ');
                subLabel.textContent = `ID Remoto: ${node.id} • ${pathDisplay}`;

                // Actualizar selector superior activamente con la carpeta actual y sus subcarpetas
                if (dirSelector) {
                    const subOpts = (node.children || []).filter(c => c.type === 'folder')
                        .map(sf => `<option value="${escapeAttr(sf.id)}">↳ 📁 ${escapeAttr(sf.name)} (#${escapeAttr(sf.id)})</option>`)
                        .join('');
                    const currentLabel = String(node.id) === 'ROOT' ? '/ (Raíz de FileJump)' : `📂 ${node.name} (#${node.id})`;
                    dirSelector.innerHTML = `
                        <option value="${escapeAttr(node.id)}" selected>${escapeAttr(currentLabel)}</option>
                        ${String(node.id) !== 'ROOT' ? '<option value="ROOT">/ (Raíz de FileJump)</option>' : ''}
                        <option value="">Auto (Carpeta de Tarea)</option>
                        ${subOpts}
                    `;
                }
            }

            bcBar.innerHTML = chain.map((n, idx) => {
                const isLast = idx === chain.length - 1;
                const sep = idx > 0 ? '<span class="text-slate-600">/</span>' : '';
                return `${sep}<button onclick="selectRemoteNode('${escapeAttr(n.key)}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-indigo-400'} flex items-center gap-1 shrink-0 max-w-[180px] truncate">${idx === 0 ? '☁️ ' : ''}${escapeAttr(n.name)}</button>`;
            }).join('');

            if (!node) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Sin contenido remoto disponible</td></tr>';
                badge.textContent = '0 elementos';
                return;
            }

            const items = sortExplorerItems(node.children || [], 'remote');
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Carpeta remota vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const icon = isFolder ? '📁' : (isAudioFilename(item.name) ? '🎵' : '☁️');
                const metaJson = escapeAttr(JSON.stringify(item));
                const dblClick = isFolder
                    ? `selectRemoteNode('${escapeAttr(item.id)}')`
                    : `openExplorerFile('remote', ${metaJson})`;

                return `
                    <tr ondblclick="${dblClick}"
                        oncontextmenu="openExplorerContextMenu(event, 'remote', ${metaJson})"
                        class="hover:bg-slate-800/65 cursor-pointer transition">
                        <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[240px] truncate" title="${escapeAttr(item.name)}">
                            <span class="mr-1.5">${icon}</span><span>${escapeAttr(item.name)}</span>
                        </td>
                        <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                        <td class="py-2 px-2.5 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeAttr(item.mtime_str || '-')}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">
                            <span class="text-[10px] font-mono text-slate-500 mr-1.5">#${escapeAttr(item.id)}</span>
                            ${getStatusBadge(item.sync_status)}
                        </td>
                    </tr>`;
            }).join('');
        }

        function openExplorerFile(source, fileMeta) {
            currentPreviewSource = source;
            const folderFiles = getCurrentFolderFiles(source);
            const nonAudioFiles = folderFiles.filter(f => !isAudioFilename(f.name));
            if (isAudioFilename(fileMeta.name)) {
                openMusicDockPlayer(source, fileMeta, false);
                return;
            }
            currentPreviewList = nonAudioFiles.length > 0 ? nonAudioFiles : [fileMeta];
            currentPreviewIndex = currentPreviewList.findIndex(f =>
                source === 'local'
                    ? (f.full_path && fileMeta.full_path ? f.full_path === fileMeta.full_path : f.rel_path === fileMeta.rel_path)
                    : String(f.id) === String(fileMeta.id)
            );
            if (currentPreviewIndex < 0) currentPreviewIndex = 0;
            renderPreviewItem(currentPreviewList[currentPreviewIndex]);
        }

        function navigatePreview(delta) {
            if (currentPreviewList.length <= 1) return;
            currentPreviewIndex = (currentPreviewIndex + delta + currentPreviewList.length) % currentPreviewList.length;
            renderPreviewItem(currentPreviewList[currentPreviewIndex]);
        }

        async function renderPreviewItem(fileMeta) {
            if (!fileMeta) return;
            const modal = document.getElementById('previewModal');
            const titleEl = document.getElementById('previewTitle');
            const badgeEl = document.getElementById('previewBadge');
            const counterEl = document.getElementById('previewCounter');
            const openTabBtn = document.getElementById('previewOpenTabBtn');
            const prevBtn = document.getElementById('previewPrevBtn');
            const nextBtn = document.getElementById('previewNextBtn');
            const body = document.getElementById('previewBody');

            const name = fileMeta.name || 'Archivo';
            const ext = name.split('.').pop().toLowerCase();
            const qParams = buildAudioQueryParams(currentPreviewSource, fileMeta);
            const streamUrl = `/api/file/stream?${qParams}`;

            titleEl.textContent = name;
            badgeEl.textContent = currentPreviewSource === 'remote' ? '☁️ Nube' : '💻 Local';
            counterEl.textContent = currentPreviewList.length > 1 ? `(${currentPreviewIndex + 1} / ${currentPreviewList.length})` : '';
            openTabBtn.href = streamUrl;
            prevBtn.disabled = currentPreviewList.length <= 1;
            nextBtn.disabled = currentPreviewList.length <= 1;

            modal.classList.remove('hidden');
            modal.classList.add('flex');

            const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
            const videoExts = ['mp4', 'webm', 'ogg', 'mov', 'avi', 'mkv', 'wmv', 'flv'];

            if (ext === 'pdf') {
                body.innerHTML = `<iframe src="${streamUrl}" class="w-full h-[72vh] rounded-xl border border-slate-800 bg-white"></iframe>`;
                return;
            }

            if (imageExts.includes(ext)) {
                body.innerHTML = `<img src="${streamUrl}" alt="${escapeAttr(name)}" class="max-h-[72vh] max-w-full object-contain rounded-xl shadow-lg">`;
                return;
            }

            if (videoExts.includes(ext)) {
                const fileMetaEncoded = escapeAttr(JSON.stringify(fileMeta));
                body.innerHTML = `
                    <div class="w-full flex flex-col items-center gap-3">
                        <video id="activePreviewVideo" src="${streamUrl}" controls autoplay playsinline class="max-h-[68vh] w-full rounded-xl bg-black"></video>
                        <div id="videoAudioControlsBar" class="w-full flex flex-wrap items-center justify-between gap-2 bg-slate-900/90 border border-slate-800 rounded-xl px-3 py-2 text-xs">
                            <div class="flex items-center gap-2">
                                <span class="text-slate-400">Pista de audio:</span>
                                <select id="videoAudioTrackSelect" onchange="onSelectVideoAudioTrack('${escapeAttr(streamUrl)}', '${escapeAttr(name)}', this.value)" class="bg-slate-950 border border-slate-700 rounded-lg px-2 py-1 text-slate-200">
                                    <option value="0">Pista 1 (Predeterminada)</option>
                                </select>
                            </div>
                            <div class="flex items-center gap-2">
                                <button type="button" onclick="openFileInNativeProgram('${escapeAttr(currentPreviewSource)}', ${fileMetaEncoded})" class="px-3 py-1 rounded-lg bg-emerald-600/25 hover:bg-emerald-600/40 border border-emerald-500/40 text-emerald-300 transition flex items-center gap-1.5" title="Abrir este video directamente en el reproductor local de tu equipo">
                                    <span>🖥️</span><span>Abrir con programa local</span>
                                </button>
                                <button type="button" onclick="forceVideoAudioTranscode('${escapeAttr(streamUrl)}', '${escapeAttr(name)}', document.getElementById('videoAudioTrackSelect').value)" class="px-3 py-1 rounded-lg bg-indigo-600/25 hover:bg-indigo-600/40 border border-indigo-500/40 text-indigo-300 transition">
                                    🔊 Convertir Audio a AAC
                                </button>
                            </div>
                        </div>
                    </div>`;
                fetch(`/api/video/tracks?${qParams}`)
                    .then(r => r.json())
                    .then(d => {
                        const sel = document.getElementById('videoAudioTrackSelect');
                        if (sel && Array.isArray(d.tracks) && d.tracks.length > 0) {
                            sel.innerHTML = d.tracks.map(t => `<option value="${t.audio_index}">${escapeAttr(t.label)}</option>`).join('');
                        }
                    })
                    .catch(() => {});
                return;
            }

            body.innerHTML = '<div class="text-xs text-slate-400 animate-pulse">Generando vista previa...</div>';
            try {
                const res = await fetch(`/api/file/preview?${qParams}`);
                const data = await res.json();
                if (data.kind === 'text') {
                    body.innerHTML = `<pre class="w-full h-[68vh] overflow-auto bg-slate-950 border border-slate-800 rounded-xl p-4 text-xs font-mono text-slate-200 whitespace-pre-wrap break-words">${escapeAttr(data.content || '')}</pre>`;
                } else if (data.kind === 'office') {
                    body.innerHTML = `<div class="w-full h-[68vh] overflow-auto bg-slate-950 border border-slate-800 rounded-xl p-5 text-slate-200">${data.html}</div>`;
                } else {
                    body.innerHTML = `
                        <div class="text-center space-y-3 py-8">
                            <div class="text-4xl">📄</div>
                            <div class="text-sm font-semibold text-white">${escapeAttr(name)}</div>
                            <p class="text-xs text-slate-400">Vista previa directa no disponible para archivos .${escapeAttr(ext)}.</p>
                            <a href="${streamUrl}" target="_blank" rel="noopener" class="inline-block px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Descargar / Abrir Archivo</a>
                        </div>`;
                }
            } catch (e) {
                body.innerHTML = '<div class="text-xs text-rose-400">No se pudo cargar la vista previa del archivo.</div>';
            }
        }

        function closePreviewModal() {
            const modal = document.getElementById('previewModal');
            const body = document.getElementById('previewBody');
            if (body) body.innerHTML = '';
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        function prefetchNextRemoteTrack() {
            if (musicSource !== 'remote' || musicPlayOrder.length <= 1) return;
            const nextPos = (musicOrderPos + 1) % musicPlayOrder.length;
            const nextTrack = musicPlaylist[musicPlayOrder[nextPos]];
            if (!nextTrack || !nextTrack.id || prefetchedRemoteIds.has(String(nextTrack.id))) return;

            const badge = document.getElementById('dockPrefetchBadge');
            const mtimeParam = nextTrack.mtime ? `&mtime=${encodeURIComponent(nextTrack.mtime)}` : '';
            fetch(`/api/file/prefetch?source=remote&remote_id=${encodeURIComponent(nextTrack.id)}&name=${encodeURIComponent(nextTrack.name || 'audio')}${mtimeParam}`)
                .then(r => r.json())
                .then(res => {
                    if (res && res.prefetched) {
                        prefetchedRemoteIds.add(String(nextTrack.id));
                        if (badge) badge.classList.remove('hidden');
                        renderMusicPlaylist();
                    }
                })
                .catch(() => {});
        }

        function forceVideoAudioTranscode(streamUrl, fileName, audioTrackIdx = 0) {
            const body = document.getElementById('previewBody');
            if (!body) return;
            const sep = streamUrl.includes('?') ? '&' : '?';
            const targetUrl = `${streamUrl}${sep}transcode=1&audio_track=${encodeURIComponent(audioTrackIdx)}`;
            const existingControls = document.getElementById('videoAudioControlsBar');
            const controlsHtml = existingControls ? existingControls.outerHTML : '';

            body.innerHTML = `
                <div class="w-full flex flex-col items-center gap-3">
                    <div class="text-xs text-sky-300 animate-pulse">Extrayendo pista de audio #${Number(audioTrackIdx) + 1} y convirtiendo a AAC estéreo...</div>
                    <video id="activePreviewVideo" src="${targetUrl}" controls autoplay playsinline class="max-h-[68vh] w-full rounded-xl bg-black"></video>
                    ${controlsHtml}
                </div>`;
            const vid = document.getElementById('activePreviewVideo');
            if (vid) {
                vid.muted = false;
                vid.volume = 0.85;
            }
            const sel = document.getElementById('videoAudioTrackSelect');
            if (sel) sel.value = String(audioTrackIdx);
        }

        function onSelectVideoAudioTrack(streamUrl, fileName, trackIdx) {
            forceVideoAudioTranscode(streamUrl, fileName, Number(trackIdx || 0));
        }

        function initSvgVisualizerDom() {
            const svg = document.getElementById('dockSvgVisualizer');
            if (!svg || svgVisInitialized) return;
            const ns = 'http://www.w3.org/2000/svg';
            svg.innerHTML = '';

            const defs = document.createElementNS(ns, 'defs');
            const grad = document.createElementNS(ns, 'linearGradient');
            grad.setAttribute('id', 'svgVisGrad');
            grad.setAttribute('x1', '0%');
            grad.setAttribute('y1', '100%');
            grad.setAttribute('x2', '100%');
            grad.setAttribute('y2', '0%');
            svgVisGradStops = [
                { offset: '0%', color: 'hsl(235, 85%, 58%)' },
                { offset: '55%', color: 'hsl(195, 92%, 55%)' },
                { offset: '100%', color: 'hsl(155, 85%, 52%)' }
            ].map(st => {
                const stop = document.createElementNS(ns, 'stop');
                stop.setAttribute('offset', st.offset);
                stop.setAttribute('stop-color', st.color);
                grad.appendChild(stop);
                return stop;
            });
            defs.appendChild(grad);
            svg.appendChild(defs);

            svgVisBars = [];
            const slotW = 320 / SVG_BARS_COUNT;
            const barW = Math.max(3, slotW - 2.2);
            for (let i = 0; i < SVG_BARS_COUNT; i++) {
                const rect = document.createElementNS(ns, 'rect');
                rect.setAttribute('x', (i * slotW + 1.1).toFixed(1));
                rect.setAttribute('y', '41');
                rect.setAttribute('width', barW.toFixed(1));
                rect.setAttribute('height', '3');
                rect.setAttribute('rx', '1.8');
                rect.setAttribute('fill', 'url(#svgVisGrad)');
                svg.appendChild(rect);
                svgVisBars.push(rect);
            }

            svgVisPolyline = document.createElementNS(ns, 'polyline');
            svgVisPolyline.setAttribute('fill', 'none');
            svgVisPolyline.setAttribute('stroke', 'url(#svgVisGrad)');
            svgVisPolyline.setAttribute('stroke-width', '2.4');
            svgVisPolyline.setAttribute('stroke-linejoin', 'round');
            svgVisPolyline.setAttribute('stroke-linecap', 'round');
            svgVisPolyline.setAttribute('points', '0,22 320,22');
            svgVisPolyline.style.display = 'none';
            svg.appendChild(svgVisPolyline);

            svgVisInitialized = true;
        }

        function syncSvgVisualizerMode() {
            initSvgVisualizerDom();
            const isWave = visualizerStyle === 'wave';
            svgVisBars.forEach(b => { b.style.display = isWave ? 'none' : ''; });
            if (svgVisPolyline) svgVisPolyline.style.display = isWave ? '' : 'none';
        }

        function startVisualizerLoop() {
            if (visualizerRafId) cancelAnimationFrame(visualizerRafId);
            syncSvgVisualizerMode();
            const audioEl = document.getElementById('dockAudioElement');
            let freqData = null;
            let timeData = null;

            function draw(now) {
                if (!visualizerEnabled || document.hidden || musicDockMode === 'minimized' || (audioEl && audioEl.paused)) {
                    visualizerRafId = null;
                    return;
                }
                visualizerRafId = requestAnimationFrame(draw);

                if (now - lastVisualizerFrameTime < VISUALIZER_FPS_INTERVAL) return;
                lastVisualizerFrameTime = now;

                if (!analyserNode) return;
                const bufferLength = analyserNode.frequencyBinCount;

                if (!freqData || freqData.length !== bufferLength) {
                    freqData = new Uint8Array(bufferLength);
                }
                analyserNode.getByteFrequencyData(freqData);

                // Detección instantánea de energía de graves (kick/ritmo) y brillo agudo para cambiar colores al ritmo
                const bassSum = (freqData[1] + freqData[2] + freqData[3] + freqData[4]) / (4 * 255);
                const midSum = (freqData[6] + freqData[8] + freqData[10] + freqData[12]) / (4 * 255);
                const highSum = (freqData[15] + freqData[18] + freqData[21] + freqData[24]) / (4 * 255);
                const beatPulse = Math.max(0, bassSum - beatEnergySmooth);
                beatEnergySmooth = beatEnergySmooth * 0.72 + bassSum * 0.28;

                // Desplazamiento dinámico de matiz (HSL): tonos fríos en calma -> fucsia/naranja/dorado en golpes de ritmo
                const targetHue = (225 + bassSum * 135 + beatPulse * 180 + highSum * 60) % 360;
                beatHueSmooth = beatHueSmooth + (targetHue - beatHueSmooth) * 0.35;
                const h1 = Math.round(beatHueSmooth);
                const h2 = (h1 + Math.round(45 + midSum * 55)) % 360;
                const h3 = (h2 + Math.round(50 + highSum * 65)) % 360;
                const lumBoost = Math.round(54 + Math.min(18, (bassSum + beatPulse) * 24));

                if (svgVisGradStops.length === 3) {
                    svgVisGradStops[0].setAttribute('stop-color', `hsl(${h1}, 90%, ${lumBoost}%)`);
                    svgVisGradStops[1].setAttribute('stop-color', `hsl(${h2}, 94%, ${lumBoost + 4}%)`);
                    svgVisGradStops[2].setAttribute('stop-color', `hsl(${h3}, 90%, ${lumBoost + 2}%)`);
                }

                if (visualizerStyle === 'wave') {
                    if (!timeData || timeData.length !== bufferLength) {
                        timeData = new Uint8Array(bufferLength);
                    }
                    analyserNode.getByteTimeDomainData(timeData);
                    if (svgVisPolyline) {
                        const pts = [];
                        const usableBins = Math.min(bufferLength, 48);
                        const stepX = 320 / Math.max(1, usableBins - 1);
                        for (let i = 0; i < usableBins; i++) {
                            const centered = (timeData[i] - 128) / 128;
                            const y = 22 + centered * (20 + beatPulse * 10);
                            pts.push(`${(i * stepX).toFixed(1)},${Math.max(2, Math.min(42, y)).toFixed(1)}`);
                        }
                        svgVisPolyline.setAttribute('points', pts.join(' '));
                    }
                } else {
                    const isMirror = visualizerStyle === 'mirror';
                    // Distribución logarítmica sobre las frecuencias audibles para mayor precisión musical
                    const maxUsefulBin = Math.min(bufferLength - 1, 44);
                    for (let i = 0; i < SVG_BARS_COUNT; i++) {
                        const normPos = i / (SVG_BARS_COUNT - 1);
                        const binIdx = Math.min(maxUsefulBin, Math.floor(Math.pow(normPos, 1.35) * maxUsefulBin));
                        const rawVal = freqData[binIdx] / 255;
                        // Compensación de agudos + impulso en el golpe de ritmo
                        const gainBoost = 1 + normPos * 0.42 + beatPulse * 0.35;
                        const val = Math.min(1, Math.pow(rawVal * gainBoost, 1.15));
                        const h = Math.max(3, Math.round(val * 41));
                        const y = isMirror ? Math.round((44 - h) / 2) : (44 - h);
                        const rect = svgVisBars[i];
                        if (rect) {
                            rect.setAttribute('y', String(y));
                            rect.setAttribute('height', String(h));
                        }
                    }
                }
            }
            visualizerRafId = requestAnimationFrame(draw);
        }

        document.addEventListener('visibilitychange', () => {
            const audioEl = document.getElementById('dockAudioElement');
            if (!document.hidden && visualizerEnabled && audioEl && !audioEl.paused && musicDockMode !== 'minimized') {
                startVisualizerLoop();
            }
        });

        async function openFileInNativeProgram(source, itemMeta) {
            if (!itemMeta || itemMeta.type === 'folder') return;
            const taskName = document.getElementById('taskSelector').value || '';
            const vidEl = document.getElementById('activePreviewVideo');
            if (vidEl && !vidEl.paused) {
                vidEl.pause();
            }
            try {
                const res = await fetch('/api/explorer/open_native', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source,
                        id: itemMeta.id,
                        name: itemMeta.name,
                        size: itemMeta.size || 0,
                        mtime: itemMeta.mtime || 0,
                        full_path: itemMeta.full_path || '',
                        root_path: localActiveDriveRoot || '',
                        task: taskName,
                        rel_path: itemMeta.rel_path || ''
                    })
                });
                const data = await res.json();
                if (data.success) {
                    if (source === 'remote' && data.job_id) {
                        startTransferTracking(
                            data.job_id,
                            `Preparando en caché: ${itemMeta.name}`,
                            'download',
                            () => {
                                if (itemMeta.id) prefetchedRemoteIds.add(String(itemMeta.id));
                            }
                        );
                    }
                    refreshStatusAndLogs();
                } else {
                    showNotice('Error al abrir archivo', data.error || 'No se pudo abrir con la aplicación del sistema.');
                }
            } catch (e) {
                showNotice('Error de Red', 'No se pudo comunicar con el servidor para abrir el archivo localmente.');
            }
        }

        function openExplorerContextMenu(event, source, itemMeta) {
            event.preventDefault();
            event.stopPropagation();
            activeContextTarget = { source, item: itemMeta };

            const menu = document.getElementById('explorerContextMenu');
            const isFolder = itemMeta.type === 'folder';
            const isAudio = !isFolder && isAudioFilename(itemMeta.name);
            const canQueueAudio = isFolder || isAudio;
            const canOpenNative = !isFolder && !isAudio;
            const itemsHtml = [];

            itemsHtml.push(`<button onclick="ctxActionOpen()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 flex items-center gap-2.5"><span>📂</span><span>Abrir</span></button>`);

            if (canOpenNative) {
                itemsHtml.push(`<button onclick="ctxActionOpenNative()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-amber-300 flex items-center gap-2.5"><span>🖥️</span><span>Abrir con programa local</span></button>`);
            }

            if (canQueueAudio) {
                itemsHtml.push(`<button onclick="ctxActionAddToPlaylist()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-indigo-300 flex items-center gap-2.5"><span>🎵</span><span>Agregar a la lista de reproducción</span></button>`);
            }

            if (source === 'remote' && String(itemMeta.id) !== 'ROOT') {
                itemsHtml.push(`<button onclick="ctxActionDownloadToLocal()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-sky-300 flex items-center gap-2.5"><span>⬇️</span><span>Descargar a carpeta local abierta</span></button>`);
            }

            if (source === 'local' && (itemMeta.rel_path !== '' || itemMeta.full_path)) {
                itemsHtml.push(`<button onclick="ctxActionUploadToRemote()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-emerald-300 flex items-center gap-2.5"><span>⬆️</span><span>Cargar a carpeta remota abierta</span></button>`);
            }

            if ((source === 'local' && (itemMeta.rel_path !== '' || itemMeta.full_path)) || (source === 'remote' && String(itemMeta.id) !== 'ROOT')) {
                itemsHtml.push(`<div class="my-1 border-t border-slate-800"></div>`);
                itemsHtml.push(`<button onclick="ctxActionRename()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 flex items-center gap-2.5"><span>✏️</span><span>Cambiar de nombre</span></button>`);
                itemsHtml.push(`<button onclick="ctxActionDelete()" class="w-full px-3.5 py-2 text-left hover:bg-rose-950/60 text-rose-400 flex items-center gap-2.5"><span>🗑️</span><span>Eliminar</span></button>`);
            }

            itemsHtml.push(`<div class="my-1 border-t border-slate-800"></div>`);
            itemsHtml.push(`<button onclick="ctxActionProperties()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-slate-300 flex items-center gap-2.5"><span>ℹ️</span><span>Propiedades</span></button>`);

            menu.innerHTML = itemsHtml.join('');
            menu.classList.remove('hidden');

            const menuWidth = 240;
            const menuHeight = 250;
            const posX = Math.min(event.clientX, window.innerWidth - menuWidth - 8);
            const posY = Math.min(event.clientY, window.innerHeight - menuHeight - 8);
            menu.style.left = `${Math.max(8, posX)}px`;
            menu.style.top = `${Math.max(8, posY)}px`;
        }

        function closeExplorerContextMenu() {
            const menu = document.getElementById('explorerContextMenu');
            if (menu) menu.classList.add('hidden');
        }

        document.addEventListener('click', closeExplorerContextMenu);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeExplorerContextMenu();
        });

        function ctxActionOpen() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();
            if (source === 'local') {
                if (item.type === 'folder') {
                    selectLocalFolder(item.rel_path || '');
                } else {
                    openExplorerFile('local', item);
                }
            } else {
                if (item.type === 'folder') {
                    selectRemoteNode(String(item.id));
                } else {
                    openExplorerFile('remote', item);
                }
            }
        }

        function ctxActionOpenNative() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();
            openFileInNativeProgram(source, item);
        }

        function cycleVisualizerStyle() {
            const order = ['bars', 'mirror', 'wave'];
            const labels = { bars: 'SVG Barras', mirror: 'SVG Espejo', wave: 'SVG Onda' };
            const nextIdx = (order.indexOf(visualizerStyle) + 1) % order.length;
            visualizerStyle = order[nextIdx];
            const btn = document.getElementById('dockVisStyleBtn');
            if (btn) btn.textContent = labels[visualizerStyle];
            syncSvgVisualizerMode();
            const audioEl = document.getElementById('dockAudioElement');
            if (visualizerEnabled && audioEl && !audioEl.paused) {
                startVisualizerLoop();
            }
        }

        function closeAudioScanDock() {
            audioScanDockClosedByUser = true;
            const dock = document.getElementById('audioScanDockWidget');
            if (dock) dock.classList.add('hidden');
        }

        function renderAudioScanProgressUI(job) {
            const dock = document.getElementById('audioScanDockWidget');
            if (!dock || audioScanDockClosedByUser) return;

            dock.classList.remove('hidden');
            document.getElementById('audioScanTitle').textContent = job.title || 'Escaneando música...';
            document.getElementById('audioScanCurrentFolder').textContent = job.error ? job.error : (job.current_folder || '');
            document.getElementById('audioScanCount').textContent = String(job.found_count || 0);
            document.getElementById('audioScanFoldersLabel').textContent = `${job.scanned_folders || 0} carpeta(s) revisada(s)`;

            const dot = document.getElementById('audioScanDot');
            const bar = document.getElementById('audioScanBar');
            const statusLbl = document.getElementById('audioScanStatusLabel');

            if (job.status === 'completed') {
                dot.className = 'w-2 h-2 rounded-full bg-emerald-400 shrink-0';
                bar.style.width = '100%';
                bar.className = 'h-full bg-emerald-500 rounded-full transition-all duration-200';
                statusLbl.textContent = '¡Listo! ✔';
                statusLbl.className = 'text-emerald-400 font-semibold';
            } else if (job.status === 'error') {
                dot.className = 'w-2 h-2 rounded-full bg-rose-500 shrink-0';
                bar.style.width = '100%';
                bar.className = 'h-full bg-rose-500 rounded-full transition-all duration-200';
                statusLbl.textContent = 'Error ✖';
                statusLbl.className = 'text-rose-400 font-semibold';
            } else {
                dot.className = 'w-2 h-2 rounded-full bg-indigo-400 animate-pulse shrink-0';
                const pseudoPct = Math.min(92, 18 + ((job.scanned_folders || 1) * 7) % 74);
                bar.style.width = `${pseudoPct}%`;
                bar.className = 'h-full bg-gradient-to-r from-indigo-500 via-sky-400 to-emerald-400 rounded-full transition-all duration-200';
                statusLbl.textContent = 'Escaneando subcarpetas...';
                statusLbl.className = 'text-indigo-300';
            }
        }

        async function ctxActionAddToPlaylist() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const payload = {
                source,
                type: item.type,
                id: item.id,
                name: item.name || 'Selección',
                full_path: item.full_path || '',
                root_path: localActiveDriveRoot || '',
                task: taskName,
                rel_path: item.rel_path || ''
            };

            try {
                const res = await fetch('/api/explorer/audio_scan_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const startData = await res.json();
                if (!startData.job_id) {
                    showNotice('Error', startData.error || 'No se pudo iniciar el escaneo de audio.');
                    return;
                }

                if (activeAudioScanPollTimer) {
                    clearInterval(activeAudioScanPollTimer);
                    activeAudioScanPollTimer = null;
                }

                audioScanDockClosedByUser = false;
                renderAudioScanProgressUI({
                    title: startData.title || `Escaneando: ${item.name}`,
                    status: 'running',
                    current_folder: item.name || 'Iniciando...',
                    found_count: 0,
                    scanned_folders: 0
                });

                activeAudioScanPollTimer = setInterval(async () => {
                    try {
                        const stRes = await fetch(`/api/explorer/audio_scan_status/${encodeURIComponent(startData.job_id)}`);
                        if (!stRes.ok) return;
                        const job = await stRes.json();
                        renderAudioScanProgressUI(job);

                        if (job.status === 'completed' || job.status === 'error') {
                            clearInterval(activeAudioScanPollTimer);
                            activeAudioScanPollTimer = null;
                            refreshStatusAndLogs();

                            if (job.status === 'error') {
                                showNotice('Error de Escaneo', job.error || 'Ocurrió un error al buscar pistas de audio.');
                                return;
                            }

                            const tracks = job.tracks || [];
                            if (tracks.length === 0) {
                                showNotice('Sin archivos de audio', 'No se encontraron pistas de audio compatibles en la selección.');
                                setTimeout(() => {
                                    const d = document.getElementById('audioScanDockWidget');
                                    if (d) d.classList.add('hidden');
                                }, 1800);
                                return;
                            }

                            const dockWidget = document.getElementById('musicDockWidget');
                            const isDockHidden = !dockWidget || dockWidget.classList.contains('hidden');
                            const wasEmpty = isDockHidden || musicPlaylist.length === 0 || musicSource !== source;
                            if (wasEmpty) {
                                musicSource = source;
                                musicPlaylist = tracks;
                                rebuildMusicPlayOrder(0);
                                setMusicDockMode(musicDockMode === 'minimized' ? 'normal' : (musicDockMode || 'normal'));
                                await playDockTrackAtOrderPos(0);
                            } else {
                                tracks.forEach(t => {
                                    musicPlaylist.push(t);
                                    musicPlayOrder.push(musicPlaylist.length - 1);
                                });
                                document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
                                renderMusicPlaylist();
                                prefetchNextRemoteTrack();
                            }

                            setTimeout(() => {
                                const d = document.getElementById('audioScanDockWidget');
                                if (d) d.classList.add('hidden');
                            }, 2400);
                        }
                    } catch (e) {}
                }, 220);
            } catch (e) {
                showNotice('Error', 'No se pudieron recopilar los archivos multimedia de audio.');
            }
        }

        function setTransferDockMode(mode) {
            transferDockMode = mode;
            const widget = document.getElementById('transferDockWidget');
            const minEl = document.getElementById('transferDockMinimized');
            const expEl = document.getElementById('transferDockExpanded');
            const itemsList = document.getElementById('transferItemsList');
            const maxBtn = document.getElementById('transferMaxRestoreBtn');

            widget.classList.remove('hidden');

            if (mode === 'minimized') {
                widget.className = 'fixed bottom-4 left-4 z-50';
                minEl.classList.remove('hidden');
                expEl.classList.add('hidden');
                return;
            }

            minEl.classList.add('hidden');
            expEl.classList.remove('hidden');

            if (mode === 'maximized') {
                widget.className = 'fixed inset-0 w-screen h-screen p-3 sm:p-6 z-50 flex items-center justify-center bg-slate-950/90';
                expEl.className = 'w-full h-full max-w-3xl max-h-[85vh] bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                itemsList.className = 'flex-1 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (maxBtn) { maxBtn.textContent = '🗗'; maxBtn.title = 'Restaurar tamaño'; }
            } else {
                widget.className = 'fixed bottom-4 left-4 z-50';
                expEl.className = 'w-80 sm:w-[440px] bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                itemsList.className = 'max-h-36 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (maxBtn) { maxBtn.textContent = '⛶'; maxBtn.title = 'Maximizar detalle'; }
            }
        }

        function toggleTransferDockMaximize() {
            setTransferDockMode(transferDockMode === 'maximized' ? 'normal' : 'maximized');
        }

        function closeTransferDock() {
            if (activeTransferPollTimer) {
                clearInterval(activeTransferPollTimer);
                activeTransferPollTimer = null;
            }
            document.getElementById('transferDockWidget').classList.add('hidden');
        }

        function formatMB(bytes) {
            const num = Number(bytes || 0);
            return `${(num / (1024 * 1024)).toFixed(2)} MB`;
        }

        function renderTransferJobUI(job) {
            const isUpload = job.direction === 'upload';
            const pct = Math.max(0, Math.min(100, Number(job.percent || 0)));
            const speedStr = job.speed_text || '0.00 MB/s';

            // Minimizado
            document.getElementById('transferMiniIcon').textContent = isUpload ? '⬆️' : '⬇️';
            document.getElementById('transferMiniTitle').textContent = job.title || 'Transfiriendo...';
            document.getElementById('transferMiniSpeed').textContent = job.status === 'running' ? speedStr : '';
            document.getElementById('transferMiniPct').textContent = `${pct}%`;
            document.getElementById('transferMiniBar').style.width = `${pct}%`;

            // Expandido / Maximizado
            document.getElementById('transferHeaderBadge').textContent = isUpload ? '⬆️ Carga a FileJump' : '⬇️ Descarga a Equipo Local';
            document.getElementById('transferTitleText').textContent = job.title || 'Transferencia';
            document.getElementById('transferCurrentFileText').textContent = job.error ? job.error : (job.current_file || '');
            document.getElementById('transferSpeedBadge').textContent = speedStr;
            document.getElementById('transferPctText').textContent = `${pct}%`;
            document.getElementById('transferCountText').textContent = `${job.processed_files || 0} / ${job.total_files || 0} archivo(s)`;
            document.getElementById('transferMainBar').style.width = `${pct}%`;
            document.getElementById('transferBytesText').textContent = `${formatMB(job.bytes_done || 0)} / ${formatMB(job.bytes_total || 0)}`;

            const dot = document.getElementById('transferStatusDot');
            const stateLabel = document.getElementById('transferStateLabel');
            const mainBar = document.getElementById('transferMainBar');

            if (job.status === 'completed') {
                dot.className = 'w-2 h-2 rounded-full bg-emerald-400 shrink-0';
                stateLabel.textContent = 'Completado ✔';
                stateLabel.className = 'text-emerald-400 font-semibold';
                mainBar.className = 'h-full bg-emerald-500 rounded-full transition-all duration-150';
            } else if (job.status === 'error') {
                dot.className = 'w-2 h-2 rounded-full bg-rose-500 shrink-0';
                stateLabel.textContent = 'Error ✖';
                stateLabel.className = 'text-rose-400 font-semibold';
                mainBar.className = 'h-full bg-rose-500 rounded-full transition-all duration-150';
            } else {
                dot.className = 'w-2 h-2 rounded-full bg-sky-400 animate-pulse shrink-0';
                stateLabel.textContent = job.file_percent > 0 ? `Archivo actual: ${job.file_percent}%` : 'Transfiriendo...';
                stateLabel.className = 'text-sky-300';
                mainBar.className = 'h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-emerald-400 rounded-full transition-all duration-150';
            }

            const items = job.items || [];
            document.getElementById('transferDetailCountTag').textContent = `${items.length} archivo(s)`;
            const listEl = document.getElementById('transferItemsList');
            if (items.length === 0) {
                listEl.innerHTML = '<div class="px-3.5 py-4 text-center text-slate-500">Esperando lista de archivos...</div>';
            } else {
                listEl.innerHTML = items.map(it => {
                    let stIcon = '⏳';
                    let rowCls = 'text-slate-400';
                    let progressInfo = formatMB(it.size);
                    if (it.status === 'active') {
                        stIcon = '🔄';
                        rowCls = 'bg-sky-600/15 text-white font-semibold';
                        const fp = Number(it.file_pct || 0);
                        progressInfo = `${formatMB(it.sent || 0)} / ${formatMB(it.size)} (${fp}%)`;
                    } else if (it.status === 'done') {
                        stIcon = '✅';
                        rowCls = 'text-emerald-300';
                        progressInfo = `${formatMB(it.size)} (100%)`;
                    } else if (it.status === 'error') {
                        stIcon = '❌';
                        rowCls = 'text-rose-400';
                    }
                    return `
                        <div class="px-3.5 py-1.5 flex items-center justify-between gap-2 ${rowCls}">
                            <div class="flex items-center gap-2 min-w-0">
                                <span class="shrink-0 text-[11px]">${stIcon}</span>
                                <span class="truncate" title="${escapeAttr(it.name)}">${escapeAttr(it.name)}</span>
                            </div>
                            <span class="text-[10px] font-mono text-slate-400 shrink-0">${progressInfo}</span>
                        </div>`;
                }).join('');
            }
        }

        function startTransferTracking(jobId, initialTitle, direction, onCompleted = null) {
            if (activeTransferPollTimer) {
                clearInterval(activeTransferPollTimer);
                activeTransferPollTimer = null;
            }

            renderTransferJobUI({
                direction,
                title: initialTitle,
                status: 'running',
                current_file: 'Iniciando transferencia...',
                processed_files: 0,
                total_files: 0,
                bytes_done: 0,
                bytes_total: 0,
                percent: 2,
                items: []
            });
            setTransferDockMode(transferDockMode === 'minimized' ? 'normal' : (transferDockMode || 'normal'));

            activeTransferPollTimer = setInterval(async () => {
                try {
                    const res = await fetch(`/api/transfer/status/${encodeURIComponent(jobId)}`);
                    if (!res.ok) return;
                    const job = await res.json();
                    renderTransferJobUI(job);
                    refreshStatusAndLogs();

                    if (job.status === 'completed' || job.status === 'error') {
                        clearInterval(activeTransferPollTimer);
                        activeTransferPollTimer = null;
                        if (typeof onCompleted === 'function') {
                            onCompleted(job);
                        } else if (job.direction === 'download') {
                            await fetchLocalNodeChildren(localSelectedPath || '');
                            renderLocalTreePanel();
                            renderLocalContentsPanel();
                        } else {
                            await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                            renderRemoteTreePanel();
                            renderRemoteContentsPanel();
                            fetchUserStorageInfo();
                        }
                    }
                } catch (e) {}
            }, 220);
        }

        async function ctxActionDownloadToLocal() {
            if (!activeContextTarget) return;
            const { item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const currentLocalNode = localNodes.get(localSelectedPath || '');
            const payload = {
                direction: 'download',
                type: item.type,
                id: item.id,
                name: item.name,
                size: item.size || 0,
                mtime: item.mtime || 0,
                dest_full_path: (currentLocalNode && currentLocalNode.full_path) || '',
                dest_root_path: localActiveDriveRoot || '',
                dest_task: taskName,
                dest_rel_path: localSelectedPath || ''
            };

            try {
                const res = await fetch('/api/explorer/transfer_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.job_id) {
                    startTransferTracking(data.job_id, `Descargando: ${item.name}`, 'download');
                } else {
                    showNotice('Error de Descarga', data.error || 'No se pudo iniciar la descarga.');
                }
            } catch (e) {
                showNotice('Error de Descarga', 'Fallo de comunicación al iniciar la descarga desde FileJump.');
            }
        }

        async function ctxActionUploadToRemote() {
            if (!activeContextTarget) return;
            const { item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const payload = {
                direction: 'upload',
                type: item.type,
                name: item.name,
                size: item.size || 0,
                full_path: item.full_path || '',
                root_path: localActiveDriveRoot || '',
                task: taskName,
                rel_path: item.rel_path || '',
                dest_remote_id: remoteSelectedKey || 'ROOT'
            };

            try {
                const res = await fetch('/api/explorer/transfer_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.job_id) {
                    startTransferTracking(data.job_id, `Subiendo: ${item.name}`, 'upload');
                } else {
                    showNotice('Error de Carga', data.error || `Fallo al iniciar la subida de '${item.name}'`);
                }
            } catch (e) {
                showNotice('Error de Carga', `Fallo de comunicación al subir '${item.name}'`);
            }
        }

        function ctxActionRename() {
            if (!activeContextTarget) return;
            renamePendingTarget = activeContextTarget;
            closeExplorerContextMenu();
            const modal = document.getElementById('renameItemModal');
            const input = document.getElementById('renameNewNameInput');
            input.value = renamePendingTarget.item.name || '';
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            setTimeout(() => { input.focus(); input.select(); }, 50);
        }

        function closeRenameModal() {
            document.getElementById('renameItemModal').classList.add('hidden');
            document.getElementById('renameItemModal').classList.remove('flex');
        }

        async function submitRenameModal() {
            if (!renamePendingTarget) return;
            const newName = document.getElementById('renameNewNameInput').value.trim();
            if (!newName) return;
            const { source, item } = renamePendingTarget;
            closeRenameModal();

            const taskName = document.getElementById('taskSelector').value || '';
            const res = await fetch('/api/explorer/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source,
                    type: item.type,
                    id: item.id,
                    full_path: item.full_path || '',
                    root_path: localActiveDriveRoot || '',
                    task: taskName,
                    rel_path: item.rel_path || '',
                    new_name: newName
                })
            });
            const data = await res.json();
            if (data.success) {
                if (source === 'local') {
                    await fetchLocalNodeChildren(localSelectedPath || '');
                    renderLocalTreePanel();
                    renderLocalContentsPanel();
                } else {
                    await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                    renderRemoteTreePanel();
                    renderRemoteContentsPanel();
                }
            } else {
                showNotice('Error al renombrar', data.error || 'No se pudo cambiar el nombre.');
            }
        }

        function ctxActionDelete() {
            if (!activeContextTarget) return;
            const target = activeContextTarget;
            closeExplorerContextMenu();
            showNotice(
                'Confirmar Eliminación',
                `¿Deseas eliminar definitivamente '${target.item.name}'? Esta acción no se puede deshacer.`,
                async () => {
                    const taskName = document.getElementById('taskSelector').value || '';
                    const res = await fetch('/api/explorer/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            source: target.source,
                            type: target.item.type,
                            id: target.item.id,
                            full_path: target.item.full_path || '',
                            root_path: localActiveDriveRoot || '',
                            task: taskName,
                            rel_path: target.item.rel_path || ''
                        })
                    });
                    const data = await res.json();
                    if (data.success) {
                        if (target.source === 'local') {
                            await fetchLocalNodeChildren(localSelectedPath || '');
                            renderLocalTreePanel();
                            renderLocalContentsPanel();
                        } else {
                            await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                            renderRemoteTreePanel();
                            renderRemoteContentsPanel();
                            fetchUserStorageInfo();
                        }
                    } else {
                        showNotice('Error al eliminar', data.error || 'No se pudo eliminar el elemento.');
                    }
                }
            );
        }

        async function ctxActionProperties() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();

            const modal = document.getElementById('propertiesItemModal');
            const body = document.getElementById('propertiesModalBody');
            body.innerHTML = '<div class="text-slate-400 animate-pulse">Calculando propiedades...</div>';
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            const taskName = document.getElementById('taskSelector').value || '';
            try {
                const res = await fetch('/api/explorer/properties', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source,
                        type: item.type,
                        id: item.id,
                        name: item.name,
                        size: item.size || 0,
                        mtime: item.mtime || 0,
                        full_path: item.full_path || '',
                        root_path: localActiveDriveRoot || '',
                        task: taskName,
                        rel_path: item.rel_path || ''
                    })
                });
                const p = await res.json();
                body.innerHTML = `
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Nombre:</span><span class="text-white font-semibold text-right break-all">${escapeAttr(p.name)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Tipo:</span><span class="text-slate-200">${escapeAttr(p.type)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Ubicación:</span><span class="text-slate-300 font-mono text-[11px] text-right break-all">${escapeAttr(p.location)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Tamaño total:</span><span class="text-sky-300 font-mono">${formatBytes(p.size)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Contenido:</span><span class="text-slate-200">${p.file_count} archivo(s), ${p.folder_count} carpeta(s)</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Modificado:</span><span class="text-slate-200 font-mono">${escapeAttr(p.modified)}</span></div>
                `;
            } catch (e) {
                body.innerHTML = '<div class="text-rose-400">No se pudieron obtener las propiedades.</div>';
            }
        }

        function closePropertiesModal() {
            document.getElementById('propertiesItemModal').classList.add('hidden');
            document.getElementById('propertiesItemModal').classList.remove('flex');
        }

        function shutdownApplication() {
            showNotice(
                'Cerrar FileJump Sync Studio',
                'Se cerrará esta ventana del navegador, el servidor Python y la consola asociada. ¿Deseas salir ahora?',
                async () => {
                    try {
                        await fetch('/api/shutdown', { method: 'POST' });
                    } catch (e) {}

                    // Intentar cerrar la pestaña/ventana del navegador
                    try { window.open('', '_self'); } catch (e) {}
                    try { window.close(); } catch (e) {}

                    // Pantalla de respaldo si el navegador bloquea window.close() por seguridad
                    setTimeout(() => {
                        document.body.innerHTML = `
                            <div class="min-h-screen bg-slate-950 text-slate-200 flex flex-col items-center justify-center p-6 text-center space-y-3">
                                <div class="w-12 h-12 rounded-2xl bg-rose-600/20 border border-rose-500/40 text-rose-400 flex items-center justify-center text-2xl">⏻</div>
                                <h1 class="text-lg font-bold text-white">Servidor y consola cerrados correctamente</h1>
                                <p class="text-xs text-slate-400 max-w-md">El proceso de Python y la ventana de comandos han finalizado. Ya puedes cerrar esta pestaña.</p>
                            </div>`;
                    }, 300);
                }
            );
        }
