/* global JsSIP */
const { username, password } = JSON.parse(document.querySelector('#sip-config').textContent);
const websocketURL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const callButton = document.querySelector('#call');
const hangupButton = document.querySelector('#hangup');
const folderInput = document.querySelector('#audio-folder');
const batchButton = document.querySelector('#start-batch');
const stopBatchButton = document.querySelector('#stop-batch');
let controller;
let files = [];
const reports = [];
const reportButton = document.querySelector('#download-report');

function status(title, detail, state = '') {
  document.querySelector('#status').textContent = title;
  document.querySelector('#detail').textContent = detail;
  document.querySelector('#dot').dataset.state = state;
}

function setBusy(busy, batch = false) {
  callButton.disabled = busy;
  hangupButton.disabled = !busy || batch;
  folderInput.disabled = busy;
  batchButton.disabled = busy || files.length === 0;
  stopBatchButton.disabled = !busy || !batch;
  reportButton.disabled = busy || reports.length === 0;
}

async function callRequest(path, options = {}) {
  const response = await fetch(`/api/experiment/calls${path}`, {
    ...options,
    signal: options.signal || AbortSignal.timeout(5000),
    headers: { 'Content-Type': 'application/json' },
  });
  if (!response.ok) throw new Error(`Could not save or retrieve call data (HTTP ${response.status}).`);
  return response.json();
}

function resultText(report) {
  const summary = report?.summary;
  if (!summary) return 'Call ended; results unavailable';
  if (summary.stream_status === 'failed') return 'Audio processing failed';
  if (summary.stream_status !== 'completed') return 'Call ended; processing pending';
  if (!summary.classified_chunks) return 'Inconclusive: no classifiable audio';
  const margin = summary.decision_margin;
  return `${summary.prediction} · margin ${margin.toFixed(3)} · ${summary.classified_chunks} chunks`;
}

function updateFile(entry, message, state = '') {
  entry.row.textContent = `${entry.file.webkitRelativePath || entry.file.name} — ${message}`;
  entry.row.dataset.state = state;
}

async function placeCall(stream, signal, playback = null) {
  let ua;
  let session;
  let source;
  let connectTimeout;
  let playbackTimeout;
  let drainTimeout;
  let onAbort;
  let callId;
  let callError;
  let peerConnection;
  let statsTimer;
  let statsWork = Promise.resolve();
  let collectingStats = false;
  let sampleIndex = 0;
  let rtpError = null;
  let clockOffset = null;
  let clockSyncRtt = null;
  const report = { source_filename: playback?.filename || 'microphone', attempt: playback?.attempt || 1 };
  const collectStats = () => {
    if (collectingStats || !peerConnection || peerConnection.connectionState === 'closed') return statsWork;
    collectingStats = true;
    statsWork = (async () => {
      const stats = await peerConnection.getStats();
      for (const outbound of stats.values()) {
        if (outbound.type !== 'outbound-rtp' || (outbound.kind || outbound.mediaType) !== 'audio') continue;
        const remote = stats.get(outbound.remoteId) || Array.from(stats.values()).find(
          item => item.type === 'remote-inbound-rtp' && item.localId === outbound.id
        );
        const codec = stats.get(outbound.codecId);
        await callRequest(`/${callId}/rtp`, {
          method: 'POST',
          body: JSON.stringify({
            stream_id: outbound.id,
            sample_index: sampleIndex++,
            timestamp_ms: outbound.timestamp,
            packets_sent: outbound.packetsSent,
            bytes_sent: outbound.bytesSent,
            packets_lost: remote?.packetsLost ?? null,
            jitter_ms: remote?.jitter == null ? null : remote.jitter * 1000,
            round_trip_time_ms: remote?.roundTripTime == null ? null : remote.roundTripTime * 1000,
            server_timestamp_ms: clockOffset == null ? null : outbound.timestamp + clockOffset,
            remote_timestamp_ms: remote?.timestamp ?? null,
            remote_server_timestamp_ms: clockOffset == null || remote?.timestamp == null ? null : remote.timestamp + clockOffset,
            clock_sync_rtt_ms: clockSyncRtt,
            packets_received: remote?.packetsReceived ?? null,
            reported_fraction_lost: remote?.fractionLost == null ? null : Math.max(0, remote.fractionLost),
            codec_mime_type: codec?.mimeType ?? null,
            codec_clock_rate: codec?.clockRate ?? null,
            codec_channels: codec?.channels ?? null,
          }),
        });
      }
    })().catch(error => { rtpError = error.message; }).finally(() => { collectingStats = false; });
    return statsWork;
  };
  try {
    signal.throwIfAborted();
    if (!window.JsSIP) throw new Error('The calling library could not load. Refresh the page.');
    callId = (await callRequest('', {
      method: 'POST',
      body: JSON.stringify({ source_filename: report.source_filename }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
    })).call_id;
    report.call_id = callId;
    try {
      const before = performance.timeOrigin + performance.now();
      const clock = await callRequest('/clock');
      const after = performance.timeOrigin + performance.now();
      clockOffset = clock.server_timestamp_ms - (before + after) / 2;
      clockSyncRtt = after - before;
    } catch (error) {
      rtpError = `Clock synchronization failed: ${error.message}`;
    }
    signal.throwIfAborted();
    ua = new JsSIP.UA({
      sockets: [new JsSIP.WebSocketInterface(websocketURL)],
      uri: `sip:${username}@${location.hostname}`,
      password,
      register: true,
      session_timers: false,
    });
    await new Promise((resolve, reject) => {
      let settled = false;
      let confirmed = false;
      let connected = false;
      let started = false;
      let playbackFinished = false;
      const finish = error => {
        if (settled) return;
        settled = true;
        if (error) reject(error);
        else resolve();
      };
      const startAudio = () => {
        if (settled || started || !confirmed || !connected) return;
        started = true;
        clearTimeout(connectTimeout);
        collectStats();
        statsTimer = setInterval(collectStats, 1000);
        if (!playback) {
          status('Experiment connected', 'Speak normally. AASIST is analyzing your audio and saving predictions.', 'active');
          return;
        }
        try {
          if (playback.context.state !== 'running') throw new Error('Audio playback was suspended. Keep this page active.');
          source = playback.context.createBufferSource();
          source.buffer = playback.buffer;
          source.connect(playback.destination);
          source.onended = () => {
            if (settled) return;
            playbackFinished = true;
            collectStats();
            // Let the final audio frames leave the browser before ending the call.
            drainTimeout = setTimeout(() => {
              try { session.terminate(); }
              catch (error) { finish(error); }
            }, 300);
          };
          source.start();
          playback.onPlaying();
          playbackTimeout = setTimeout(() => finish(new Error('Audio playback timed out.')), (playback.buffer.duration + 10) * 1000);
        } catch (error) {
          finish(error);
        }
      };
      onAbort = () => finish(new DOMException('Stopped', 'AbortError'));
      signal.addEventListener('abort', onAbort, { once: true });
      connectTimeout = setTimeout(() => finish(new Error('The call did not connect within 30 seconds.')), 30000);
      ua.on('disconnected', () => finish(new Error('The connection to Asterisk closed.')));
      ua.on('registrationFailed', () => finish(new Error('The calling account could not connect.')));
      ua.on('registered', () => {
        if (settled || session) return;
        try {
          session = ua.call(`sip:experiment@${location.hostname}`, {
            mediaStream: stream,
            extraHeaders: [`X-Experiment-Call-ID: ${callId}`],
            pcConfig: { iceServers: [] },
            eventHandlers: {
              sdp: event => {
                if (event.originator === 'local' && ['localhost', '127.0.0.1'].includes(location.hostname)) {
                  // Replace Firefox's hold address for calls made from localhost.
                  event.sdp = event.sdp.replace(/^c=IN IP4 0\.0\.0\.0$/gm, 'c=IN IP4 127.0.0.1');
                }
              },
              peerconnection: ({ peerconnection }) => {
                peerConnection = peerconnection;
                peerconnection.addEventListener('connectionstatechange', () => {
                  if (settled) return;
                  connected = peerconnection.connectionState === 'connected';
                  if (connected) startAudio();
                  else if (peerconnection.connectionState === 'failed') finish(new Error('The audio connection failed.'));
                });
              },
              confirmed: () => { confirmed = true; startAudio(); },
              failed: ({ cause }) => finish(new Error(cause || 'The call failed.')),
              ended: () => finish(playback && !playbackFinished ? new Error('The call ended before the file finished.') : null),
            },
          });
        } catch (error) {
          finish(error);
        }
      });
      ua.start();
    });
  } catch (error) {
    callError = error;
  } finally {
    signal.removeEventListener('abort', onAbort);
    clearTimeout(connectTimeout);
    clearTimeout(playbackTimeout);
    clearTimeout(drainTimeout);
    clearInterval(statsTimer);
    const finalStats = collectStats();
    if (source) {
      source.onended = null;
      try { source.stop(); } catch { /* The source may not have started. */ }
      source.disconnect();
    }
    stream.getTracks().forEach(track => track.stop());
    if (session && !session.isEnded()) {
      try { session.terminate(); } catch (error) { callError ||= error; }
    }
    await finalStats;
    if (ua) {
      // Wait for unregistering before the next file reuses this SIP account.
      await new Promise(resolve => {
        const stopped = () => {
          clearTimeout(stopTimeout);
          ua.removeListener('disconnected', stopped);
          resolve();
        };
        const stopTimeout = setTimeout(stopped, 3000);
        ua.once('disconnected', stopped);
        try { ua.stop(); } catch (error) { callError ||= error; stopped(); }
        if (!ua.isConnected()) stopped();
      });
    }
    report.call_status = signal.aborted ? 'stopped' : callError ? 'failed' : 'completed';
    report.call_error = signal.aborted && callError?.name === 'AbortError' ? null : callError?.message || null;
    report.rtp_error = rtpError;
    if (callId) {
      try {
        await callRequest(`/${callId}/result`, { method: 'POST', body: JSON.stringify(report) });
        // AudioSocket can still be processing its final chunk after SIP hangup.
        for (let attempt = 0; attempt < 30; attempt += 1) {
          report.summary = await callRequest(`/${callId}`);
          if (['completed', 'failed'].includes(report.summary.stream_status)) break;
          if (report.call_status !== 'completed' && report.summary.stream_status === 'registered') break;
          await new Promise(resolve => setTimeout(resolve, 500));
        }
        if (report.summary.stream_status === 'failed' && !callError) {
          callError = new Error(report.summary.error || 'Audio processing failed.');
          report.call_status = 'failed';
          report.call_error = callError.message;
          await callRequest(`/${callId}/result`, { method: 'POST', body: JSON.stringify(report) });
        }
      } catch (error) {
        report.storage_error = error.message;
        callError ||= error;
        report.call_status = signal.aborted ? 'stopped' : 'failed';
        report.call_error = error.message;
        try {
          await callRequest(`/${callId}/result`, { method: 'POST', body: JSON.stringify(report) });
        } catch { /* The downloadable report retains errors if the database is unavailable. */ }
      }
    }
    reports.push(report);
  }
  if (callError) throw callError;
  return report;
}

document.querySelector('#call-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (controller) return;
  controller = new AbortController();
  const { signal } = controller;
  const reportCount = reports.length;
  setBusy(true);
  status('Requesting microphone', 'Allow microphone access to start your call.');
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone access requires HTTPS or localhost.');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      video: false,
    });
    if (!signal.aborted) status('Calling experiment', 'Connecting your microphone…');
    const report = await placeCall(stream, signal);
    status('Call ended', `${resultText(report)}. ${report.storage_error || report.rtp_error || ''}`);
  } catch (error) {
    if (signal.aborted) status('Call ended', reports.length > reportCount
      ? `${resultText(reports.at(-1))}. You can download the report.`
      : 'Call stopped before connecting.');
    else status('Call failed', error.name === 'NotAllowedError' ? 'Allow microphone access and try again.' : error.message, 'error');
  } finally {
    controller = null;
    setBusy(false);
  }
});

folderInput.addEventListener('change', () => {
  const list = document.querySelector('#batch-files');
  list.replaceChildren();
  files = Array.from(folderInput.files)
    .filter(file => file.type.startsWith('audio/') || /\.(wav|flac|mp3|ogg|m4a|aac|opus|webm)$/i.test(file.name))
    .sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, undefined, { numeric: true }))
    .map(file => {
      const entry = { file, row: document.createElement('li') };
      updateFile(entry, 'Waiting');
      list.append(entry.row);
      return entry;
    });
  document.querySelector('#folder-detail').textContent = files.length
    ? `${files.length} audio file${files.length === 1 ? '' : 's'} queued in filename order, including subfolders.`
    : 'No audio files selected. Choose a folder containing audio files.';
  setBusy(false);
});

batchButton.addEventListener('click', async () => {
  if (controller || !files.length) return;
  controller = new AbortController();
  const { signal } = controller;
  let context;
  let completed = 0;
  let failed = 0;
  let currentEntry;
  setBusy(true, true);
  files.forEach(entry => updateFile(entry, 'Waiting'));
  try {
    context = new AudioContext();
    await context.resume();
    for (const [index, entry] of files.entries()) {
      currentEntry = entry;
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        signal.throwIfAborted();
        const progress = `${index + 1}/${files.length}, attempt ${attempt}/2`;
        updateFile(entry, `Opening (${progress})`, 'active');
        status('Batch running', `${entry.file.name} — ${progress}`);
        const reportCount = reports.length;
        try {
          const buffer = await context.decodeAudioData(await entry.file.arrayBuffer());
          signal.throwIfAborted();
          const destination = context.createMediaStreamDestination();
          updateFile(entry, `Calling (${progress})`, 'active');
          const report = await placeCall(destination.stream, signal, {
            context, buffer, destination, attempt,
            filename: entry.file.webkitRelativePath || entry.file.name,
            onPlaying: () => {
              updateFile(entry, `Playing (${progress})`, 'active');
              status('Playing file', `${entry.file.name} — ${progress}`, 'active');
            },
          });
          signal.throwIfAborted();
          completed += 1;
          updateFile(entry, `${resultText(report)}${report.storage_error || report.rtp_error ? ' · measurement/reporting error (see report)' : ''}`, 'complete');
          break;
        } catch (error) {
          if (reports.length === reportCount) {
            reports.push({ source_filename: entry.file.webkitRelativePath || entry.file.name,
              attempt, call_status: signal.aborted ? 'stopped' : 'failed', call_error: error.message });
          }
          if (signal.aborted) throw error;
          if (attempt === 2) {
            failed += 1;
            updateFile(entry, `Failed after retry: ${error.message}`, 'error');
          } else {
            updateFile(entry, `Retrying: ${error.message}`, 'active');
          }
        }
      }
    }
    status('Batch finished', `${completed} completed, ${failed} failed.`, failed ? 'error' : 'active');
  } catch (error) {
    if (signal.aborted) {
      if (currentEntry) updateFile(currentEntry, 'Stopped');
      status('Batch stopped', `${completed} completed, ${failed} failed.`);
    } else {
      status('Batch failed', error.message, 'error');
    }
  } finally {
    if (context && context.state !== 'closed') await context.close();
    controller = null;
    setBusy(false);
  }
});

function stop() {
  if (!controller) return;
  status('Stopping', 'Ending the current call…');
  controller.abort();
}

hangupButton.addEventListener('click', stop);
stopBatchButton.addEventListener('click', stop);
window.addEventListener('pagehide', stop);

reportButton.addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(reports, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'call-experiments.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
