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
  try {
    signal.throwIfAborted();
    if (!window.JsSIP) throw new Error('The calling library could not load. Refresh the page.');
    let callId;
    if (playback) {
      const response = await fetch('/api/experiment/calls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_filename: playback.filename }),
        signal,
      });
      if (!response.ok) throw new Error('Could not save the source filename for this call.');
      callId = (await response.json()).call_id;
      signal.throwIfAborted();
    }
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
            extraHeaders: callId ? [`X-Experiment-Call-ID: ${callId}`] : [],
            pcConfig: { iceServers: [] },
            eventHandlers: {
              sdp: event => {
                if (event.originator === 'local' && ['localhost', '127.0.0.1'].includes(location.hostname)) {
                  // Replace Firefox's hold address for calls made from localhost.
                  event.sdp = event.sdp.replace(/^c=IN IP4 0\.0\.0\.0$/gm, 'c=IN IP4 127.0.0.1');
                }
              },
              peerconnection: ({ peerconnection }) => {
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
  } finally {
    signal.removeEventListener('abort', onAbort);
    clearTimeout(connectTimeout);
    clearTimeout(playbackTimeout);
    clearTimeout(drainTimeout);
    if (source) {
      source.onended = null;
      try { source.stop(); } catch { /* The source may not have started. */ }
      source.disconnect();
    }
    stream.getTracks().forEach(track => track.stop());
    if (session && !session.isEnded()) session.terminate();
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
        ua.stop();
        if (!ua.isConnected()) stopped();
      });
    }
  }
}

document.querySelector('#call-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (controller) return;
  controller = new AbortController();
  const { signal } = controller;
  setBusy(true);
  status('Requesting microphone', 'Allow microphone access to start your call.');
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone access requires HTTPS or localhost.');
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      video: false,
    });
    if (!signal.aborted) status('Calling experiment', 'Connecting your microphone…');
    await placeCall(stream, signal);
    status('Call ended', 'You can start another experiment.');
  } catch (error) {
    if (signal.aborted) status('Call ended', 'You can start another experiment.');
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
        try {
          const buffer = await context.decodeAudioData(await entry.file.arrayBuffer());
          signal.throwIfAborted();
          const destination = context.createMediaStreamDestination();
          updateFile(entry, `Calling (${progress})`, 'active');
          await placeCall(destination.stream, signal, {
            context, buffer, destination,
            filename: entry.file.webkitRelativePath || entry.file.name,
            onPlaying: () => {
              updateFile(entry, `Playing (${progress})`, 'active');
              status('Playing file', `${entry.file.name} — ${progress}`, 'active');
            },
          });
          signal.throwIfAborted();
          completed += 1;
          updateFile(entry, 'Call completed', 'complete');
          break;
        } catch (error) {
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
