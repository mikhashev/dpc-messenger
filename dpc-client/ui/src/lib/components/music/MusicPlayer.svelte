<!-- MusicPlayer.svelte — the Music Player window, listener mode (ADR-051 D1, D2). -->
<!--
  A window the person can drag by its header, resize by the corner handle and
  minimise to its header bar, like the Agent map; its box and the minimised flag are
  remembered through utils/windowGeometry.ts under its own key. Minimised, the bar
  is the mini-player: prev / play / next, the title, "k / n", shuffle and repeat.
  The bar is resized by its right-edge handle and keeps its own width (miniWidth)
  and its own corner (miniX / miniY), remembered beside the box: the bar is dragged
  on its own, and restoring gives back the window at its own place and size. A narrow bar
  drops shuffle and repeat, then "k / n"; restore and close always stay.

  Non-modal: the chat stays usable beside it. The audio element and the playback
  state are not here but in services/music.ts, a singleton of the page, so playback
  goes on while the person switches chats and while this window is minimised or
  closed. Keyboard shortcuts (Space, N, P, Shift+S, Shift+R, arrows to seek, Alt+Up /
  Alt+Down to move a playlist row) work only while focus is inside the window.

  The playback source: the core's loopback track stream (ADR-051 N8, D28) on Linux and
  for files outside ~/.dpc, `asset:` for files under ~/.dpc on Windows and macOS. The
  one place that decides is `trackPlaybackUrl` in utils/musicPlayer.ts.

  Under Now playing sits the Lyrics area (MusicLyrics.svelte): the lyrics chain of
  the core (.lrc sidecar, file tags, LRCLIB, manual edit) for the current track.

  The Create button in the controls row opens the Create panel (MusicCreate.svelte: a song from
  your own text, its jobs, and the A/B review of its takes) in place of the listening
  columns; Listen is the default view on every opening. Keys inside the panel are the
  panel's own (the review's A / B, Space), not the queue's.

  The library comes from the core music commands; until they are wired into
  local_api the window says "Music library not available on this backend yet" and
  the player stays usable for nothing else.
-->

<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { connectionStatus } from '$lib/coreService';
  import { confirmAsync } from '$lib/utils/dialog';
  import MusicLyrics from './MusicLyrics.svelte';
  import MusicCreate from './MusicCreate.svelte';
  import MusicComments from './MusicComments.svelte';
  import MusicWaveform from './MusicWaveform.svelte';
  import { revealItemInDir } from '@tauri-apps/plugin-opener';
  import { revealInFolder } from '$lib/utils/revealInFolder';
  import { isCommentShortcut } from '$lib/utils/musicComments';
  import { ensureComments, musicComments } from '$lib/services/musicComments';
  import {
    HEADER_HEIGHT,
    isClick,
    loadWindowState,
    moveRect,
    resizeRect,
    saveWindowState,
    type WindowRect,
    type WindowState,
  } from '$lib/utils/windowGeometry';
  import {
    LIBRARY_POLL_MS,
    MUSIC_PLAYER_WINDOW,
    SMART_ALL_ID,
    TRACK_ROWS_STEP,
    canDeleteFile,
    canRevealFile,
    currentId,
    dropIndex,
    fitMusicPlayer,
    formatTime,
    hiddenEntries,
    matchesFilter,
    missingEntries,
    minimiseMusicPlayer,
    miniPlayerRect,
    miniPlayerShows,
    miniPosition,
    moveItem,
    moveMiniPlayer,
    nextPlaylistName,
    originBadgeTitle,
    originTag,
    playbackProblem,
    repeatLabel,
    resizeMiniWidth,
    resolveDuration,
    restoreMusicPlayer,
    trackSubtitle,
    type LibraryEntry,
    type MiniPosition,
    type Playlist,
  } from '$lib/utils/musicPlayer';
  import {
    addFolder,
    clearNotice,
    clearQueue,
    createPlaylist,
    cycleRepeatMode,
    deleteGeneratedFile,
    deletePlaylist,
    hideTrack,
    loadMusicLibrary,
    musicCurrentEntry,
    musicEntriesById,
    musicLibrary,
    musicNotice,
    musicPlayback,
    musicPosition,
    musicReview,
    nextTrack,
    playList,
    playQueueAt,
    playbackEnv,
    prevTrack,
    queueTrack,
    removeFolder,
    removeFromQueue,
    removeMissingTracks,
    renamePlaylist,
    rescanFolder,
    seekBy,
    seekTo,
    setPlaylistTracks,
    setShuffle,
    setVolume,
    togglePlay,
    unhideTrack,
  } from '$lib/services/music';

  let { open = $bindable(false), minimised = $bindable(false) }: { open?: boolean; minimised?: boolean } = $props();

  const LIMITS = MUSIC_PLAYER_WINDOW.limits;

  // -- the window ------------------------------------------------------------------

  let rootEl = $state<HTMLDivElement | null>(null);
  // The full window's box: where it is open, and where a restore brings it back.
  let rect = $state<WindowRect>({ x: 0, y: 0, width: 0, height: 0 });
  // The mini-player bar's own width; null until the bar is resized (then it is as wide as the window).
  let miniWidth = $state<number | null>(null);
  // The bar's own corner; null until the first minimise (then it is where the window's header is).
  let mini = $state<MiniPosition | null>(null);
  type GestureKind = 'move' | 'resize' | 'mini-resize';
  let gesture: { kind: GestureKind; px: number; py: number; start: WindowRect } | null = null;
  let dragged = false;

  const viewport = () => ({ width: window.innerWidth, height: window.innerHeight });
  // The bar's box at its own corner and width (both are kept in the viewport by fitMusicPlayer).
  const barRect = () => miniPlayerRect(rect, miniWidth, viewport(), mini);
  // Minimised, the window is drawn as the bar: at the bar's corner and width.
  let shown = $derived(minimised ? barRect() : rect);
  let shownWidth = $derived(shown.width);
  let miniShows = $derived(miniPlayerShows(shownWidth));

  function storage(): Storage | null {
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  }
  /** Both boxes as one state; a field never set is left out, so it stores as before. */
  function windowState(): WindowState {
    const w: WindowState = { rect, minimised };
    if (miniWidth !== null) w.miniWidth = miniWidth;
    if (mini) {
      w.miniX = mini.x;
      w.miniY = mini.y;
    }
    return w;
  }
  function apply(w: WindowState) {
    rect = w.rect;
    miniWidth = w.miniWidth ?? null;
    mini = miniPosition(w);
  }
  const remember = () => saveWindowState(storage(), windowState(), MUSIC_PLAYER_WINDOW);

  // Listen is the default view; Create is opened from the controls row, Listen from the header.
  let view = $state<'listen' | 'create'>('listen');

  // Each opening takes the remembered boxes, fitted to the viewport as it is now.
  $effect(() => {
    if (!open) return;
    untrack(() => {
      view = 'listen';
      const w = loadWindowState(storage(), viewport(), MUSIC_PLAYER_WINDOW);
      apply(fitMusicPlayer({ ...w, minimised }, viewport()));
      tick().then(() => rootEl?.focus({ preventScroll: true }));
    });
  });

  // Minimised or restored, from the bar or from the Music Player button: the bar goes
  // to its own corner, the window comes back at its own.
  let lastMinimised = minimised;
  $effect(() => {
    const m = minimised;
    untrack(() => {
      if (m === lastMinimised) return;
      lastMinimised = m;
      if (open) {
        const w = { ...windowState(), minimised: m };
        apply(m ? minimiseMusicPlayer(w, viewport()) : restoreMusicPlayer(w, viewport()));
        remember();
      }
    });
  });

  function close() {
    open = false;
  }

  function toggleMinimised() {
    minimised = !minimised;
  }

  function beginGesture(e: PointerEvent, kind: GestureKind) {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (kind === 'move' && target.closest('button, input, select')) return;
    // The bar's handle is not the bar: its press must never become the bar's "click to restore".
    if (kind === 'mini-resize') e.stopPropagation();
    const start = kind === 'mini-resize' || (kind === 'move' && minimised) ? barRect() : { ...rect };
    gesture = { kind, px: e.clientX, py: e.clientY, start };
    dragged = false;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    rootEl?.focus({ preventScroll: true });
    e.preventDefault();
  }

  function moveGesture(e: PointerEvent) {
    if (!gesture) return;
    const dx = e.clientX - gesture.px;
    const dy = e.clientY - gesture.py;
    if (!isClick(dx, dy)) dragged = true;
    const s = gesture.start;
    // The bar's left edge stays: the handle changes its width only.
    if (gesture.kind === 'mini-resize') {
      miniWidth = resizeMiniWidth(s, dx, viewport());
      mini = { x: s.x, y: s.y };
    } else if (gesture.kind === 'resize') rect = resizeRect(s, dx, dy, viewport(), LIMITS);
    // Minimised, only the bar moves, kept in view at its own width.
    else if (minimised) mini = moveMiniPlayer(s, dx, dy, viewport());
    else rect = moveRect(s, dx, dy, viewport(), false, LIMITS);
  }

  function endGesture(e: PointerEvent) {
    if (!gesture) return;
    const kind = gesture.kind;
    gesture = null;
    const el = e.currentTarget as HTMLElement;
    if (el.hasPointerCapture?.(e.pointerId)) el.releasePointerCapture(e.pointerId);
    // A press on the mini-player bar that did not drag is a click: restore.
    if (kind === 'move' && minimised && !dragged) {
      toggleMinimised();
      return;
    }
    remember();
  }

  function refit() {
    if (open) apply(fitMusicPlayer(windowState(), viewport()));
  }

  // -- the library -----------------------------------------------------------------

  // Reading: open and not minimised. Read now, then on the interval while the core
  // answers; a backend without the music commands is not asked again until "Try again".
  let active = $derived(open && !minimised);
  $effect(() => {
    if (!active) return;
    untrack(() => loadMusicLibrary($musicLibrary.status === 'ready'));
    const poll = setInterval(() => {
      if ($musicLibrary.status === 'ready') loadMusicLibrary(true);
    }, LIBRARY_POLL_MS);
    return () => clearInterval(poll);
  });

  // A reconnected backend: read again.
  let lastConnection = '';
  $effect(() => {
    const c = $connectionStatus;
    untrack(() => {
      if (active && c === 'connected' && lastConnection && lastConnection !== 'connected') loadMusicLibrary();
      lastConnection = c;
    });
  });

  let selectedListId = $state(SMART_ALL_ID);
  let lists = $derived($musicLibrary.playlists);
  let smartLists = $derived(lists.filter(p => p.kind === 'smart'));
  let userLists = $derived(lists.filter(p => p.kind === 'user'));
  let selectedList = $derived(lists.find(p => p.id === selectedListId) ?? lists[0] ?? null);
  let isUserList = $derived(selectedList?.kind === 'user');

  let filter = $state('');
  let rowLimit = $state(TRACK_ROWS_STEP);
  $effect(() => {
    // Another list or another filter starts at the top again.
    void selectedListId;
    void filter;
    untrack(() => (rowLimit = TRACK_ROWS_STEP));
  });

  // Hidden tracks (Hide) are left out of every list; "Show hidden" lists them below.
  let hiddenList = $derived(hiddenEntries($musicLibrary.entries));
  let missingList = $derived(missingEntries($musicLibrary.entries));
  let showHidden = $state(false);
  $effect(() => {
    if (!hiddenList.length) showHidden = false;
  });
  let menuId = $state<string | null>(null);

  type Row = { index: number; id: string; entry: LibraryEntry | null };
  // `index` is the place in the whole list (what a reorder works on), hidden tracks included.
  let listRows = $derived<Row[]>(
    (selectedList?.track_ids ?? []).map((id, index) => ({ index, id, entry: $musicEntriesById.get(id) ?? null })),
  );
  let allRows = $derived(listRows.filter(r => !r.entry?.hidden));
  let filteredRows = $derived(filter.trim() ? allRows.filter(r => matchesFilter(r.entry, filter)) : allRows);
  let shownRows = $derived(filteredRows.slice(0, rowLimit));
  // Reordering works on the whole list only, so a filtered view does not reorder.
  let canReorder = $derived(isUserList && !filter.trim() && allRows.length === listRows.length);

  let env = $derived($playbackEnv);
  function problemOf(entry: LibraryEntry | null) {
    if (!env) return null;
    return playbackProblem(entry, env);
  }

  let actionError = $state('');
  function fail(message: string) {
    actionError = message;
  }

  /** The origin badge: opens the file's folder with the file selected (Explorer, Finder, or the
   *  Linux file manager through the opener plugin) when the track is a file we can reach. */
  async function revealEntry(entry: LibraryEntry) {
    const failure = await revealInFolder(entry.path, revealItemInDir);
    if (failure) fail(failure);
  }

  // -- comments (mp.comment/1): the C key and the card's Comment button open the form ---------
  let commentSignal = $state(0);
  let regionSignal = $state(0);
  let regionRequest = $state<{ id: number; start: number; end: number } | null>(null);
  let draftRegion = $state<{ start: number; end: number } | null>(null);
  function pickRegion(start: number, end: number) {
    draftRegion = { start, end };
    regionRequest = { id: (regionRequest?.id ?? 0) + 1, start, end };
  }

  // -- playlist forms --------------------------------------------------------------

  let plForm = $state<{ mode: 'new' | 'rename'; name: string } | null>(null);
  let plBusy = $state(false);
  let plInput = $state<HTMLInputElement | null>(null);

  async function openPlForm(mode: 'new' | 'rename') {
    if (mode === 'rename' && !isUserList) return;
    plForm = { mode, name: mode === 'new' ? nextPlaylistName(lists) : selectedList?.name ?? '' };
    actionError = '';
    await tick();
    plInput?.focus();
    plInput?.select();
  }

  function closePlForm() {
    plForm = null;
  }

  async function submitPlForm() {
    if (!plForm || plBusy) return;
    const name = plForm.name.trim();
    if (!name) {
      fail('A playlist needs a name.');
      return;
    }
    plBusy = true;
    try {
      if (plForm.mode === 'new') {
        const a = await createPlaylist(name);
        if (a.ok) {
          selectedListId = a.data.playlist.id;
          plForm = null;
        } else fail(a.message);
      } else if (selectedList) {
        const a = await renamePlaylist(selectedList.id, name);
        if (a.ok) plForm = null;
        else fail(a.message);
      }
    } finally {
      plBusy = false;
    }
  }

  function plFormKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitPlForm();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closePlForm();
    }
  }

  async function removeSelectedList() {
    const p = selectedList;
    if (!p || p.kind !== 'user') return;
    const yes = await confirmAsync(`Delete the playlist "${p.name}"? Its tracks stay in the library.`, {
      title: 'Delete playlist',
      kind: 'warning',
    });
    if (!yes) return;
    const a = await deletePlaylist(p.id);
    if (a.ok) selectedListId = SMART_ALL_ID;
    else fail(a.message);
  }

  // -- playlist rows ---------------------------------------------------------------

  async function moveRow(index: number, to: number) {
    const p = selectedList;
    if (!p || p.kind !== 'user' || !canReorder) return;
    const ids = moveItem(p.track_ids, index, to);
    if (ids.every((id, i) => id === p.track_ids[i])) return;
    const moved = p.track_ids[index];
    const a = await setPlaylistTracks(p.id, ids);
    if (!a.ok) {
      fail(a.message);
      return;
    }
    const at = ids.indexOf(moved);
    musicNotice(`Moved "${$musicEntriesById.get(moved)?.title ?? moved}" to position ${at + 1} of ${ids.length}.`);
  }

  async function removeRow(index: number) {
    const p = selectedList;
    if (!p || p.kind !== 'user') return;
    const a = await setPlaylistTracks(p.id, p.track_ids.filter((_, i) => i !== index));
    if (!a.ok) fail(a.message);
  }

  async function addToList(trackId: string, e: Event) {
    const select = e.currentTarget as HTMLSelectElement;
    const listId = select.value;
    select.value = '';
    const p = lists.find(x => x.id === listId);
    if (!p || p.kind !== 'user') return;
    const title = $musicEntriesById.get(trackId)?.title ?? trackId;
    if (p.track_ids.includes(trackId)) {
      musicNotice(`"${title}" is already in ${p.name}.`);
      return;
    }
    const a = await setPlaylistTracks(p.id, [...p.track_ids, trackId]);
    if (a.ok) musicNotice(`Added "${title}" to ${p.name}.`);
    else fail(a.message);
  }

  // -- hide, remove, delete ---------------------------------------------------------

  async function hideRow(entry: LibraryEntry) {
    menuId = null;
    const a = await hideTrack(entry.id);
    if (a.ok) musicNotice(`Hid "${entry.title}". Show hidden (${hiddenList.length + 1}) brings it back.`);
    else fail(a.message);
  }

  async function unhideRow(entry: LibraryEntry) {
    const a = await unhideTrack(entry.id);
    if (!a.ok) fail(a.message);
  }

  async function removeMissingRow(entry: LibraryEntry) {
    menuId = null;
    const a = await removeMissingTracks(entry.id);
    if (!a.ok) fail(a.message);
  }

  async function removeAllMissing() {
    const n = missingList.length;
    if (!n) return;
    const yes = await confirmAsync(
      `Remove ${n} missing ${n === 1 ? 'track' : 'tracks'} from the library? Their files are already gone; they leave every playlist too.`,
      { title: 'Remove missing tracks', kind: 'warning' },
    );
    if (!yes) return;
    const a = await removeMissingTracks();
    if (a.ok) musicNotice(`Removed ${a.data.removed.length} missing ${a.data.removed.length === 1 ? 'track' : 'tracks'}.`);
    else fail(a.message);
  }

  async function deleteFileRow(entry: LibraryEntry) {
    menuId = null;
    const name = entry.source?.name ?? entry.title;
    const playing = $musicCurrentEntry?.id === entry.id && $musicPlayback.loadedId === entry.id;
    const yes = await confirmAsync(
      `Delete the file "${name}" from disk? This cannot be undone.${playing ? ' It is playing now and will stop.' : ''}`,
      { title: 'Delete file', kind: 'warning' },
    );
    if (!yes) return;
    const a = await deleteGeneratedFile(entry.id);
    if (a.ok) musicNotice(`Deleted "${name}".`);
    else fail(a.message);
  }

  function playRow(row: Row) {
    if (selectedList) playList(selectedList, row.id);
  }

  function playSelectedList() {
    if (selectedList) playList(selectedList);
  }

  // Drag a row by its handle (pointer events: HTML5 drag and drop is taken by the
  // webview's file drop on Windows, so it is not used).
  let listEl = $state<HTMLOListElement | null>(null);
  let rowDrag = $state<{ from: number; to: number; midpoints: number[] } | null>(null);

  function beginRowDrag(e: PointerEvent, index: number) {
    if (e.button !== 0 || !canReorder || !listEl) return;
    const rows = [...listEl.querySelectorAll<HTMLElement>('[data-row-index]')];
    const midpoints = rows.map(r => {
      const b = r.getBoundingClientRect();
      return b.top + b.height / 2;
    });
    rowDrag = { from: index, to: index, midpoints };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function moveRowDrag(e: PointerEvent) {
    if (!rowDrag) return;
    rowDrag = { ...rowDrag, to: dropIndex(rowDrag.midpoints, e.clientY, rowDrag.from) };
  }

  function endRowDrag(e: PointerEvent) {
    if (!rowDrag) return;
    const { from, to } = rowDrag;
    rowDrag = null;
    const el = e.currentTarget as HTMLElement;
    if (el.hasPointerCapture?.(e.pointerId)) el.releasePointerCapture(e.pointerId);
    if (to !== from) moveRow(from, to);
  }

  function cancelRowDrag() {
    rowDrag = null;
  }

  async function focusRow(index: number) {
    await tick();
    listEl?.querySelector<HTMLElement>(`[data-row-index="${index}"] .row-handle`)?.focus();
  }

  // -- folders ---------------------------------------------------------------------

  let folderBusy = $state<string | null>(null);
  let folderPathForm = $state<string | null>(null);

  async function pickFolder() {
    actionError = '';
    let picked: string | null = null;
    try {
      const { open: openDialog } = await import('@tauri-apps/plugin-dialog');
      const r = await openDialog({ multiple: false, directory: true, title: 'Add a music folder' });
      if (typeof r !== 'string' || !r) return;
      picked = r;
    } catch {
      // No Tauri dialog (the browser dev server): type the path instead.
      folderPathForm = '';
      return;
    }
    await addPickedFolder(picked);
  }

  async function addPickedFolder(path: string) {
    if (!path.trim()) return;
    folderBusy = 'add';
    try {
      const a = await addFolder(path.trim());
      if (a.ok) {
        folderPathForm = null;
        musicNotice(`Watching ${a.data.folder.path}; its first scan runs in the background.`);
      } else fail(a.message);
    } finally {
      folderBusy = null;
    }
  }

  async function rescan(id?: string) {
    folderBusy = id ?? 'all';
    try {
      const a = await rescanFolder(id);
      if (!a.ok) fail(a.message);
    } finally {
      folderBusy = null;
    }
  }

  async function unwatch(id: string, path: string) {
    const yes = await confirmAsync(`Stop watching ${path}? Its tracks leave the library; the files on disk are not touched.`, {
      title: 'Remove folder',
      kind: 'warning',
    });
    if (!yes) return;
    folderBusy = id;
    try {
      const a = await removeFolder(id);
      if (!a.ok) fail(a.message);
    } finally {
      folderBusy = null;
    }
  }

  function folderState(f: { watch_state?: string; file_count: number; hashed_count?: number }): string {
    const parts = [`${f.file_count} file${f.file_count === 1 ? '' : 's'}`];
    if (typeof f.hashed_count === 'number' && f.hashed_count < f.file_count) parts.push(`${f.hashed_count} hashed`);
    if (f.watch_state) parts.push(f.watch_state === 'polling' ? 'watched (polling)' : f.watch_state);
    return parts.join(' · ');
  }

  // -- the player ------------------------------------------------------------------

  let p = $derived($musicPlayback);
  let cur = $derived($musicCurrentEntry);
  let curComments = $derived($musicComments[cur?.id ?? ''] ?? []);
  $effect(() => {
    ensureComments(cur?.id);
  });
  let curProblem = $derived(cur ? problemOf(cur) : null);
  let loaded = $derived(!!p.loadedId && p.loadedId === currentId(p.queue));
  let shownDuration = $derived(resolveDuration(loaded && p.duration > 0 ? p.duration : null, cur?.duration_s, loaded ? p.currentTime : 0));
  let summary = $derived(
    $musicReview.active
      ? `Reviewing takes${$musicReview.playing ? ' (playing)' : ''}; the queue waits`
      : cur ? `${p.playing ? 'Playing' : loaded ? 'Paused' : 'Up next'}: ${cur.title}` : `${$musicLibrary.entries.length} tracks`,
  );

  function onVolume(e: Event) {
    setVolume(parseFloat((e.currentTarget as HTMLInputElement).value));
  }

  /** The slider is let go: write the last value now (the drag writes are throttled). */
  function onVolumeEnd(e: Event) {
    setVolume(parseFloat((e.currentTarget as HTMLInputElement).value), true);
  }

  // -- keyboard: only while focus is inside this window, never while typing --------

  function handleKeydown(e: KeyboardEvent) {
    if (!open || !rootEl) return;
    const a = document.activeElement as HTMLElement | null;
    if (!a || !rootEl.contains(a)) return;
    if (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
    // The Create panel handles its own keys (the review's A / B, Space, arrows).
    if (a.closest('[data-music-create]')) return;
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      const row = a.closest<HTMLElement>('[data-row-index]');
      if (row && canReorder) {
        e.preventDefault();
        const index = Number(row.dataset.rowIndex);
        const to = index + (e.key === 'ArrowUp' ? -1 : 1);
        moveRow(index, to).then(() => focusRow(Math.max(0, Math.min(allRows.length - 1, to))));
      }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const kl = k.toLowerCase();
    if (isCommentShortcut(e, a)) {
      if (!cur) return;
      e.preventDefault();
      commentSignal += 1;
    } else if (kl === 'r' && !e.shiftKey) {
      if (!cur) return;
      e.preventDefault();
      regionSignal += 1;
    } else if (k === ' ') {
      if (a.tagName === 'BUTTON') return; // the button presses itself
      e.preventDefault();
      togglePlay();
    } else if (kl === 'n') {
      e.preventDefault();
      nextTrack();
    } else if (kl === 'p') {
      e.preventDefault();
      prevTrack();
    } else if (e.shiftKey && kl === 's') {
      e.preventDefault();
      setShuffle(!p.shuffle);
    } else if (e.shiftKey && kl === 'r') {
      e.preventDefault();
      cycleRepeatMode();
    } else if (k === 'ArrowLeft' || k === 'ArrowRight') {
      if (!loaded) return;
      e.preventDefault();
      seekBy((k === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 1 : 5));
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} onresize={refit} />

{#snippet originBadge(entry: LibraryEntry)}
  {#if canRevealFile(entry)}
    <button type="button" class="tag link" title={originBadgeTitle(entry)} aria-label="Show in folder: {entry.title}" onclick={() => revealEntry(entry)}>{originTag(entry)}</button>
  {:else}
    <span class="tag">{originTag(entry)}</span>
  {/if}
{/snippet}

{#snippet icon(name: 'prev' | 'next' | 'play' | 'pause' | 'shuffle' | 'repeat' | 'repeat-one')}
  <!-- Inline SVG: glyphs such as U+23EE render as emoji, or not at all, on some Linux fonts. -->
  <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    {#if name === 'prev'}<path d="M6 5v14" /><path d="M19 5 9 12l10 7z" fill="currentColor" />
    {:else if name === 'next'}<path d="M18 5v14" /><path d="m5 5 10 7-10 7z" fill="currentColor" />
    {:else if name === 'play'}<path d="m7 4 13 8-13 8z" fill="currentColor" />
    {:else if name === 'pause'}<path d="M7 5v14M17 5v14" stroke-width="3.5" />
    {:else if name === 'shuffle'}<path d="M3 6h4l10 12h4M3 18h4l3-3.6M14 9.6 17 6h4M18 3l3 3-3 3M18 15l3 3-3 3" />
    {:else}<path d="M4 11V9a3 3 0 0 1 3-3h13M17 3l3 3-3 3M20 13v2a3 3 0 0 1-3 3H4M7 21l-3-3 3-3" />{#if name === 'repeat-one'}<path d="M11.5 10.5 12.5 10v5" stroke-width="1.8" />{/if}
    {/if}
  </svg>
{/snippet}

{#snippet transport(small: boolean)}
  <div class="transport" class:small>
    <button type="button" class="t-btn" onclick={prevTrack} title="Previous (P); restarts after 3 s" aria-label="Previous track">{@render icon('prev')}</button>
    <button
      type="button"
      class="t-btn play"
      onclick={togglePlay}
      title={p.playing ? 'Pause (Space)' : 'Play (Space)'}
      aria-label={p.playing ? 'Pause' : 'Play'}
    >{@render icon(p.playing ? 'pause' : 'play')}</button>
    <button type="button" class="t-btn" onclick={nextTrack} title="Next (N)" aria-label="Next track">{@render icon('next')}</button>
  </div>
{/snippet}

{#snippet modeButtons(small: boolean)}
  <button
    type="button"
    class="mode-btn"
    class:small
    class:on={p.shuffle}
    aria-pressed={p.shuffle}
    onclick={() => setShuffle(!p.shuffle)}
    title="Shuffle (Shift+S)"
  >{#if small}{@render icon('shuffle')}<span class="sr-only">Shuffle {p.shuffle ? 'on' : 'off'}</span>{:else}Shuffle{/if}</button>
  <button
    type="button"
    class="mode-btn"
    class:small
    class:on={p.repeat !== 'off'}
    onclick={cycleRepeatMode}
    title="{repeatLabel(p.repeat)} (Shift+R)"
  >{#if small}{@render icon(p.repeat === 'one' ? 'repeat-one' : 'repeat')}<span class="sr-only">{repeatLabel(p.repeat)}</span>{:else}{repeatLabel(p.repeat)}{/if}</button>
{/snippet}

{#if open}
  <div
    bind:this={rootEl}
    class="music-window"
    class:minimised
    role="dialog"
    aria-modal="false"
    aria-labelledby="music-player-title"
    tabindex="-1"
    style="left: {shown.x}px; top: {shown.y}px; width: {shownWidth}px; height: {minimised ? HEADER_HEIGHT : rect.height}px"
  >
    <div
      class="mp-head"
      role="presentation"
      title={minimised ? 'Click to restore, drag to move' : 'Drag to move'}
      onpointerdown={(e) => beginGesture(e, 'move')}
      onpointermove={moveGesture}
      onpointerup={endGesture}
      onpointercancel={endGesture}
    >
      {#if minimised}
        <div class="mini">
          {@render transport(true)}
          <span id="music-player-title" class="mini-title" title={cur?.title ?? 'Music Player'}>
            {$musicReview.active ? 'Reviewing takes' : (cur?.title ?? 'Music Player')}
          </span>
          {#if $musicPosition && miniShows.position}<span class="mini-pos" title="Track k of n in Up next">{$musicPosition}</span>{/if}
          {#if miniShows.modes}{@render modeButtons(true)}{/if}
        </div>
      {:else}
        <div class="mp-title">
          <h1 id="music-player-title">Music Player</h1>
          <p class="muted">{summary}</p>
        </div>
      {/if}
      <div class="mp-buttons">
        {#if !minimised}
          <!-- In Create the player card is hidden, so the way back lives here; in Listen the
               Create button sits in the player's controls row (Mike, 2026-11-22). -->
          {#if view === 'create'}
            <button
              type="button"
              class="view-btn on"
              aria-pressed="true"
              onclick={() => (view = 'listen')}
              title="Back to listening"
            >Listen</button>
          {/if}
        {/if}
        <button
          type="button"
          class="mp-button"
          onclick={toggleMinimised}
          aria-label={minimised ? 'Restore the music player' : 'Minimise to the mini-player'}
          title={minimised ? 'Restore' : 'Minimise'}
        >{minimised ? '▢' : '–'}</button>
        <button type="button" class="mp-button" onclick={close} aria-label="Close the music player (playback goes on)" title="Close (playback goes on)">×</button>
      </div>
    </div>

    {#if !minimised}
      <div class="mp-body">
        {#if $musicLibrary.status === 'unavailable'}
          <div class="banner info" role="status">
            <span>{$musicLibrary.message}</span>
            <button type="button" class="small-btn" onclick={() => loadMusicLibrary()}>Try again</button>
          </div>
        {:else if $musicLibrary.status === 'error'}
          <div class="banner error" role="alert">
            <span>{$musicLibrary.message}</span>
            <button type="button" class="small-btn" onclick={() => loadMusicLibrary()}>Try again</button>
          </div>
        {/if}
        {#if actionError}
          <div class="banner error" role="alert">
            <span>{actionError}</span>
            <button type="button" class="small-btn" onclick={() => (actionError = '')}>Dismiss</button>
          </div>
        {/if}
        <div class="notice" role="status" aria-live="polite">
          {#if p.notice}<span>{p.notice}</span><button type="button" class="link-btn" onclick={clearNotice}>×</button>{/if}
        </div>

        {#if view === 'create'}
          <MusicCreate />
        {:else}
        <div class="mp-columns">
          <div class="col-player">
            <section class="card now" aria-label="Now playing">
              <div class="now-head">
                <div class="now-title">
                  <span class="track-title big">{cur?.title ?? 'Nothing playing'}</span>
                  {#if cur && trackSubtitle(cur)}<span class="muted small">{trackSubtitle(cur)}</span>{/if}
                </div>
                {#if cur}
                  {@render originBadge(cur)}
                  {#if curProblem}<span class="tag warn" title={curProblem.reason}>{curProblem.problem === 'missing' ? 'missing' : 'not playable yet'}</span>{/if}
                {/if}
              </div>
              {#if curProblem}<p class="muted small">{curProblem.reason}</p>{/if}
              <div class="seek-row">
                <span class="time">{formatTime(loaded ? p.currentTime : 0)}</span>
                <div class="seek-col">
                  <MusicWaveform
                    trackId={cur?.id ?? null}
                    comments={curComments}
                    position={loaded ? p.currentTime : 0}
                    duration={shownDuration}
                    seekable={loaded}
                    ariaLabel="Position in the track"
                    draft={draftRegion}
                    {regionSignal}
                    onseek={(t) => seekTo(t, 'ui')}
                    onregion={pickRegion}
                  />
                </div>
                <span class="time">{formatTime(shownDuration || null)}</span>
              </div>
              <div class="controls-row">
                {@render transport(false)}
                <div class="modes">{@render modeButtons(false)}</div>
                <button type="button" class="small-btn" onclick={() => (commentSignal += 1)} disabled={!cur} title="Comment at the current position (C)">Comment</button>
                <button type="button" class="small-btn" onclick={() => (view = 'create')} title="Make a song from your own text">Create</button>
                <label class="volume">
                  <span class="muted small">Volume</span>
                  <input type="range" min="0" max="1" step="0.05" value={p.volume} oninput={onVolume} onchange={onVolumeEnd} aria-label="Volume" />
                </label>
              </div>
              <p class="muted tiny">Space play/pause · N next · P previous · Shift+S shuffle · Shift+R repeat · ← → seek · C comment · R region</p>
            </section>

            <MusicLyrics entry={cur} currentTime={loaded ? p.currentTime : 0} duration={shownDuration} {loaded} />

            <MusicComments trackId={cur?.id ?? null} currentTime={loaded ? p.currentTime : 0} duration={shownDuration} {loaded} openSignal={commentSignal} {regionRequest} onclose={() => (draftRegion = null)} />

            <section class="card" aria-labelledby="up-next-label">
              <div class="section-head">
                <span id="up-next-label" class="section-label">UP NEXT {#if p.queue.ids.length}<span class="count">{p.queue.ids.length}</span>{/if}</span>
                <span class="muted small">shuffle {p.shuffle ? 'on' : 'off'} · repeat {p.repeat}</span>
                <button type="button" class="small-btn" onclick={clearQueue} disabled={p.queue.ids.length <= 1}>Clear</button>
              </div>
              {#if !p.queue.ids.length}
                <p class="muted small">The queue is empty. Play a list, or press Queue on a track.</p>
              {:else}
                <ol class="rows">
                  {#each p.queue.ids as id, i (i + ':' + id)}
                    {@const e = $musicEntriesById.get(id) ?? null}
                    {@const problem = problemOf(e)}
                    <li class="row" class:current={i === p.queue.index} class:unplayable={!!problem}>
                      <span class="num">{i + 1}</span>
                      <button type="button" class="row-main" onclick={() => playQueueAt(i)} disabled={!!problem} title={problem?.reason ?? 'Play now'}>
                        <span class="track-title">{e?.title ?? 'Not in the library'}</span>
                        {#if problem}<span class="tag warn">{problem.problem === 'missing' ? 'missing' : 'not playable yet'}</span>{/if}
                      </button>
                      <span class="time">{formatTime(e?.duration_s ?? null)}</span>
                      {#if i !== p.queue.index}
                        <button type="button" class="small-btn" onclick={() => removeFromQueue(i)} aria-label="Remove from Up next">Remove</button>
                      {:else}
                        <span class="muted tiny now-mark">now</span>
                      {/if}
                    </li>
                  {/each}
                </ol>
              {/if}
            </section>
          </div>

          <div class="col-library">
            <section class="card" aria-labelledby="library-label">
              <div class="section-head">
                <span id="library-label" class="section-label">LIBRARY <span class="count">{$musicLibrary.entries.length}</span></span>
                {#if $musicLibrary.status === 'loading'}<span class="muted small">Loading…</span>{/if}
              </div>
              <div class="list-bar">
                <select class="list-select" bind:value={selectedListId} aria-label="Library list" disabled={!lists.length}>
                  {#if !lists.length}<option value={SMART_ALL_ID}>All tracks</option>{/if}
                  {#each smartLists as l (l.id)}<option value={l.id}>{l.name} ({l.track_ids.length})</option>{/each}
                  {#if userLists.length}
                    <optgroup label="Playlists">
                      {#each userLists as l (l.id)}<option value={l.id}>{l.name} ({l.track_ids.length})</option>{/each}
                    </optgroup>
                  {/if}
                </select>
                <button type="button" class="small-btn" onclick={() => openPlForm('new')} disabled={$musicLibrary.status !== 'ready'}>New</button>
                <button type="button" class="small-btn" onclick={() => openPlForm('rename')} disabled={!isUserList}>Rename</button>
                <button type="button" class="small-btn danger" onclick={removeSelectedList} disabled={!isUserList}>Delete</button>
              </div>
              {#if plForm}
                <div class="pl-form">
                  <label class="small" for="music-pl-name">{plForm.mode === 'new' ? 'New playlist' : 'Rename to'}</label>
                  <input id="music-pl-name" class="text-input" type="text" bind:this={plInput} bind:value={plForm.name} onkeydown={plFormKeydown} disabled={plBusy} />
                  <button type="button" class="small-btn" onclick={submitPlForm} disabled={plBusy}>{plForm.mode === 'new' ? 'Create' : 'Save'}</button>
                  <button type="button" class="small-btn" onclick={closePlForm} disabled={plBusy}>Cancel</button>
                </div>
              {/if}
              <div class="list-bar">
                <button type="button" class="small-btn primary" onclick={playSelectedList} disabled={!selectedList?.track_ids.length}>Play this list</button>
                <input class="text-input filter" type="search" placeholder="Filter tracks" bind:value={filter} aria-label="Filter tracks" />
              </div>
              {#if hiddenList.length || missingList.length}
                <div class="list-bar">
                  {#if hiddenList.length}
                    <button type="button" class="small-btn" aria-pressed={showHidden} onclick={() => (showHidden = !showHidden)}>
                      {showHidden ? 'Hide the hidden' : 'Show hidden'} ({hiddenList.length})
                    </button>
                  {/if}
                  {#if missingList.length}
                    <button type="button" class="small-btn danger" onclick={removeAllMissing}>Remove all missing ({missingList.length})</button>
                  {/if}
                </div>
              {/if}
              {#if isUserList}
                <p class="muted tiny">{canReorder ? 'Drag rows by ⠿, use ↑ ↓, or Alt+↑ / Alt+↓ on a row, to reorder.' : 'Clear the filter to reorder.'}</p>
              {/if}

              {#if $musicLibrary.status === 'ready' && !allRows.length}
                <p class="muted small">
                  {#if selectedList?.kind === 'user'}This playlist is empty: use "Add to…" on a track.
                  {:else if selectedList?.rule === 'generated'}No generated tracks yet.
                  {:else}No tracks yet. Add a folder below.{/if}
                </p>
              {/if}

              <ol class="rows" bind:this={listEl}>
                {#each shownRows as row (row.index + ':' + row.id)}
                  {@const problem = problemOf(row.entry)}
                  <li
                    class="row"
                    class:current={row.id === currentId(p.queue)}
                    class:unplayable={!!problem}
                    class:drop-target={rowDrag && rowDrag.to === row.index && rowDrag.from !== row.index}
                    class:dragging={rowDrag?.from === row.index}
                    data-row-index={row.index}
                  >
                    {#if canReorder}
                      <button
                        type="button"
                        class="row-handle"
                        title="Drag to reorder (or Alt+↑ / Alt+↓)"
                        aria-label="Reorder {row.entry?.title ?? row.id}"
                        onpointerdown={(e) => beginRowDrag(e, row.index)}
                        onpointermove={moveRowDrag}
                        onpointerup={endRowDrag}
                        onpointercancel={cancelRowDrag}
                      >⠿</button>
                    {/if}
                    <span class="num">{row.index + 1}</span>
                    <button
                      type="button"
                      class="row-main"
                      onclick={() => playRow(row)}
                      disabled={!!problem}
                      title={problem?.reason ?? 'Play from here'}
                    >
                      <span class="track-title">{row.entry?.title ?? 'Not in the library'}</span>
                      {#if row.entry && trackSubtitle(row.entry)}<span class="muted tiny">{trackSubtitle(row.entry)}</span>{/if}
                    </button>
                    <span class="tags">
                      {#if row.entry}{@render originBadge(row.entry)}{/if}
                      {#if problem}<span class="tag warn" title={problem.reason}>{problem.problem === 'missing' ? 'missing' : 'not playable yet'}</span>{/if}
                    </span>
                    <span class="time">{formatTime(row.entry?.duration_s ?? null)}</span>
                    <span class="row-actions">
                      <button type="button" class="small-btn" onclick={() => queueTrack(row.id)} disabled={!row.entry}>Queue</button>
                      {#if canReorder}
                        <button type="button" class="small-btn icon" onclick={() => moveRow(row.index, row.index - 1)} disabled={row.index === 0} aria-label="Move up">↑</button>
                        <button type="button" class="small-btn icon" onclick={() => moveRow(row.index, row.index + 1)} disabled={row.index === allRows.length - 1} aria-label="Move down">↓</button>
                      {/if}
                      {#if userLists.length && row.entry}
                        <select class="add-to" onchange={(e) => addToList(row.id, e)} aria-label="Add {row.entry.title} to a playlist">
                          <option value="">Add to…</option>
                          {#each userLists as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
                        </select>
                      {/if}
                      {#if isUserList}
                        <button type="button" class="small-btn" onclick={() => removeRow(row.index)}>Remove</button>
                      {/if}
                      {#if row.entry}
                        <button type="button" class="small-btn icon" onclick={() => (menuId = menuId === row.id ? null : row.id)}
                          aria-label="More actions for {row.entry.title}" aria-expanded={menuId === row.id} title="More">⋯</button>
                        {#if menuId === row.id}
                          <button type="button" class="small-btn" onclick={() => hideRow(row.entry!)}>Hide</button>
                          {#if row.entry.missing}
                            <button type="button" class="small-btn danger" onclick={() => removeMissingRow(row.entry!)}>Remove from library</button>
                          {/if}
                          {#if canDeleteFile(row.entry)}
                            <button type="button" class="small-btn danger" onclick={() => deleteFileRow(row.entry!)}>Delete file…</button>
                          {/if}
                        {/if}
                      {/if}
                    </span>
                  </li>
                {/each}
              </ol>
              {#if filteredRows.length > shownRows.length}
                <button type="button" class="small-btn more" onclick={() => (rowLimit += TRACK_ROWS_STEP)}>
                  Show more ({filteredRows.length - shownRows.length} left)
                </button>
              {/if}
              {#if showHidden && hiddenList.length}
                <p class="muted tiny hidden-head">Hidden ({hiddenList.length}): not in any list, never queued.</p>
                <ol class="rows">
                  {#each hiddenList as entry (entry.id)}
                    <li class="row unplayable">
                      <span class="row-main static">
                        <span class="track-title">{entry.title}</span>
                        {#if trackSubtitle(entry)}<span class="muted tiny">{trackSubtitle(entry)}</span>{/if}
                      </span>
                      <span class="tags">
                        {@render originBadge(entry)}
                        {#if entry.missing}<span class="tag warn">missing</span>{/if}
                      </span>
                      <span class="row-actions">
                        <button type="button" class="small-btn" onclick={() => unhideRow(entry)}>Unhide</button>
                        {#if entry.missing}
                          <button type="button" class="small-btn danger" onclick={() => removeMissingRow(entry)}>Remove from library</button>
                        {/if}
                        {#if canDeleteFile(entry)}
                          <button type="button" class="small-btn danger" onclick={() => deleteFileRow(entry)}>Delete file…</button>
                        {/if}
                      </span>
                    </li>
                  {/each}
                </ol>
              {/if}
            </section>

            <section class="card" aria-labelledby="folders-label">
              <div class="section-head">
                <span id="folders-label" class="section-label">FOLDERS <span class="count">{$musicLibrary.folders.length}</span></span>
                <button type="button" class="small-btn" onclick={() => rescan()} disabled={$musicLibrary.status !== 'ready' || !$musicLibrary.folders.length || !!folderBusy}>
                  {folderBusy === 'all' ? 'Rescanning…' : 'Rescan all'}
                </button>
                <button type="button" class="small-btn primary" onclick={pickFolder} disabled={$musicLibrary.status !== 'ready' || !!folderBusy}>
                  {folderBusy === 'add' ? 'Adding…' : 'Add folder'}
                </button>
              </div>
              {#if folderPathForm !== null}
                <div class="pl-form">
                  <label class="small" for="music-folder-path">Folder path</label>
                  <input
                    id="music-folder-path"
                    class="text-input"
                    type="text"
                    bind:value={folderPathForm}
                    onkeydown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); addPickedFolder(folderPathForm ?? ''); }
                      else if (e.key === 'Escape') { e.preventDefault(); folderPathForm = null; }
                    }}
                  />
                  <button type="button" class="small-btn" onclick={() => addPickedFolder(folderPathForm ?? '')}>Add</button>
                  <button type="button" class="small-btn" onclick={() => (folderPathForm = null)}>Cancel</button>
                </div>
              {/if}
              {#if $musicLibrary.status === 'ready' && !$musicLibrary.folders.length}
                <p class="muted small">No watched folders. "Add folder" watches one: new, changed and removed files are picked up by themselves.</p>
              {/if}
              <ul class="rows">
                {#each $musicLibrary.folders as f (f.id)}
                  <li class="row folder" class:unplayable={f.watch_state === 'unavailable'}>
                    <span class="folder-text">
                      <span class="folder-path" title={f.path}>{f.path}</span>
                      <span class="muted tiny" title={f.state_reason ?? ''}>{folderState(f)}{#if f.last_scan_at} · scanned {new Date(f.last_scan_at).toLocaleString()}{/if}</span>
                    </span>
                    <span class="row-actions">
                      <button type="button" class="small-btn" onclick={() => rescan(f.id)} disabled={!!folderBusy}>{folderBusy === f.id ? 'Rescanning…' : 'Rescan'}</button>
                      <button type="button" class="small-btn danger" onclick={() => unwatch(f.id, f.path)} disabled={!!folderBusy}>Remove</button>
                    </span>
                  </li>
                {/each}
              </ul>
              <p class="muted tiny">Tracks in any added folder play. On Windows and macOS files under ~/.dpc are read directly and files elsewhere play through DPC's local track stream (127.0.0.1 only); on Linux every track plays through that stream. If the stream is unavailable, files outside ~/.dpc show "not playable yet".</p>
            </section>
          </div>
        </div>
        {/if}
      </div>
      <div
        class="resize-handle"
        role="presentation"
        title="Drag to resize"
        onpointerdown={(e) => beginGesture(e, 'resize')}
        onpointermove={moveGesture}
        onpointerup={endGesture}
        onpointercancel={endGesture}
      ></div>
    {:else}
      <!-- Outside .mp-head, so its press never reaches the bar's move / restore gesture. -->
      <div
        class="mini-resize-handle"
        role="presentation"
        title="Drag to resize the mini-player"
        onpointerdown={(e) => beginGesture(e, 'mini-resize')}
        onpointermove={moveGesture}
        onpointerup={endGesture}
        onpointercancel={endGesture}
      ></div>
    {/if}
  </div>
{/if}

<style>
  .music-window {
    position: fixed;
    /* Under the Agent map (1000, modal with its backdrop at 999): this window is not modal. */
    z-index: 998;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg-primary, #15171a);
    color: var(--text-primary, #e8eaed);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    outline: none;
  }
  .music-window:focus-visible { outline: 2px solid #58a6ff; outline-offset: -2px; }

  .mp-head {
    flex: 0 0 auto;
    box-sizing: border-box;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0 8px 0 16px;
    border-bottom: 1px solid var(--border-color, #2c3036);
    cursor: move;
    user-select: none;
    -webkit-user-select: none;
    touch-action: none;
  }
  .music-window.minimised .mp-head { border-bottom: 0; cursor: pointer; padding-left: 8px; }

  .mp-title { display: flex; align-items: baseline; gap: 14px; min-width: 0; }
  .mp-title h1 { margin: 0; font-size: 18px; font-weight: 600; white-space: nowrap; }
  .mp-title p, .mini-title {
    margin: 0;
    font-size: 13px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mini { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 auto; }
  .mini-title { font-size: 14px; flex: 1 1 auto; }
  .mini-pos { font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; white-space: nowrap; }

  .mp-buttons { display: flex; gap: 2px; flex: 0 0 auto; align-items: center; }
  .view-btn {
    margin-right: 8px;
    padding: 6px 14px;
    border-radius: 8px;
    border: 1px solid #58a6ff;
    background: transparent;
    color: #58a6ff;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
    white-space: nowrap;
  }
  .view-btn:hover { background: #1d2a3d; }
  .view-btn.on { background: #1d2a3d; }
  .view-btn:focus-visible, .mp-button:focus-visible { outline: 2px solid #58a6ff; outline-offset: 1px; }
  .mp-button {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: #c9d1d9;
    font-size: 22px;
    cursor: pointer;
  }
  .mp-button:hover { color: #ffffff; background: var(--bg-hover, #22262c); }

  .mp-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    box-sizing: border-box;
    padding: 12px 16px 24px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .resize-handle {
    position: absolute;
    right: 0;
    bottom: 0;
    width: 18px;
    height: 18px;
    cursor: nwse-resize;
    touch-action: none;
    background: linear-gradient(135deg, transparent 0 50%, #6e7681 50% 56%, transparent 56% 70%, #6e7681 70% 76%, transparent 76%);
    border-bottom-right-radius: 10px;
  }

  /* The mini-player's right edge: width only. Inside the head's 8 px right padding,
     so it never covers the close button. */
  .mini-resize-handle {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: 6px;
    cursor: ew-resize;
    touch-action: none;
  }
  .mini-resize-handle:hover { background: rgba(110, 118, 129, 0.35); }

  .banner {
    display: flex;
    align-items: center;
    gap: 12px;
    justify-content: space-between;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 14px;
  }
  .banner.info { background: #1d2a3d; color: #b6d4ff; }
  .banner.error { background: #3d1d1d; color: #f5a5a5; }
  .notice { min-height: 20px; display: flex; align-items: center; gap: 8px; font-size: 13px; color: #c9d1d9; }

  .mp-columns { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
  .col-player { flex: 1 1 360px; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
  .col-library { flex: 1.3 1 420px; min-width: 0; display: flex; flex-direction: column; gap: 12px; }

  .card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
  }

  .now-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .now-title { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
  .track-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .track-title.big { font-size: 18px; font-weight: 600; }

  .seek-row { display: flex; align-items: center; gap: 10px; }
  .seek-col { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }
  .time { font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; white-space: nowrap; }

  .controls-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .modes { display: flex; gap: 6px; }
  .volume { display: flex; align-items: center; gap: 6px; margin-left: auto; }
  .volume input { width: 90px; }

  .transport { display: flex; align-items: center; gap: 6px; }
  .t-btn {
    width: 38px;
    height: 38px;
    border-radius: 50%;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: #e8eaed;
    font-size: 15px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .t-btn:hover { background: #2c3036; }
  .t-btn.play { width: 44px; height: 44px; background: #58a6ff; border-color: #58a6ff; color: #0d1117; }
  .transport.small { flex: 0 0 auto; }
  .transport.small .t-btn { width: 32px; height: 32px; font-size: 13px; }
  .transport.small .t-btn.play { width: 36px; height: 36px; }

  .mode-btn {
    padding: 6px 10px;
    border-radius: 8px;
    border: 1px solid #3a3f47;
    background: transparent;
    color: #c9d1d9;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
    white-space: nowrap;
  }
  .mode-btn.small { padding: 4px 6px; font-size: 14px; }
  .mode-btn.on { border-color: #58a6ff; color: #58a6ff; background: #1d2a3d; }

  .section-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .section-head > .section-label { margin-right: auto; }
  .section-label { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; color: var(--text-secondary, #a9b0bb); }
  .count { margin-left: 4px; color: #c9d1d9; }

  .list-bar, .pl-form { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .list-select, .add-to, .text-input {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-primary, #14161a);
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 13px;
  }
  /* WebKitGTK (Linux) paints its light GTK menulist over the author background
     unless native appearance is dropped, leaving light text on a near-white box.
     background-color, not the shorthand, so the arrow image survives. */
  .list-select, .add-to {
    color-scheme: dark;
    -webkit-appearance: none;
    appearance: none;
    background-color: var(--bg-primary, #14161a);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23aaa'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 24px;
  }
  .list-select { flex: 1 1 180px; min-width: 0; }
  .text-input { flex: 1 1 160px; min-width: 0; }
  .add-to { max-width: 130px; }

  .small-btn {
    padding: 3px 10px;
    border-radius: 6px;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: #c9d1d9;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }
  .small-btn:hover:not(:disabled) { background: #2c3036; color: #ffffff; }
  .small-btn:disabled { opacity: 0.5; cursor: default; }
  .small-btn.primary { border-color: #58a6ff; color: #58a6ff; }
  .small-btn.danger { border-color: #8b3a3a; color: #f5a5a5; }
  .small-btn.icon { padding: 3px 7px; }
  .small-btn.more { align-self: center; }
  .link-btn { border: 0; background: transparent; color: var(--text-secondary, #a9b0bb); cursor: pointer; font-size: 14px; }

  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 8px;
    background: #22262c;
    border: 1px solid transparent;
    font-size: 13px;
    min-width: 0;
  }
  .row.current { border-color: #58a6ff; background: #232a35; }
  .row.unplayable .track-title { color: var(--text-secondary, #a9b0bb); }
  .row.drop-target { border-top: 2px solid #58a6ff; }
  .row.dragging { opacity: 0.55; }
  .num { width: 26px; flex: 0 0 26px; text-align: right; font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; }
  .row-main {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
    padding: 0;
  }
  .row-main:disabled { cursor: default; }
  .row-main > .track-title { max-width: 100%; }
  .row-handle {
    flex: 0 0 auto;
    border: 0;
    background: transparent;
    color: var(--text-secondary, #a9b0bb);
    cursor: grab;
    font-size: 14px;
    padding: 0 2px;
    touch-action: none;
  }
  .row-handle:active { cursor: grabbing; }
  .tags { display: flex; gap: 4px; flex: 0 0 auto; }
  .row-actions { display: flex; gap: 4px; align-items: center; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
  .now-mark { width: 52px; text-align: center; }

  .tag { font-size: 11px; padding: 1px 6px; border-radius: 5px; background: #31353d; color: #c9d1d9; white-space: nowrap; }
  .tag.warn { background: #3d3016; color: #f0c674; }
  button.tag.link { border: 0; font: inherit; font-size: 11px; cursor: pointer; }
  button.tag.link:hover, button.tag.link:focus-visible { background: #3d4a63; color: #ffffff; text-decoration: underline; }
  .hidden-head { margin: 8px 0 4px; }
  .row-main.static { cursor: default; }

  .folder { align-items: flex-start; }
  .folder-text { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .folder-path { font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }

  .muted { color: var(--text-secondary, #a9b0bb); }
  .small { font-size: 13px; }
  .tiny { font-size: 12px; }
  p.muted { margin: 0; }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
