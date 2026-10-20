(() => {
  const shell = document.getElementById('video-shell');
  const video = document.getElementById('video-player');
  if (!shell || !video) return;

  const controls = document.getElementById('player-controls');
  const playButton = document.getElementById('play-toggle');
  const centerPlay = document.getElementById('center-play');
  const downloadButton = document.getElementById('player-download');
  const actionDownloadButton = document.getElementById('action-download');
  const downloadMessage = document.getElementById('player-download-message');
  const playerDetails = document.getElementById('player-details');
  const muteButton = document.getElementById('mute-toggle');
  const seekArea = document.querySelector('.seek-area');
  const seekSlider = document.getElementById('seek-slider');
  const seekPlayed = document.getElementById('seek-played');
  const seekBuffered = document.getElementById('seek-buffered');
  const seekThumb = document.getElementById('seek-thumb');
  const seekTooltip = document.getElementById('seek-tooltip');
  const volumeSlider = document.getElementById('volume-slider');
  const volumeLevel = document.getElementById('volume-level');
  const timeDisplay = document.getElementById('time-display');
  const speedSelect = document.getElementById('player-speed');
  const actionSpeedSelect = document.getElementById('action-speed');
  const bufferingIndicator = document.getElementById('buffering-indicator');
  const errorOverlay = document.getElementById('player-error');
  const errorMessage = document.getElementById('player-error-message');
  const retryButton = document.getElementById('player-retry');
  const fullscreenButton = document.getElementById('fullscreen-toggle');
  const pipButton = document.getElementById('pip-toggle');
  const STORAGE_KEY = 'yt-viewer-player-v1';
  let hideTimer = 0;
  let seeking = false;
  let volumeDragging = false;
  let retryHandler = () => {};
  let downloadHandler = () => {};

  function readPreferences() {
    try {
      const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}');
      return value && typeof value === 'object' ? value : {};
    } catch (_) {
      return {};
    }
  }

  function savePreferences() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
        volume: video.volume,
        muted: video.muted,
        speed: video.playbackRate
      }));
    } catch (_) {
      // Playback remains usable when storage is unavailable.
    }
  }

  const preferences = readPreferences();
  video.volume = Number.isFinite(preferences.volume) ? Math.min(1, Math.max(0, preferences.volume)) : 1;
  video.muted = preferences.muted === true;
  const storedSpeed = Number(preferences.speed);
  video.playbackRate = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].includes(storedSpeed) ? storedSpeed : 1;

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
  }

  function duration() {
    return Number.isFinite(video.duration) ? video.duration : 0;
  }

  function updatePlayState() {
    const isPlaying = !video.paused && !video.ended;
    shell.classList.toggle('is-playing', isPlaying);
    centerPlay.hidden = isPlaying || !video.currentSrc;
    playButton.setAttribute('aria-label', isPlaying ? 'Pause' : 'Play');
    playButton.title = isPlaying ? 'Pause (Space or K)' : 'Play (Space or K)';
    centerPlay.setAttribute('aria-label', isPlaying ? 'Pause video' : 'Play video');
    if (!isPlaying) showControls();
  }

  function setBuffered() {
    const total = duration();
    if (!total || !video.buffered.length) {
      seekBuffered.style.width = '0%';
      return;
    }
    let bufferedEnd = 0;
    for (let index = 0; index < video.buffered.length; index += 1) {
      const start = video.buffered.start(index);
      const end = video.buffered.end(index);
      if (video.currentTime >= start && video.currentTime <= end) {
        bufferedEnd = end;
        break;
      }
      bufferedEnd = Math.max(bufferedEnd, end);
    }
    seekBuffered.style.width = `${Math.min(100, (bufferedEnd / total) * 100)}%`;
  }

  function updateProgress() {
    const total = duration();
    const current = Math.min(video.currentTime || 0, total);
    const percentage = total ? (current / total) * 100 : 0;
    seekPlayed.style.width = `${percentage}%`;
    seekThumb.style.left = `${percentage}%`;
    seekSlider.setAttribute('aria-valuemax', String(Math.floor(total)));
    seekSlider.setAttribute('aria-valuenow', String(Math.floor(current)));
    seekSlider.setAttribute('aria-valuetext', `${formatTime(current)} of ${formatTime(total)}`);
    timeDisplay.textContent = `${formatTime(current)} / ${formatTime(total)}`;
    setBuffered();
  }

  function updateVolume() {
    const value = video.muted ? 0 : video.volume;
    volumeLevel.style.width = `${value * 100}%`;
    volumeSlider.setAttribute('aria-valuenow', String(Math.round(value * 100)));
    volumeSlider.setAttribute('aria-valuetext', `${Math.round(value * 100)}%`);
    muteButton.setAttribute('aria-label', video.muted || video.volume === 0 ? 'Unmute' : 'Mute');
    muteButton.title = video.muted || video.volume === 0 ? 'Unmute (M)' : 'Mute (M)';
    shell.classList.toggle('is-muted', video.muted || video.volume === 0);
  }

  function setSpeed(value) {
    const speed = Number(value);
    if (![0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].includes(speed)) return;
    video.playbackRate = speed;
    speedSelect.value = String(speed);
    actionSpeedSelect.value = String(speed);
    savePreferences();
  }

  function showError(message) {
    errorMessage.textContent = message || 'Video could not be loaded.';
    errorOverlay.hidden = false;
    bufferingIndicator.hidden = true;
    shell.classList.remove('controls-hidden');
    window.clearTimeout(hideTimer);
  }

  function clearError() {
    errorOverlay.hidden = true;
  }

  function showControls() {
    shell.classList.remove('controls-hidden');
    window.clearTimeout(hideTimer);
    if (!video.paused && !video.ended) {
      hideTimer = window.setTimeout(() => shell.classList.add('controls-hidden'), 2500);
    }
  }

  function positionAt(event, move = true) {
    const rect = seekSlider.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const time = fraction * duration();
    if (move && duration()) video.currentTime = time;
    seekTooltip.textContent = formatTime(time);
    seekTooltip.style.left = `${fraction * 100}%`;
    return time;
  }

  function setVolumeFromPointer(event) {
    const rect = volumeSlider.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    video.volume = fraction;
    video.muted = fraction === 0;
    updateVolume();
    savePreferences();
  }

  function togglePlayback() {
    if (!video.currentSrc) return;
    if (video.paused) {
      video.play().catch((error) => showError(error.message || 'Could not play this video.'));
    } else {
      video.pause();
    }
  }

  function toggleFullscreen() {
    if (document.fullscreenElement === shell) {
      document.exitFullscreen?.().catch(() => {});
    } else if (shell.requestFullscreen) {
      shell.requestFullscreen().catch((error) => showError(error.message || 'Fullscreen is unavailable.'));
    }
  }

  function updateFullscreen() {
    const fullscreen = document.fullscreenElement === shell;
    fullscreenButton.setAttribute('aria-label', fullscreen ? 'Exit fullscreen' : 'Enter fullscreen');
    fullscreenButton.title = fullscreen ? 'Exit fullscreen (F)' : 'Fullscreen (F)';
    shell.classList.toggle('is-fullscreen', fullscreen);
  }

  function togglePictureInPicture() {
    if (!document.pictureInPictureEnabled || !video.currentSrc) return;
    const action = document.pictureInPictureElement
      ? document.exitPictureInPicture()
      : video.requestPictureInPicture();
    action.catch((error) => showError(error.message || 'Picture-in-picture is unavailable.'));
  }

  function onSeekPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) return;
    seeking = true;
    seekArea.classList.add('is-dragging');
    seekSlider.focus();
    seekSlider.setPointerCapture?.(event.pointerId);
    positionAt(event);
    event.preventDefault();
  }

  seekArea.addEventListener('pointerdown', onSeekPointerDown);
  seekArea.addEventListener('pointermove', (event) => {
    positionAt(event, seeking);
  });
  seekArea.addEventListener('pointerup', (event) => {
    if (!seeking) return;
    positionAt(event);
    seeking = false;
    seekArea.classList.remove('is-dragging');
  });
  seekArea.addEventListener('pointercancel', () => {
    seeking = false;
    seekArea.classList.remove('is-dragging');
  });
  seekArea.addEventListener('pointerleave', () => {
    if (!seeking) seekTooltip.style.opacity = '';
  });

  seekSlider.addEventListener('keydown', (event) => {
    const steps = { ArrowLeft: -5, ArrowRight: 5, PageDown: -10, PageUp: 10 };
    if (Object.hasOwn(steps, event.key)) {
      video.currentTime = Math.min(duration(), Math.max(0, video.currentTime + steps[event.key]));
      event.preventDefault();
    } else if (event.key === 'Home') {
      video.currentTime = 0;
      event.preventDefault();
    } else if (event.key === 'End') {
      video.currentTime = duration();
      event.preventDefault();
    }
  });

  volumeSlider.addEventListener('pointerdown', (event) => {
    volumeDragging = true;
    volumeSlider.setPointerCapture?.(event.pointerId);
    setVolumeFromPointer(event);
    event.preventDefault();
  });
  volumeSlider.addEventListener('pointermove', (event) => {
    if (volumeDragging) setVolumeFromPointer(event);
  });
  volumeSlider.addEventListener('pointerup', () => { volumeDragging = false; });
  volumeSlider.addEventListener('pointercancel', () => { volumeDragging = false; });
  volumeSlider.addEventListener('keydown', (event) => {
    const increments = { ArrowDown: -0.05, ArrowLeft: -0.05, ArrowUp: 0.05, ArrowRight: 0.05 };
    if (!Object.hasOwn(increments, event.key)) return;
    video.volume = Math.min(1, Math.max(0, video.volume + increments[event.key]));
    if (video.volume > 0) video.muted = false;
    updateVolume();
    savePreferences();
    event.preventDefault();
  });

  playButton.addEventListener('click', togglePlayback);
  centerPlay.addEventListener('click', togglePlayback);
  muteButton.addEventListener('click', () => {
    video.muted = !video.muted;
    updateVolume();
    savePreferences();
  });
  document.getElementById('skip-back').addEventListener('click', () => {
    video.currentTime = Math.max(0, video.currentTime - 10);
  });
  document.getElementById('skip-forward').addEventListener('click', () => {
    video.currentTime = Math.min(duration(), video.currentTime + 10);
  });
  speedSelect.addEventListener('change', () => setSpeed(speedSelect.value));
  actionSpeedSelect.addEventListener('change', () => setSpeed(actionSpeedSelect.value));
  fullscreenButton.addEventListener('click', toggleFullscreen);
  pipButton.addEventListener('click', togglePictureInPicture);
  retryButton.addEventListener('click', () => {
    clearError();
    retryHandler();
  });
  downloadButton.addEventListener('click', () => downloadHandler());
  actionDownloadButton.addEventListener('click', () => downloadHandler());

  video.addEventListener('click', () => {
    shell.focus({ preventScroll: true });
    togglePlayback();
  });
  video.addEventListener('dblclick', toggleFullscreen);
  shell.addEventListener('pointermove', showControls);
  shell.addEventListener('pointerdown', showControls);
  shell.addEventListener('touchstart', showControls, { passive: true });

  shell.addEventListener('keydown', (event) => {
    if (event.defaultPrevented) return;
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (event.target.closest('button') && event.key === ' ') return;
    const key = event.key.toLowerCase();
    if (key === ' ' || key === 'k') {
      togglePlayback();
      event.preventDefault();
    } else if (key === 'arrowleft') {
      video.currentTime = Math.max(0, video.currentTime - 5);
      event.preventDefault();
    } else if (key === 'arrowright') {
      video.currentTime = Math.min(duration(), video.currentTime + 5);
      event.preventDefault();
    } else if (key === 'j') {
      video.currentTime = Math.max(0, video.currentTime - 10);
      event.preventDefault();
    } else if (key === 'l') {
      video.currentTime = Math.min(duration(), video.currentTime + 10);
      event.preventDefault();
    } else if (key === 'arrowup' || key === 'arrowdown') {
      video.volume = Math.min(1, Math.max(0, video.volume + (key === 'arrowup' ? 0.05 : -0.05)));
      if (video.volume > 0) video.muted = false;
      updateVolume();
      savePreferences();
      event.preventDefault();
    } else if (key === 'm') {
      video.muted = !video.muted;
      updateVolume();
      savePreferences();
    } else if (key === 'f') {
      toggleFullscreen();
    } else if (/^[0-9]$/.test(key) && duration()) {
      video.currentTime = (Number(key) / 10) * duration();
    }
  });

  video.addEventListener('play', () => {
    clearError();
    bufferingIndicator.hidden = false;
    updatePlayState();
    showControls();
  });
  video.addEventListener('playing', () => {
    bufferingIndicator.hidden = true;
    shell.classList.add('is-ready');
    updatePlayState();
    showControls();
  });
  video.addEventListener('pause', updatePlayState);
  video.addEventListener('ended', updatePlayState);
  video.addEventListener('waiting', () => {
    if (!video.paused) bufferingIndicator.hidden = false;
  });
  video.addEventListener('stalled', () => {
    if (!video.paused) bufferingIndicator.hidden = false;
  });
  video.addEventListener('canplay', () => {
    bufferingIndicator.hidden = true;
    shell.classList.add('is-ready');
    updatePlayState();
  });
  video.addEventListener('loadedmetadata', () => {
    shell.classList.add('is-ready');
    updateProgress();
    updatePlayState();
  });
  video.addEventListener('timeupdate', updateProgress);
  video.addEventListener('progress', setBuffered);
  video.addEventListener('durationchange', updateProgress);
  video.addEventListener('volumechange', updateVolume);
  video.addEventListener('ratechange', () => {
    speedSelect.value = String(video.playbackRate);
    actionSpeedSelect.value = String(video.playbackRate);
  });
    video.addEventListener('error', async () => {
    if (!video.currentSrc) return;
    const source = video.currentSrc;
    const code = video.error?.code;
    const messages = {
      1: 'Video loading was cancelled.',
      2: 'A network error interrupted the video.',
      3: 'The video could not be decoded.',
      4: 'This video format is not supported.'
    };
    let message = messages[code] || 'Video could not be loaded.';
    if (code === 2 || code === 4) {
      try {
        const probe = await fetch(source, { method: 'HEAD', cache: 'no-store' });
        if (!probe.ok) message = `The server could not provide the video (HTTP ${probe.status}).`;
      } catch (_) {
        message = 'Could not reach the server.';
      }
    }
    if (video.currentSrc !== source) return;
    showError(message);
  });
  document.addEventListener('fullscreenchange', updateFullscreen);
  document.addEventListener('enterpictureinpicture', () => pipButton.setAttribute('aria-label', 'Exit picture-in-picture'));
  document.addEventListener('leavepictureinpicture', () => pipButton.setAttribute('aria-label', 'Enter picture-in-picture'));

  if (!document.pictureInPictureEnabled) pipButton.disabled = true;
  updateProgress();
  updateVolume();
  setSpeed(video.playbackRate);
  updateFullscreen();
  updatePlayState();

  window.playerUI = {
    clearError,
    setRetryHandler(handler) {
      retryHandler = typeof handler === 'function' ? handler : () => {};
    },
    showError,
    setDownloadHandler(handler) {
      downloadHandler = typeof handler === 'function' ? handler : () => {};
    },
    showDownloadMessage(message) {
      downloadMessage.textContent = message;
      downloadMessage.hidden = false;
      playerDetails.open = true;
    },
    clearDownloadMessage() {
      downloadMessage.textContent = '';
      downloadMessage.hidden = true;
    },
    setMetadata(info = {}) {
      const channel = document.getElementById('video-channel');
      const meta = document.getElementById('video-meta');
      channel.textContent = info.channel || info.uploader || '';
      if (info.quality_sizes && typeof info.quality_sizes === 'object') {
        for (const qualitySelect of document.querySelectorAll('#quality, #setting-quality')) {
          for (const option of qualitySelect.options) {
            const size = info.quality_sizes[option.value];
            if (size) option.textContent = `${option.value}p (${size})`;
          }
        }
      }
      const pieces = [];
      if (Number.isFinite(Number(info.view_count)) && info.view_count !== null) {
        pieces.push(`${Number(info.view_count).toLocaleString()} views`);
      }
      if (typeof info.upload_date === 'string' && /^\d{8}$/.test(info.upload_date)) {
        const date = new Date(`${info.upload_date.slice(0, 4)}-${info.upload_date.slice(4, 6)}-${info.upload_date.slice(6, 8)}T00:00:00`);
        if (!Number.isNaN(date.valueOf())) pieces.push(date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }));
      }
      meta.textContent = pieces.join(' · ');
    }
  };
})();
