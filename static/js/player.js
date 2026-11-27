// =========================================================================
        // REPRODUCTOR MUSICAL FLOTANTE (ID3 + PLAYLIST + EQ 5 BANDAS + VISUALIZADOR)
        // =========================================================================
        function rebuildMusicPlayOrder(startIndex = 0) {
            const total = musicPlaylist.length;
            if (total === 0) {
                musicPlayOrder = [];
                musicOrderPos = -1;
                return;
            }
            const indices = Array.from({ length: total }, (_, i) => i);
            if (musicShuffle && total > 1) {
                const others = indices.filter(i => i !== startIndex);
                for (let i = others.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [others[i], others[j]] = [others[j], others[i]];
                }
                musicPlayOrder = [startIndex, ...others];
            } else {
                musicPlayOrder = indices;
            }
            musicOrderPos = Math.max(0, musicPlayOrder.indexOf(startIndex));
        }

        function setMusicDockMode(mode) {
            musicDockMode = mode;
            const widget = document.getElementById('musicDockWidget');
            const minEl = document.getElementById('musicDockMinimized');
            const expEl = document.getElementById('musicDockExpanded');
            const mainGrid = document.getElementById('musicDockMainGrid');
            const playerCol = document.getElementById('musicDockPlayerCol');
            const playlistCol = document.getElementById('musicDockPlaylistCol');
            const playlistItems = document.getElementById('dockPlaylistItems');
            const searchWrap = document.getElementById('dockPlaylistSearchWrap');
            const coverBox = document.getElementById('dockCoverBox');
            const visWrap = document.getElementById('dockVisualizerWrap');
            const maxBtn = document.getElementById('dockMaxRestoreBtn');

            widget.classList.remove('hidden');

            if (mode === 'minimized') {
                widget.className = 'fixed bottom-4 right-4 z-50';
                minEl.classList.remove('hidden');
                expEl.classList.add('hidden');
                return;
            }

            minEl.classList.add('hidden');
            expEl.classList.remove('hidden');

            if (mode === 'maximized') {
                // Sin backdrop-blur para evitar que Chromium recomponga toda la pantalla en cada frame (GPU < 3%)
                widget.className = 'fixed inset-0 w-screen h-screen p-3 sm:p-6 z-50 flex items-center justify-center bg-slate-950/90';
                expEl.className = 'w-full h-full max-w-5xl max-h-[90vh] bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'grid grid-cols-1 md:grid-cols-12 flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'md:col-span-6 p-5 sm:p-6 space-y-4 flex flex-col justify-between overflow-y-auto';
                playlistCol.className = 'md:col-span-6 border-t md:border-t-0 md:border-l border-slate-800 bg-slate-950 flex flex-col min-h-0';
                playlistItems.className = 'flex-1 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (searchWrap) searchWrap.classList.remove('hidden');
                coverBox.className = 'w-28 h-28 sm:w-36 sm:h-36 rounded-2xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-xl cursor-zoom-in';
                if (visWrap) {
                    visWrap.className = `bg-slate-950 border border-slate-800 rounded-xl p-2 relative overflow-hidden h-14 flex items-center${visualizerEnabled ? '' : ' hidden'}`;
                }
                if (maxBtn) { maxBtn.textContent = '🗗'; maxBtn.title = 'Restaurar tamaño'; }
                const savePlBtn = document.getElementById('dockSavePlaylistBtn');
                if (savePlBtn) savePlBtn.classList.remove('hidden');
                const savedPlBtn = document.getElementById('dockSavedPlaylistsBtn');
                if (savedPlBtn) savedPlBtn.classList.remove('hidden');
            } else {
                widget.className = 'fixed bottom-4 right-4 z-50';
                expEl.className = 'w-80 sm:w-[410px] bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'flex flex-col flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'p-4 space-y-3.5 flex flex-col justify-between shrink-0';
                playlistCol.className = 'border-t border-slate-800 bg-slate-950 flex flex-col min-h-0';
                playlistItems.className = 'max-h-44 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (searchWrap) searchWrap.classList.add('hidden');
                coverBox.className = 'w-16 h-16 rounded-xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-inner cursor-zoom-in';
                if (visWrap) {
                    visWrap.className = `bg-slate-950 border border-slate-800 rounded-xl p-2 relative overflow-hidden h-14 flex items-center${visualizerEnabled ? '' : ' hidden'}`;
                }
                if (maxBtn) { maxBtn.textContent = '⛶'; maxBtn.title = 'Maximizar reproductor'; }
                const savePlBtn = document.getElementById('dockSavePlaylistBtn');
                if (savePlBtn) savePlBtn.classList.add('hidden');
                const savedPlBtn = document.getElementById('dockSavedPlaylistsBtn');
                if (savedPlBtn) savedPlBtn.classList.add('hidden');
            }

            const audioEl = document.getElementById('dockAudioElement');
            if (visualizerEnabled && audioEl && !audioEl.paused) {
                startVisualizerLoop();
            }
        }

        function toggleMusicDockMaximize() {
            setMusicDockMode(musicDockMode === 'maximized' ? 'normal' : 'maximized');
        }

        function closeMusicDock() {
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl) {
                audioEl.pause();
                audioEl.removeAttribute('src');
                audioEl.load();
            }
            if (visualizerRafId) {
                cancelAnimationFrame(visualizerRafId);
                visualizerRafId = null;
            }
            musicPlaylist = [];
            musicPlayOrder = [];
            musicOrderPos = -1;
            musicPlaylistSearchQuery = '';
            const searchInput = document.getElementById('dockPlaylistSearchInput');
            if (searchInput) searchInput.value = '';
            const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
            if (clearBtn) clearBtn.classList.add('hidden');
            document.getElementById('musicDockWidget').classList.add('hidden');
        }

        function buildAudioQueryParams(source, track) {
            const taskName = document.getElementById('taskSelector').value || '';
            const mtimeParam = track.mtime ? `&mtime=${encodeURIComponent(track.mtime)}` : '';
            if (source === 'local') {
                return track.full_path
                    ? `source=local&full_path=${encodeURIComponent(track.full_path)}`
                    : `source=local&task=${encodeURIComponent(taskName)}&rel_path=${encodeURIComponent(track.rel_path || '')}`;
            }
            return `source=remote&remote_id=${encodeURIComponent(track.id)}&name=${encodeURIComponent(track.name || 'audio')}${mtimeParam}`;
        }

        function initDockWebAudio(audioEl) {
            if (eqInitialized) return;
            try {
                const AudioContextClass = window.AudioContext || window.webkitAudioContext;
                if (!AudioContextClass) return;
                audioCtx = new AudioContextClass();
                const sourceNode = audioCtx.createMediaElementSource(audioEl);

                eqFilters = EQ_BANDS_CONFIG.map((cfg, idx) => {
                    const filter = audioCtx.createBiquadFilter();
                    filter.type = cfg.type;
                    filter.frequency.value = cfg.freq;
                    filter.Q.value = 1.0;
                    const slider = document.getElementById(`eqBand${idx}`);
                    filter.gain.value = slider ? Number(slider.value || 0) : 0;
                    return filter;
                });

                analyserNode = audioCtx.createAnalyser();
                analyserNode.fftSize = 128;
                analyserNode.smoothingTimeConstant = 0.62;

                let prevNode = sourceNode;
                eqFilters.forEach(f => {
                    prevNode.connect(f);
                    prevNode = f;
                });
                prevNode.connect(analyserNode);
                analyserNode.connect(audioCtx.destination);
                eqInitialized = true;
            } catch (e) {}
        }

        function formatTrackTime(secs) {
            if (!isFinite(secs) || secs < 0) return '0:00';
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60);
            return `${m}:${s < 10 ? '0' : ''}${s}`;
        }

        function getTrackFavoriteKey(track, source = musicSource) {
            if (!track) return '';
            if (source === 'remote' && track.id != null) {
                return `remote:${track.id}`;
            }
            const cleanName = (track.name || '').trim().toLowerCase();
            return `name:${cleanName}`;
        }

        function isTrackFavorite(track, source = musicSource) {
            if (!track) return false;
            const primaryKey = getTrackFavoriteKey(track, source);
            if (primaryKey && favoriteTracksMap[primaryKey]) return true;
            const byNameKey = `name:${(track.name || '').trim().toLowerCase()}`;
            return Boolean(favoriteTracksMap[byNameKey]);
        }

        function updateDockFavoriteButtonUI() {
            const btn = document.getElementById('dockFavoriteBtn');
            const miniBtn = document.getElementById('miniFavoriteBtn');
            const currentTrack = (musicOrderPos >= 0 && musicPlayOrder.length > 0)
                ? musicPlaylist[musicPlayOrder[musicOrderPos]]
                : null;
            const isFav = isTrackFavorite(currentTrack, musicSource);

            if (btn) {
                if (isFav) {
                    btn.textContent = '❤️';
                    btn.className = 'w-9 h-9 rounded-xl bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/50 text-rose-400 flex items-center justify-center text-base shadow-md shadow-rose-500/15 transition';
                    btn.title = 'Quitar de favoritos (guardado en filejump_sync_studio)';
                } else {
                    btn.textContent = '🤍';
                    btn.className = 'w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-base transition';
                    btn.title = 'Agregar a favoritos (guardado en filejump_sync_studio)';
                }
            }

            if (miniBtn) {
                if (isFav) {
                    miniBtn.textContent = '❤️';
                    miniBtn.className = 'w-7 h-7 rounded-full bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/50 text-rose-400 flex items-center justify-center text-xs transition';
                    miniBtn.title = 'Quitar de favoritos';
                } else {
                    miniBtn.textContent = '🤍';
                    miniBtn.className = 'w-7 h-7 rounded-full bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-xs transition';
                    miniBtn.title = 'Agregar a favoritos';
                }
            }
        }

        async function loadRemoteMusicFavorites() {
            try {
                const res = await fetch('/api/music/favorites');
                const data = await res.json();
                if (data && data.favorites && typeof data.favorites === 'object') {
                    favoriteTracksMap = data.favorites;
                    updateDockFavoriteButtonUI();
                    if (musicPlaylist.length > 0) {
                        renderMusicPlaylist();
                    }
                }
            } catch (e) {}
        }

        async function toggleCurrentTrackFavorite() {
            if (favoritesSyncInProgress || musicOrderPos < 0 || musicPlayOrder.length === 0) return;
            const track = musicPlaylist[musicPlayOrder[musicOrderPos]];
            if (!track) return;

            const key = getTrackFavoriteKey(track, musicSource);
            const nameKey = `name:${(track.name || '').trim().toLowerCase()}`;
            const currentlyFav = isTrackFavorite(track, musicSource);
            const activeKey = favoriteTracksMap[key] ? key : (favoriteTracksMap[nameKey] ? nameKey : key);

            // Actualización optimista inmediata en la interfaz
            if (currentlyFav) {
                delete favoriteTracksMap[key];
                delete favoriteTracksMap[nameKey];
            } else {
                favoriteTracksMap[key] = {
                    name: track.name || '',
                    title: track.id3_title || '',
                    artist: track.id3_artist || '',
                    album: track.id3_album || '',
                    source: musicSource,
                    remote_id: track.id || null
                };
            }
            updateDockFavoriteButtonUI();
            renderMusicPlaylist();

            favoritesSyncInProgress = true;
            const btn = document.getElementById('dockFavoriteBtn');
            const miniBtn = document.getElementById('miniFavoriteBtn');
            if (btn) btn.classList.add('opacity-70');
            if (miniBtn) miniBtn.classList.add('opacity-70');

            try {
                const res = await fetch('/api/music/favorites', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        action: currentlyFav ? 'remove' : 'add',
                        key: activeKey,
                        meta: {
                            name: track.name || '',
                            title: track.id3_title || '',
                            artist: track.id3_artist || '',
                            album: track.id3_album || '',
                            source: musicSource,
                            remote_id: track.id || null,
                            full_path: track.full_path || '',
                            rel_path: track.rel_path || track.name || '',
                            size: track.size || 0,
                            mtime: track.mtime || 0
                        }
                    })
                });
                const data = await res.json();
                if (data && data.favorites) {
                    favoriteTracksMap = data.favorites;
                }
                if (!data.success && data.error) {
                    showNotice('Aviso de Favoritos', data.error);
                }
                refreshStatusAndLogs();
            } catch (e) {
                showNotice('Error', 'No se pudo guardar la lista de favoritos en FileJump.');
            } finally {
                favoritesSyncInProgress = false;
                if (btn) btn.classList.remove('opacity-70');
                if (miniBtn) miniBtn.classList.remove('opacity-70');
                updateDockFavoriteButtonUI();
                renderMusicPlaylist();
            }
        }

        async function openMusicDockPlayer(source, fileMeta, keepExistingPlaylist = false) {
            musicSource = source;
            if (!keepExistingPlaylist) {
                const folderFiles = getCurrentFolderFiles(source);
                const audioFiles = folderFiles.filter(f => isAudioFilename(f.name));
                musicPlaylist = audioFiles.length > 0 ? audioFiles : [fileMeta];
                const clickedIdx = musicPlaylist.findIndex(f =>
                    source === 'local'
                        ? (f.full_path && fileMeta.full_path ? f.full_path === fileMeta.full_path : f.rel_path === fileMeta.rel_path)
                        : String(f.id) === String(fileMeta.id)
                );
                rebuildMusicPlayOrder(clickedIdx >= 0 ? clickedIdx : 0);
            }

            if (musicDockMode === 'minimized') {
                setMusicDockMode('normal');
            } else {
                setMusicDockMode(musicDockMode || 'normal');
            }

            await playDockTrackAtOrderPos(musicOrderPos >= 0 ? musicOrderPos : 0);
        }

        async function playDockTrackAtOrderPos(orderPos) {
            if (musicPlayOrder.length === 0 || orderPos < 0 || orderPos >= musicPlayOrder.length) return;
            musicOrderPos = orderPos;
            const realIdx = musicPlayOrder[musicOrderPos];
            const track = musicPlaylist[realIdx];
            if (!track) return;

            const audioEl = document.getElementById('dockAudioElement');
            const qParams = buildAudioQueryParams(musicSource, track);
            const streamUrl = `/api/file/stream?${qParams}`;
            const metaUrl = `/api/file/audio_meta?${qParams}`;

            // Actualizar etiquetas iniciales antes de recibir ID3
            const fallbackTitle = (track.name || 'Pista de audio').replace(/\.[^/.]+$/, '');
            document.getElementById('dockTrackTitle').textContent = fallbackTitle;
            document.getElementById('dockTrackTitle').title = track.name || fallbackTitle;
            document.getElementById('dockTrackArtist').textContent = musicSource === 'remote' ? 'Cargando desde FileJump...' : 'Archivo local';
            document.getElementById('dockTrackAlbum').textContent = '';
            document.getElementById('miniTrackTitle').textContent = fallbackTitle;
            document.getElementById('miniTrackArtist').textContent = 'Reproduciendo...';
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            document.getElementById('dockSourceBadge').textContent = musicSource === 'remote' ? '☁️ Nube' : '💻 Local';

            currentCoverDataUrl = null;
            currentCoverFilename = `${fallbackTitle}_caratula.jpg`;
            document.getElementById('dockCoverBox').innerHTML = '<span id="dockCoverEmoji" class="text-3xl">🎵</span>';
            document.getElementById('miniCoverWrap').innerHTML = '<span class="text-base">🎵</span>';

            updateDockFavoriteButtonUI();
            renderMusicPlaylist();

            initDockWebAudio(audioEl);
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }

            audioEl.src = streamUrl;
            const volSlider = document.getElementById('dockVolumeBar');
            if (volSlider) audioEl.volume = Number(volSlider.value || 85) / 100;

            audioEl.onloadedmetadata = () => {
                document.getElementById('dockTotalTime').textContent = formatTrackTime(audioEl.duration);
            };
            let lastTimeUiUpdate = 0;
            audioEl.ontimeupdate = () => {
                const now = performance.now();
                if (now - lastTimeUiUpdate < 250) return;
                lastTimeUiUpdate = now;
                const cur = audioEl.currentTime || 0;
                const dur = audioEl.duration || 0;
                document.getElementById('dockCurrentTime').textContent = formatTrackTime(cur);
                document.getElementById('dockTotalTime').textContent = formatTrackTime(dur);
                document.getElementById('dockSeekBar').value = dur > 0 ? Math.round((cur / dur) * 100) : 0;
            };
            audioEl.onplay = () => {
                document.getElementById('dockPlayPauseBtn').textContent = '⏸';
                document.getElementById('miniPlayPauseBtn').textContent = '⏸';
                startVisualizerLoop();
            };
            audioEl.onpause = () => {
                document.getElementById('dockPlayPauseBtn').textContent = '▶';
                document.getElementById('miniPlayPauseBtn').textContent = '▶';
            };
            audioEl.onended = () => {
                if (musicOrderPos + 1 < musicPlayOrder.length) {
                    playDockTrackAtOrderPos(musicOrderPos + 1);
                } else if (musicPlayOrder.length > 1) {
                    playDockTrackAtOrderPos(0);
                }
            };

            try {
                await audioEl.play();
            } catch (e) {}

            if (musicSource === 'remote' && track.id) {
                prefetchedRemoteIds.add(String(track.id));
                renderMusicPlaylist();
            }

            // Cargar metadatos ID3 y carátula incrustada en paralelo
            fetch(metaUrl)
                .then(r => r.json())
                .then(meta => {
                    if (!meta || meta.error) return;
                    const title = meta.title || fallbackTitle;
                    const artist = meta.artist || 'Artista desconocido';
                    const album = meta.album || '';
                    track.id3_title = title;
                    track.id3_artist = artist;
                    track.id3_album = album;
                    document.getElementById('dockTrackTitle').textContent = title;
                    document.getElementById('dockTrackTitle').title = title;
                    document.getElementById('dockTrackArtist').textContent = artist;
                    document.getElementById('dockTrackAlbum').textContent = album;
                    document.getElementById('miniTrackTitle').textContent = title;
                    document.getElementById('miniTrackArtist').textContent = artist;
                    renderMusicPlaylist();

                    if (meta.cover_data_url) {
                        currentCoverDataUrl = meta.cover_data_url;
                        document.getElementById('dockCoverBox').innerHTML = `<img src="${meta.cover_data_url}" alt="Cover" class="w-full h-full object-cover">`;
                        document.getElementById('miniCoverWrap').innerHTML = `<img src="${meta.cover_data_url}" alt="Cover" class="w-full h-full object-cover">`;
                    }
                })
                .catch(() => {});

            prefetchNextRemoteTrack();
        }
        function filterMusicPlaylist(query) {
            musicPlaylistSearchQuery = (query || '').trim().toLowerCase();
            const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
            if (clearBtn) {
                clearBtn.classList.toggle('hidden', musicPlaylistSearchQuery.length === 0);
            }
            renderMusicPlaylist();
        }

        function clearMusicPlaylistSearch() {
            const input = document.getElementById('dockPlaylistSearchInput');
            if (input) input.value = '';
            filterMusicPlaylist('');
        }

        function locateCurrentTrackInPlaylist() {
            if (musicOrderPos < 0 || musicPlayOrder.length === 0) return;

            // Si hay un filtro de búsqueda activo que oculta la pista actual, limpiarlo para mostrarla en su posición
            if (musicPlaylistSearchQuery) {
                const input = document.getElementById('dockPlaylistSearchInput');
                if (input) input.value = '';
                musicPlaylistSearchQuery = '';
                const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
                if (clearBtn) clearBtn.classList.add('hidden');
                renderMusicPlaylist();
            }

            const rowEl = document.getElementById(`dockPlaylistRow_${musicOrderPos}`);
            if (!rowEl) return;

            rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            rowEl.classList.add('ring-2', 'ring-indigo-400', 'bg-indigo-500/35');
            setTimeout(() => {
                rowEl.classList.remove('ring-2', 'ring-indigo-400', 'bg-indigo-500/35');
            }, 1300);
        }

        function renderMusicPlaylist() {
            const container = document.getElementById('dockPlaylistItems');
            const orderTag = document.getElementById('dockPlaylistOrderTag');
            if (!container) return;

            if (orderTag) {
                orderTag.textContent = musicShuffle ? '🔀 Aleatorio activo' : 'Orden normal';
            }

            if (musicPlayOrder.length === 0) {
                container.innerHTML = '<div class="px-3.5 py-4 text-center text-slate-500">Cola vacía</div>';
                return;
            }

            const q = musicPlaylistSearchQuery;
            const visibleEntries = [];
            musicPlayOrder.forEach((realIdx, pos) => {
                const tr = musicPlaylist[realIdx];
                if (!tr) return;
                if (q) {
                    const haystack = `${tr.name || ''} ${tr.id3_title || ''} ${tr.id3_artist || ''} ${tr.id3_album || ''}`.toLowerCase();
                    if (!haystack.includes(q)) return;
                }
                visibleEntries.push({ tr, pos });
            });

            if (visibleEntries.length === 0) {
                container.innerHTML = `<div class="px-3.5 py-6 text-center text-slate-500">No se encontraron canciones o artistas para "<span class="text-slate-300">${escapeAttr(musicPlaylistSearchQuery)}</span>"</div>`;
                return;
            }

            container.innerHTML = visibleEntries.map(({ tr, pos }) => {
                const isCurrent = pos === musicOrderPos;
                const isCached = musicSource === 'remote' && tr.id && prefetchedRemoteIds.has(String(tr.id));
                const isFav = isTrackFavorite(tr, musicSource);
                const displayTitle = tr.id3_title || tr.name;
                const displaySub = tr.id3_artist && tr.id3_artist !== 'Artista desconocido' ? `${tr.id3_artist} • ${tr.name}` : tr.name;
                return `
                    <div id="dockPlaylistRow_${pos}" onclick="playDockTrackAtOrderPos(${pos})"
                         class="px-3.5 py-2 flex items-center justify-between gap-2 cursor-pointer transition ${isCurrent ? 'bg-indigo-600/25 text-white font-semibold' : 'text-slate-300 hover:bg-slate-800/70'}">
                        <div class="flex items-center gap-2 min-w-0">
                            <span class="text-[10px] font-mono ${isCurrent ? 'text-indigo-300' : 'text-slate-500'} w-5 shrink-0">${isCurrent ? '▶' : (pos + 1)}</span>
                            <div class="min-w-0">
                                <div class="flex items-center gap-1.5 min-w-0">
                                    <span class="truncate" title="${escapeAttr(displayTitle)}">${escapeAttr(displayTitle)}</span>
                                    ${isFav ? '<span class="text-[11px] text-rose-400 shrink-0" title="Canción favorita">❤️</span>' : ''}
                                </div>
                                ${tr.id3_title ? `<div class="text-[10px] text-slate-400 truncate font-normal">${escapeAttr(displaySub)}</div>` : ''}
                            </div>
                        </div>
                        <div class="flex items-center gap-1.5 shrink-0">
                            ${isCached ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/25">⚡ Caché</span>' : ''}
                            <span class="text-[10px] text-slate-500">${formatBytes(tr.size)}</span>
                        </div>
                    </div>`;
            }).join('');
        }

        function toggleDockPlayPause() {
            const audioEl = document.getElementById('dockAudioElement');
            if (!audioEl || !audioEl.src) return;
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            if (audioEl.paused) audioEl.play().catch(() => {});
            else audioEl.pause();
        }

        function playDockPrev() {
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl && audioEl.currentTime > 4) {
                audioEl.currentTime = 0;
                return;
            }
            if (musicPlayOrder.length === 0) return;
            const prevPos = (musicOrderPos - 1 + musicPlayOrder.length) % musicPlayOrder.length;
            playDockTrackAtOrderPos(prevPos);
        }

        function playDockNext() {
            if (musicPlayOrder.length === 0) return;
            const nextPos = (musicOrderPos + 1) % musicPlayOrder.length;
            playDockTrackAtOrderPos(nextPos);
        }

        function seekDockAudio(pct) {
            const audioEl = document.getElementById('dockAudioElement');
            if (!audioEl || !isFinite(audioEl.duration) || audioEl.duration <= 0) return;
            audioEl.currentTime = (Number(pct) / 100) * audioEl.duration;
        }

        function toggleDockShuffle() {
            musicShuffle = !musicShuffle;
            const btn = document.getElementById('dockShuffleBtn');
            if (btn) {
                btn.className = musicShuffle
                    ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-xs text-indigo-300 transition flex items-center gap-1'
                    : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-400 transition flex items-center gap-1';
            }
            const currentRealIdx = musicOrderPos >= 0 ? musicPlayOrder[musicOrderPos] : 0;
            rebuildMusicPlayOrder(currentRealIdx);
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            renderMusicPlaylist();
            prefetchNextRemoteTrack();
        }

        function setDockVolume(val) {
            const num = Math.max(0, Math.min(100, Number(val)));
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl) {
                audioEl.volume = num / 100;
                audioEl.muted = num === 0;
            }
            if (num > 0) lastNonZeroVolume = num;
            document.getElementById('dockVolumeLabel').textContent = `${num}%`;
            document.getElementById('dockMuteBtn').textContent = num === 0 ? '🔇' : (num < 45 ? '🔉' : '🔊');
        }

        function toggleDockMute() {
            const slider = document.getElementById('dockVolumeBar');
            const current = Number(slider.value || 0);
            const target = current > 0 ? 0 : (lastNonZeroVolume || 85);
            slider.value = target;
            setDockVolume(target);
        }

        function toggleDockEqPanel() {
            const panel = document.getElementById('dockEqPanel');
            const btn = document.getElementById('dockEqToggleBtn');
            const isHidden = panel.classList.toggle('hidden');
            btn.className = !isHidden
                ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1'
                : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-300 transition flex items-center gap-1';
        }

        function setEqBandGain(bandIdx, gainDb) {
            const val = Number(gainDb || 0);
            const label = document.getElementById(`eqVal${bandIdx}`);
            if (label) label.textContent = `${val > 0 ? '+' : ''}${val}dB`;
            if (eqFilters[bandIdx]) {
                eqFilters[bandIdx].gain.value = val;
            }
        }

        function applyEqPreset(presetName) {
            const gains = EQ_PRESETS[presetName] || EQ_PRESETS.flat;
            const selectEl = document.getElementById('dockEqPreset');
            if (selectEl) selectEl.value = presetName;
            gains.forEach((g, idx) => {
                const slider = document.getElementById(`eqBand${idx}`);
                if (slider) slider.value = g;
                setEqBandGain(idx, g);
            });
        }

        function toggleDockVisualizer() {
            visualizerEnabled = !visualizerEnabled;
            const wrap = document.getElementById('dockVisualizerWrap');
            const btn = document.getElementById('dockVisToggleBtn');
            wrap.classList.toggle('hidden', !visualizerEnabled);
            btn.className = visualizerEnabled
                ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1'
                : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-400 transition flex items-center gap-1';
            if (visualizerEnabled) startVisualizerLoop();
        }

        function openCoverLightbox() {
            if (!currentCoverDataUrl) return;
            const modal = document.getElementById('coverLightboxModal');
            document.getElementById('coverLightboxImg').src = currentCoverDataUrl;
            const dlBtn = document.getElementById('coverDownloadBtn');
            dlBtn.href = currentCoverDataUrl;
            dlBtn.download = currentCoverFilename;
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function closeCoverLightbox() {
            const modal = document.getElementById('coverLightboxModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }


        // =========================================================================
        // GUARDAR Y ABRIR LISTAS DE REPRODUCCIÓN (.M3U ESTÁNDAR)
        // =========================================================================
        function computeDefaultPlaylistName() {
            const now = new Date();
            const dd = String(now.getDate()).padStart(2, '0');
            const mm = String(now.getMonth() + 1).padStart(2, '0');
            const yyyy = now.getFullYear();
            const fallbackDateName = `playlist-${dd}-${mm}-${yyyy}`;

            if (!Array.isArray(musicPlaylist) || musicPlaylist.length === 0) {
                return fallbackDateName;
            }

            const folderNames = new Set();
            let hasRootTrack = false;

            for (const tr of musicPlaylist) {
                const src = tr.source || musicSource || 'local';
                if (src === 'remote') {
                    const rel = String(tr.rel_path || '').replace(/^\/+|\/+$/g, '');
                    const parts = rel.split('/').filter(Boolean);
                    if (parts.length > 1) {
                        folderNames.add(parts[parts.length - 2]);
                    } else {
                        const rSubEl = document.getElementById('remotePathSubtitle');
                        const rSub = rSubEl ? rSubEl.textContent.trim() : '';
                        const subParts = rSub.split('/').map(s => s.trim()).filter(Boolean);
                        if (subParts.length > 0 && !rSub.includes('Raíz')) {
                            folderNames.add(subParts[subParts.length - 1]);
                        } else {
                            hasRootTrack = true;
                        }
                    }
                } else {
                    const fp = String(tr.full_path || '').replace(/\\/g, '/');
                    const parts = fp.split('/').filter(Boolean);
                    if (parts.length >= 3) {
                        folderNames.add(parts[parts.length - 2]);
                    } else {
                        hasRootTrack = true;
                    }
                }
            }

            if (!hasRootTrack && folderNames.size === 1) {
                const singleFolder = Array.from(folderNames)[0];
                if (singleFolder && singleFolder.toLowerCase() !== 'root' && singleFolder !== '/') {
                    return singleFolder;
                }
            }
            return fallbackDateName;
        }

        function openSavePlaylistModal() {
            if (!Array.isArray(musicPlaylist) || musicPlaylist.length === 0) {
                showNotice('Lista vacía', 'No hay pistas cargadas en el reproductor para guardar.');
                return;
            }
            const defaultName = computeDefaultPlaylistName();
            const input = document.getElementById('savePlaylistNameInput');
            const hint = document.getElementById('savePlaylistDefaultHint');
            if (input) {
                input.value = defaultName;
            }
            if (hint) {
                hint.textContent = `Sugerencia automática (${musicPlaylist.length} pista(s)): ${defaultName}.m3u`;
            }
            const modal = document.getElementById('savePlaylistModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            setTimeout(() => {
                if (input) {
                    input.focus();
                    input.select();
                }
            }, 60);
        }

        function closeSavePlaylistModal() {
            const modal = document.getElementById('savePlaylistModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        async function submitSavePlaylistModal() {
            const input = document.getElementById('savePlaylistNameInput');
            const btn = document.getElementById('confirmSavePlaylistBtn');
            const nameVal = input ? input.value.trim() : '';
            if (!nameVal) {
                showNotice('Nombre requerido', 'Ingresa un nombre para la lista de reproducción.');
                return;
            }

            const payloadTracks = musicPlaylist.map(tr => ({
                ...tr,
                source: tr.source || musicSource || 'local'
            }));

            if (btn) {
                btn.disabled = true;
                btn.classList.add('opacity-60');
            }
            try {
                const res = await fetch('/api/music/playlist/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: nameVal,
                        tracks: payloadTracks
                    })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    closeSavePlaylistModal();
                    showNotice('Lista Guardada', data.message || `Lista "${data.filename}" guardada en filejump_sync_studio/playlist.`);
                } else {
                    showNotice('No se pudo guardar la lista', data.error || 'Ya existe un archivo con ese nombre o hubo un error.');
                }
            } catch (e) {
                showNotice('Error de Red', 'Error de comunicación con el servidor al guardar la lista .m3u.');
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.classList.remove('opacity-60');
                }
            }
        }

        async function openM3uPlaylistFile(source, fileMeta) {
            try {
                const res = await fetch('/api/music/playlist/load', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source,
                        id: fileMeta.id,
                        name: fileMeta.name,
                        full_path: fileMeta.full_path,
                        mtime: fileMeta.mtime || 0
                    })
                });
                const data = await res.json();
                if (!res.ok || !data.success || !Array.isArray(data.tracks) || data.tracks.length === 0) {
                    showNotice('Lista de Reproducción', data.error || 'No se pudieron cargar canciones desde el archivo .m3u.');
                    return;
                }

                musicPlaylist = data.tracks;
                musicSource = data.tracks[0].source || source;
                rebuildMusicPlayOrder(0);
                setMusicDockMode('maximized');
                await playDockTrackAtOrderPos(0);
            } catch (e) {
                showNotice('Error al abrir .m3u', 'No se pudo procesar el archivo de lista de reproducción.');
            }
        }


        // =========================================================================
        // VENTANA DE LISTAS DE REPRODUCCIÓN GUARDADAS (.M3U EN FILEJUMP_SYNC_STUDIO)
        // =========================================================================
        let savedPlaylistsCache = [];
        let selectedSavedPlaylistId = null;

        function openSavedPlaylistsModal() {
            selectedSavedPlaylistId = null;
            updateSavedPlaylistsActionButtons();
            const modal = document.getElementById('savedPlaylistsModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            loadSavedPlaylistsList();
        }

        function closeSavedPlaylistsModal() {
            const modal = document.getElementById('savedPlaylistsModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        function updateSavedPlaylistsActionButtons() {
            const delBtn = document.getElementById('deleteSelectedPlaylistBtn');
            const playBtn = document.getElementById('playSelectedPlaylistBtn');
            const hasSel = selectedSavedPlaylistId !== null && savedPlaylistsCache.some(p => String(p.id) === String(selectedSavedPlaylistId));
            if (delBtn) delBtn.disabled = !hasSel;
            if (playBtn) playBtn.disabled = !hasSel;
        }

        function selectSavedPlaylistRow(playlistId) {
            selectedSavedPlaylistId = String(playlistId);
            renderSavedPlaylistsList();
            updateSavedPlaylistsActionButtons();
        }

        function renderSavedPlaylistsList() {
            const container = document.getElementById('savedPlaylistsListContainer');
            if (!container) return;

            if (!Array.isArray(savedPlaylistsCache) || savedPlaylistsCache.length === 0) {
                container.innerHTML = '<div class="py-12 px-4 text-center text-xs text-slate-500">No hay listas de reproducción (.m3u) guardadas en <code>filejump_sync_studio/playlist/</code>.</div>';
                updateSavedPlaylistsActionButtons();
                return;
            }

            container.innerHTML = savedPlaylistsCache.map(pl => {
                const isSelected = String(pl.id) === String(selectedSavedPlaylistId);
                const rowClass = isSelected
                    ? 'bg-indigo-600/25 border-l-2 border-l-indigo-400 text-white'
                    : 'hover:bg-slate-900/90 text-slate-200';
                const baseLabel = pl.display_name || String(pl.name || '').replace(/\.m3u8?$/i, '');
                const countNum = Number(pl.track_count || 0);
                const pistasLabel = `${countNum} ${countNum === 1 ? 'pista' : 'pistas'}`;
                const sizeText = pl.size > 0 ? formatBytes(pl.size) : '';
                const dateText = pl.updated_at ? String(pl.updated_at).replace('T', ' ').slice(0, 16) : '';
                const subMeta = [pl.name, sizeText, dateText].filter(Boolean).join(' • ');

                return `
                    <div onclick="selectSavedPlaylistRow('${escapeAttr(String(pl.id))}')"
                         ondblclick="playSavedPlaylistById('${escapeAttr(String(pl.id))}')"
                         class="px-3.5 py-2.5 flex items-center justify-between gap-3 cursor-pointer select-none transition ${rowClass}">
                        <div class="flex items-center gap-2.5 min-w-0 flex-1">
                            <span class="text-base shrink-0">🎶</span>
                            <div class="min-w-0 flex-1">
                                <div class="flex items-center gap-2 min-w-0 text-xs font-semibold">
                                    <span class="truncate text-white">${escapeHtml(baseLabel)}</span>
                                    <span class="text-slate-600 font-mono shrink-0">------</span>
                                    <span class="px-2 py-0.5 rounded-full bg-indigo-500/15 border border-indigo-500/30 text-[11px] font-mono text-indigo-300 shrink-0">${escapeHtml(pistasLabel)}</span>
                                </div>
                                <div class="text-[10px] text-slate-400 truncate mt-0.5">${escapeHtml(subMeta || 'Lista M3U en FileJump')}</div>
                            </div>
                        </div>
                        <span class="text-[10px] font-mono text-slate-500 shrink-0">#${escapeHtml(String(pl.id))}</span>
                    </div>
                `;
            }).join('');

            updateSavedPlaylistsActionButtons();
        }

        async function loadSavedPlaylistsList() {
            const container = document.getElementById('savedPlaylistsListContainer');
            if (container) {
                container.innerHTML = '<div class="py-10 text-center text-xs text-slate-400 animate-pulse">Cargando playlists desde filejump_sync_studio/playlist...</div>';
            }
            try {
                const res = await fetch('/api/music/playlists');
                const data = await res.json();
                if (res.ok && data.success) {
                    savedPlaylistsCache = data.playlists || [];
                    if (selectedSavedPlaylistId !== null && !savedPlaylistsCache.some(p => String(p.id) === String(selectedSavedPlaylistId))) {
                        selectedSavedPlaylistId = null;
                    }
                    renderSavedPlaylistsList();
                } else {
                    savedPlaylistsCache = [];
                    if (container) {
                        container.innerHTML = `<div class="py-10 px-4 text-center text-xs text-rose-400">${escapeHtml(data.error || 'No se pudieron cargar las playlists.')}</div>`;
                    }
                }
            } catch (e) {
                if (container) {
                    container.innerHTML = '<div class="py-10 px-4 text-center text-xs text-rose-400">Error de red al consultar las listas guardadas.</div>';
                }
            }
        }

        async function playSavedPlaylistById(playlistId) {
            const pl = savedPlaylistsCache.find(p => String(p.id) === String(playlistId));
            if (!pl) return;
            selectedSavedPlaylistId = String(pl.id);
            renderSavedPlaylistsList();
            closeSavedPlaylistsModal();
            await openM3uPlaylistFile('remote', {
                id: pl.id,
                name: pl.name,
                mtime: 0
            });
        }

        async function playSelectedSavedPlaylist() {
            if (selectedSavedPlaylistId === null) return;
            await playSavedPlaylistById(selectedSavedPlaylistId);
        }

        async function deleteSelectedSavedPlaylist() {
            if (selectedSavedPlaylistId === null) return;
            const pl = savedPlaylistsCache.find(p => String(p.id) === String(selectedSavedPlaylistId));
            if (!pl) return;

            const delBtn = document.getElementById('deleteSelectedPlaylistBtn');
            if (delBtn) {
                delBtn.disabled = true;
                delBtn.classList.add('opacity-60');
            }
            try {
                const res = await fetch('/api/music/playlist/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id: pl.id,
                        name: pl.name
                    })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    savedPlaylistsCache = savedPlaylistsCache.filter(p => String(p.id) !== String(pl.id));
                    selectedSavedPlaylistId = null;
                    renderSavedPlaylistsList();
                    showNotice('Playlist Eliminado', data.message || `Se eliminó "${pl.name}" de filejump_sync_studio.`);
                } else {
                    showNotice('Error al eliminar', data.error || 'No se pudo eliminar la lista de reproducción.');
                }
            } catch (e) {
                showNotice('Error de Red', 'Error de comunicación con el servidor al eliminar el playlist.');
            } finally {
                if (delBtn) {
                    delBtn.classList.remove('opacity-60');
                    updateSavedPlaylistsActionButtons();
                }
            }
        }
