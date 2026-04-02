// AG Grid cell renderer for the File column: minimal audio player (play/pause + seek bar).
// Uses _audio_url from row data; shows the file path as a tooltip on the container.
function(params) {
    const url = params.data._audio_url;
    const filePath = params.value || '';
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
    btn.onclick = () => {
        if (audio.paused) {
            if (window._examineActiveAudio && window._examineActiveAudio !== audio) {
                window._examineActiveAudio.pause();
                window._examineActiveBtn.textContent = '▶';
            }
            const playCol = document.getElementById('examine-audio');
            if (playCol && !playCol.paused) { playCol.pause(); clearTimeout(window._examineStopTimer); }
            window._examineActiveAudio = audio;
            window._examineActiveBtn = btn;
            audio.play();
            btn.textContent = '⏸';
        } else {
            audio.pause();
            btn.textContent = '▶';
        }
    };
    audio.onended = () => { btn.textContent = '▶'; };

    const seek = document.createElement('input');
    seek.type = 'range';
    const startTime = parseFloat(params.data._play_start) || 0;

    const rewind = document.createElement('button');
    rewind.textContent = '⏮';
    rewind.title = 'Jump to detection start';
    rewind.style = 'flex-shrink:0;font-size:10px;padding:0 3px;line-height:1.4';
    rewind.onclick = () => {
        audio.currentTime = startTime;
        if (audio.duration) seek.value = startTime / audio.duration;
    };
    seek.min = 0; seek.max = 1; seek.step = 0.001; seek.value = params.data._start_fraction || 0;
    seek.style = 'flex:1;min-width:0;height:4px;cursor:pointer';
    seek.oninput = () => {
        if (audio.duration) audio.currentTime = seek.value * audio.duration;
    };

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
