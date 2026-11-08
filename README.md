# AASIST Call Experiments

A local research setup for audio spoof detection over SIP calls. Asterisk streams
call audio to a pretrained AASIST model running on CPU. PostgreSQL stores call
records and predictions. The browser supports microphone calls and batches of
audio files, with one call per file.

## Setup

You need Docker with Docker Compose, OpenSSL, and a browser with WebRTC support.

1. Copy the environment template:

   ```sh
   cp .env.example .env
   ```

   Set two different random alphanumeric passwords for `POSTGRES_PASSWORD` and
   `SIP_PASSWORD`. Set `LAN_IP` to the Docker host's local network IPv4 address.

2. In `web/index.html`, replace `replace_with_SIP_PASSWORD` in the `sip-config`
   script with the same `SIP_PASSWORD`. The browser uses account `1002`.
   This static configuration is visible to anyone who can access the page;
   use this setup on a trusted research network. Restore the placeholder before
   committing or publishing changes.

3. Create a local certificate, replacing `192.168.1.50` with your `LAN_IP`:

   ```sh
   mkdir -p docker/tls
   openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
     -keyout docker/tls/server.key \
     -out docker/tls/server.crt \
     -subj '/CN=localhost' \
     -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.50'
   ```

   Trust the certificate in your browser or operating system for local HTTPS.
   `.env` and generated certificates are excluded by `.gitignore`.

4. Build and start the services:

   ```sh
   docker compose up --build -d
   docker compose ps
   docker compose logs aasist asterisk
   ```

   The first build downloads Asterisk and the pinned AASIST source and weights.
   Asterisk starts after PostgreSQL and the model service are healthy.

## Run an experiment

Open `https://localhost:8443`, or `https://<LAN_IP>:8443` from another device on
the same network. Port `8080` redirects to HTTPS. The page connects to Asterisk
through the web service's `/ws` proxy. No STUN or TURN service is configured.

- **Microphone:** click **Call experiment**, allow microphone access, and speak
  normally. Click **Hang up** to finish. Audio is analyzed without echo playback.
- **Audio files:** select a folder and click **Start batch**. Files, including
  those in subfolders, run in filename order. Each file is decoded by the browser
  and streamed into its own call. Failed files receive one retry. Format support
  depends on the browser. Click **Stop batch** to stop processing.
- **Softphone:** register account `1001` over UDP at `<LAN_IP>:5060` using
  `SIP_PASSWORD`, enable G.711 (PCMU/PCMA), and call `experiment`.

## Predictions and call records

Asterisk sends mono 8 kHz PCM16 audio over AudioSocket. The inference service
processes chunks of approximately 4.04 seconds, resamples them to 16 kHz, and
uses the model's 64,600-sample input length. Short inputs are repeated to fill
that length. Remaining audio is processed when the call ends.

Chunks must contain at least 0.3 seconds of audio at or above the -50 dBFS energy
threshold, measured in 20 ms frames. Silent and very quiet chunks are skipped.
This energy check does not identify speech.

Predictions are `spoof` or `bonafide`. The saved score is the raw bonafide logit
(output column 1), not a probability or calibrated confidence value.

Each call also stores a duration-weighted average score and decision margin
(bonafide logit minus spoof logit). Positive margins favor bonafide; negative
margins favor spoof. Calls with no classified audio or a zero margin remain
inconclusive. This aggregation is a research baseline, not calibrated confidence.

Browser calls collect RTP statistics approximately once per second. The existing
AASIST service stores and summarizes them in the same PostgreSQL database:

| Table | Stored data |
| --- | --- |
| `experiment_calls` | Call outcome, weighted prediction, overall RTP summary, and codec history. |
| `experiment_chunks` | Audio offsets, duration, classified/skipped status, and chunk RTP summary. |
| `experiment_predictions` | Individual chunk labels, raw scores, and decision margins. |
| `experiment_rtp_samples` | Raw packet counters, negotiated codec, jitter, RTT, and timing information. |

All experiment tables share `call_id`. Call summaries include packet-loss
percentage, worst chunk loss, and mean, 95th percentile, and maximum reported
jitter and RTT. Repeated snapshots of the same receiver report are counted once.
Missing receiver measurements remain unknown rather than zero.

Loss percentages use packet-counter changes. If the browser does not expose
receiver packet totals, `loss_counter_basis` is `sender_estimate`. The last
receiver report may precede hangup; `loss_observed_until_ms` records that coverage.
Chunk alignment uses synchronized browser timestamps and server audio offsets.
It is approximate, and loss from reports spanning multiple chunks is allocated
by time overlap. These estimates are labeled in the stored summaries. Jitter
and RTT statistics describe unique receiver reports, not individual packets.

Metrics collection currently covers browser calls. The configured codecs are
G.711 PCMU/PCMA; Opus experiments require separate codec configuration. Predictions
only store results and never flag or terminate a call. Dynamic transition
experiments are outside this setup.

After a batch or microphone call, **Download call report** exports the call
outcomes, chunk results, and network summaries as JSON. Failed files are retried
once; files that cannot be decoded appear in the report without a call UUID
because no call was placed.

Open `http://localhost:8000/admin/` to browse predictions and call records.
The admin interface has no authentication and is bound to localhost by default.
Predictions are stored in `experiment_predictions`; batch source filenames are
registered in `experiment_calls`. The prediction `call_id` matches the Asterisk
`cdr.userfield` value. Call records are written after hangup.

```sh
docker compose exec postgres psql -U asterisk -d asterisk -c \
  'SELECT call_id, chunk_index, source_filename, prediction, score FROM experiment_predictions ORDER BY id DESC LIMIT 10;'
docker compose exec postgres psql -U asterisk -d asterisk -c \
  'SELECT id, src, dst, disposition, billsec, userfield FROM cdr ORDER BY id DESC LIMIT 10;'
```

## HTTP API

The inference API is available locally at `http://localhost:8000`, with
interactive documentation at `/docs`.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Returns model availability; HTTP 503 if the model is unavailable. |
| `POST /predict` | Accepts a WAV or FLAC multipart upload in the `file` field. |
| `POST /experiment/calls` | Registers a `source_filename` and returns a call UUID. |
| `GET /experiment/calls/clock` | Provides server time for approximate browser clock synchronization. |
| `GET /experiment/calls/{call_id}` | Returns the stored call summary and chunk results. |
| `POST /experiment/calls/{call_id}/rtp` | Stores an RTP statistics snapshot and updates network summaries. |
| `POST /experiment/calls/{call_id}/result` | Stores the browser call outcome and collection errors. |

```sh
curl -F 'file=@sample.wav' http://localhost:8000/predict
```

Uploads are mixed to mono and resampled as needed. Only the first 64,600 samples
at 16 kHz are classified; shorter uploads are repeated. Insufficient audible
audio returns HTTP 422.

## Services and maintenance

| Service | Host ports |
| --- | --- |
| Web | `8080` (HTTP redirect), `8443` (HTTPS) |
| Asterisk | `5060/udp` (SIP), `10000–10100/udp` (RTP), `127.0.0.1:8088` (WebSocket) |
| AASIST | `127.0.0.1:8000` (API and admin) |
| PostgreSQL | Internal only |

Web files are mounted directly, so edits are available without rebuilding.
Rebuild after changing Asterisk configuration or inference code. Recreate
services after changing environment values; restart the web service after
changing the Nginx configuration.

```sh
docker compose up --build -d
docker compose restart
docker compose down
```

`docker compose down` preserves database records. Adding `-v` deletes the
database volume. Initialization SQL runs only for an empty volume; changing
`POSTGRES_PASSWORD` in `.env` does not update an existing database password.
The inference service initializes its experiment tables on startup, including
for existing volumes.

## Third-party components

The AASIST image downloads source, pretrained weights, `LICENSE`, and `NOTICE`
from the revision pinned in `aasist/Dockerfile`. JsSIP 3.10.0 is bundled in
`web/vendor/` with its MIT license. Asterisk is built from the version pinned
in the root `Dockerfile`.
