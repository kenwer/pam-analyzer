// AG Grid cell renderer for the File column: minimal audio player (play/pause + seek bar).
// Computes audio URL and start times on the fly from grid context and row data
// to minimize JSON transfer size for large datasets.
//
// FUTURE IMPROVEMENTS:
// - For datasets with many unique files, consider computing durations
//   client-side using HTMLMediaElement.duration and caching in a Map. This would
//   eliminate the server-side file_durations payload entirely.
// - The audio playback coordination logic (stopping other players,
//   managing window._examineActiveAudio) is scattered across this renderer and
//   the _play column handler. Consider extracting it into a dedicated module
//   that manages active audio elements centrally.
function(params) {
    const audioRoot = (params.context && params.context.audio_root) || '/media/';
    const padBefore = (params.context && params.context.pad_before) || 0;
    const fileDurations = (params.context && params.context.file_durations) || {};

    const filePath = params.value || '';
    // Compute relative path client-side from the File field.
    // This avoids sending _rel_file annotations over WebSocket, reducing payload size.
    const relPath = filePath;
    const url = relPath ? audioRoot + relPath : '';

    const container = document.createElement('div');
    container.title = filePath;
    container.style = 'display:flex;align-items:center;gap:4px;width:100%;overflow:hidden';

    if (!url) {
        container.textContent = filePath;
        return container;
    }

    const audio = document.createElement('audio');
    audio.src = url;
    audio.preload = 'none';

    const btn = document.createElement('button');
    btn.textContent = '▶';
    btn.style = 'flex-shrink:0;font-size:10px;padding:0;line-height:1.4;width:1.4em;text-align:center';
    btn.onclick = (e) => {
        if (e) e.stopPropagation();
        if (audio.paused) {
            if (window._examineActiveAudio && window._examineActiveAudio !== audio) {
                try { window._examineActiveAudio.pause(); } catch(err) {}
                if (window._examineActiveBtn) window._examineActiveBtn.textContent = '▶';
            }
            // The _play column (column 1) uses a separate audio element with id='examine-audio'
            // that is not tracked by window._examineActiveAudio, so we look it up by ID.
            const playAudio = document.getElementById('examine-audio');
            if (playAudio && !playAudio.paused) {
                try { playAudio.pause(); } catch(err) {}
                clearTimeout(window._examineStopTimer);
            }

            audio.play().then(() => {
                btn.textContent = '⏸';
                window._examineActiveAudio = audio;
                window._examineActiveBtn = btn;
            }).catch(err => {
                console.error('Audio play failed:', err);
            });
        } else {
            audio.pause();
            btn.textContent = '▶';
        }
    };
    audio.onended = () => { btn.textContent = '▶'; };

    const seek = document.createElement('input');
    seek.type = 'range';
    const startTime = Math.max(0.0, (parseFloat(params.data.Start_Time) || 0) - padBefore);

    const rewind = document.createElement('button');
    rewind.textContent = '⏮';
    rewind.title = 'Jump to detection start';
    rewind.style = 'flex-shrink:0;font-size:10px;padding:0 3px;line-height:1.4';
    rewind.onclick = (e) => {
        if (e) e.stopPropagation();
        audio.currentTime = startTime;
        if (audio.duration) seek.value = audio.currentTime / audio.duration;
    };
    seek.min = 0; seek.max = 1; seek.step = 0.001;

    // Use pre-computed duration from context if available, otherwise from row or 0
    const duration = fileDurations[relPath] || params.data._duration || 0;
    seek.value = (duration > 0) ? Math.min(1.0, startTime / duration) : 0;

    seek.style = 'flex:1;min-width:0;height:4px;cursor:pointer';
    seek.oninput = (e) => {
        if (e) e.stopPropagation();
        if (audio.duration && isFinite(audio.duration)) {
            audio.currentTime = seek.value * audio.duration;
        }
    };
    seek.onmousedown = (e) => e.stopPropagation();
    seek.onclick = (e) => e.stopPropagation();

    let seeked = false;
    audio.onloadedmetadata = () => {
        if (!seeked) { seeked = true; audio.currentTime = startTime; }
        if (audio.duration) seek.value = audio.currentTime / audio.duration;
    };
    audio.ontimeupdate = () => {
        if (audio.duration) seek.value = audio.currentTime / audio.duration;
    };

    container.append(btn, rewind, seek);
    return container;
}
