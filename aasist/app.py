import asyncio
import json
import logging
from contextlib import asynccontextmanager
from math import gcd
from pathlib import Path
from threading import Lock
from time import time
from typing import Literal
from uuid import UUID, uuid4

import numpy as np
import psycopg
from psycopg.rows import dict_row
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from scipy.signal import resample_poly

from admin import setup_admin
from data_utils import pad
from models.AASIST import Model
from metrics import update_network_summary


ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 16000
SAMPLES = 64600
LABELS = ("spoof", "bonafide")
inference_lock = Lock()
logger = logging.getLogger("uvicorn.error")
STREAM_SAMPLE_RATE = 8000
STREAM_CHUNK_BYTES = SAMPLES  # 32,300 PCM16 samples at 8 kHz: 4.0375 seconds.
MIN_AUDIO_RMS = 10 ** (-50 / 20)  # -50 dBFS energy threshold.
MIN_ACTIVE_SECONDS = 0.3


def initialize_predictions():
    """Initialize experiment tables, including on existing database volumes."""
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS experiment_calls (
                call_id uuid PRIMARY KEY,
                source_filename text NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS experiment_predictions (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                call_id uuid NOT NULL,
                chunk_index integer NOT NULL,
                audio_seconds double precision NOT NULL,
                model text NOT NULL,
                prediction text NOT NULL,
                score double precision NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (call_id, chunk_index)
            )
        """)
        connection.execute("""
            ALTER TABLE experiment_predictions
            ADD COLUMN IF NOT EXISTS source_filename text
        """)
        connection.execute("""
            ALTER TABLE experiment_predictions
            ADD COLUMN IF NOT EXISTS decision_margin double precision
        """)
        connection.execute("""
            ALTER TABLE experiment_calls
            ADD COLUMN IF NOT EXISTS stream_status text NOT NULL DEFAULT 'registered',
            ADD COLUMN IF NOT EXISTS ended_at timestamptz,
            ADD COLUMN IF NOT EXISTS error text,
            ADD COLUMN IF NOT EXISTS call_status text NOT NULL DEFAULT 'registered',
            ADD COLUMN IF NOT EXISTS call_error text,
            ADD COLUMN IF NOT EXISTS rtp_error text,
            ADD COLUMN IF NOT EXISTS classified_chunks integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS skipped_chunks integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS received_seconds double precision NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS analyzed_seconds double precision NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS average_score double precision,
            ADD COLUMN IF NOT EXISTS decision_margin double precision,
            ADD COLUMN IF NOT EXISTS prediction text NOT NULL DEFAULT 'inconclusive',
            ADD COLUMN IF NOT EXISTS spoof_audio_fraction double precision
        """)
        connection.execute("""
            ALTER TABLE experiment_calls
            ADD COLUMN IF NOT EXISTS audio_started_at_ms double precision,
            ADD COLUMN IF NOT EXISTS rtp_summary jsonb
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS experiment_rtp_samples (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                call_id uuid NOT NULL REFERENCES experiment_calls(call_id),
                stream_id text NOT NULL,
                sample_index integer NOT NULL,
                timestamp_ms double precision NOT NULL,
                packets_sent bigint NOT NULL,
                bytes_sent bigint NOT NULL,
                packets_lost bigint,
                jitter_ms double precision,
                round_trip_time_ms double precision,
                UNIQUE (call_id, stream_id, sample_index)
            )
        """)
        connection.execute("""
            ALTER TABLE experiment_rtp_samples
            ADD COLUMN IF NOT EXISTS server_timestamp_ms double precision,
            ADD COLUMN IF NOT EXISTS remote_timestamp_ms double precision,
            ADD COLUMN IF NOT EXISTS remote_server_timestamp_ms double precision,
            ADD COLUMN IF NOT EXISTS clock_sync_rtt_ms double precision,
            ADD COLUMN IF NOT EXISTS packets_received bigint,
            ADD COLUMN IF NOT EXISTS reported_fraction_lost double precision,
            ADD COLUMN IF NOT EXISTS codec_mime_type text,
            ADD COLUMN IF NOT EXISTS codec_clock_rate integer,
            ADD COLUMN IF NOT EXISTS codec_channels integer
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS experiment_chunks (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                call_id uuid NOT NULL REFERENCES experiment_calls(call_id),
                chunk_index integer NOT NULL,
                audio_start_seconds double precision NOT NULL,
                audio_seconds double precision NOT NULL,
                status text NOT NULL,
                rtp_summary jsonb,
                UNIQUE (call_id, chunk_index)
            )
        """)


def store_chunk(connection, call_id, chunk_index, audio_seconds, status):
    connection.execute(
        """INSERT INTO experiment_chunks
           (call_id, chunk_index, audio_start_seconds, audio_seconds, status)
           VALUES (%s, %s, %s, %s, %s)""",
        (call_id, chunk_index, chunk_index * STREAM_CHUNK_BYTES / (STREAM_SAMPLE_RATE * 2),
         audio_seconds, status),
    )
    update_network_summary(connection, call_id)


def predict_stream_chunk(call_id, chunk_index, audio):
    """Classify an AudioSocket chunk and save its prediction unless it is too quiet."""
    # Normalize little-endian PCM16 to match SoundFile's floating-point samples.
    waveform = np.frombuffer(audio, dtype="<i2").astype(np.float64) / 32768
    audio_seconds = len(audio) / (STREAM_SAMPLE_RATE * 2)
    try:
        result = predict_audio(waveform, STREAM_SAMPLE_RATE)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        logger.info("Experiment %s chunk %s skipped: %s", call_id, chunk_index, exc.detail)
        with psycopg.connect(connect_timeout=5) as connection:
            store_chunk(connection, call_id, chunk_index, audio_seconds, "skipped")
            connection.execute(
                "UPDATE experiment_calls SET skipped_chunks = skipped_chunks + 1 WHERE call_id = %s",
                (call_id,),
            )
        return

    with psycopg.connect(connect_timeout=5) as connection:
        store_chunk(connection, call_id, chunk_index, audio_seconds, "classified")
        connection.execute(
            """INSERT INTO experiment_predictions
               (call_id, chunk_index, audio_seconds, model, prediction, score, decision_margin, source_filename)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       (SELECT source_filename FROM experiment_calls WHERE call_id = %s))""",
            (call_id, chunk_index, audio_seconds,
             result.model, result.prediction, result.score, result.decision_margin, call_id),
        )
        update_call_summary(connection, call_id)
    logger.info("Experiment %s chunk %s: %s score=%s",
                call_id, chunk_index, result.prediction, result.score)


async def stream_call(reader, writer):
    """Read an AudioSocket call and classify full chunks plus any final audio."""
    call_id = None
    audio = bytearray()
    chunk_index = 0
    stream_error = None
    received_bytes = 0
    try:
        header = await reader.readexactly(3)
        if header[0] != 0x01 or int.from_bytes(header[1:], "big") != 16:
            raise ValueError("AudioSocket must start with a UUID")
        call_id = UUID(bytes=await reader.readexactly(16))
        await asyncio.to_thread(start_call, call_id)
        while True:
            try:
                header = await reader.readexactly(3)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise ValueError("Truncated AudioSocket header") from exc
                break
            kind = header[0]
            payload = await reader.readexactly(int.from_bytes(header[1:], "big"))
            if kind == 0x00:
                break
            if kind == 0x03:  # Ignore DTMF.
                continue
            if kind != 0x10 or len(payload) % 2:
                raise ValueError("Expected 8 kHz PCM16 AudioSocket audio")
            if received_bytes == 0 and payload:
                await asyncio.to_thread(mark_audio_started, call_id, time() * 1000)
            audio.extend(payload)
            received_bytes += len(payload)
            while len(audio) >= STREAM_CHUNK_BYTES:
                chunk = bytes(audio[:STREAM_CHUNK_BYTES])
                del audio[:STREAM_CHUNK_BYTES]
                await asyncio.to_thread(predict_stream_chunk, call_id, chunk_index, chunk)
                chunk_index += 1
        if audio:
            await asyncio.to_thread(predict_stream_chunk, call_id, chunk_index, bytes(audio))
    except asyncio.CancelledError:
        stream_error = "Audio stream interrupted by service shutdown."
        raise
    except Exception as exc:
        stream_error = str(exc)
        logger.exception("Experiment stream failed for call %s", call_id)
    finally:
        try:
            if call_id is not None:
                await asyncio.to_thread(
                    finish_call, call_id, stream_error,
                    received_bytes / (STREAM_SAMPLE_RATE * 2),
                )
        finally:
            writer.close()
            await writer.wait_closed()


def start_call(call_id):
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            """INSERT INTO experiment_calls (call_id, source_filename, stream_status, call_status)
               VALUES (%s, 'microphone or softphone', 'streaming', 'streaming')
               ON CONFLICT (call_id) DO UPDATE
               SET stream_status = 'streaming',
                   call_status = CASE WHEN experiment_calls.call_status = 'registered'
                                      THEN 'streaming' ELSE experiment_calls.call_status END""",
            (call_id,),
        )


def mark_audio_started(call_id, timestamp_ms):
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            "UPDATE experiment_calls SET audio_started_at_ms = %s WHERE call_id = %s",
            (timestamp_ms, call_id),
        )


def finish_call(call_id, error, received_seconds):
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            """UPDATE experiment_calls SET stream_status = %s, ended_at = now(), error = %s,
                   received_seconds = %s,
                   call_status = CASE WHEN call_status IN ('registered', 'streaming')
                                      THEN %s ELSE call_status END
               WHERE call_id = %s""",
            ('failed' if error is not None else 'completed', error, received_seconds,
             'failed' if error is not None else 'completed', call_id),
        )
        update_call_summary(connection, call_id)
        update_network_summary(connection, call_id)


def update_call_summary(connection, call_id):
    """Weight chunk scores by actual duration, excluding skipped audio."""
    connection.execute(
        """UPDATE experiment_calls SET
               classified_chunks = summary.classified_chunks,
               analyzed_seconds = summary.analyzed_seconds,
               average_score = summary.average_score,
               decision_margin = summary.decision_margin,
               spoof_audio_fraction = summary.spoof_audio_fraction,
               prediction = CASE WHEN summary.decision_margin > 0 THEN 'bonafide'
                                 WHEN summary.decision_margin < 0 THEN 'spoof'
                                 ELSE 'inconclusive' END
           FROM (
               SELECT count(*) AS classified_chunks,
                      coalesce(sum(audio_seconds), 0) AS analyzed_seconds,
                      sum(score * audio_seconds) / nullif(sum(audio_seconds), 0) AS average_score,
                      sum(decision_margin * audio_seconds) /
                        nullif(sum(audio_seconds) FILTER (WHERE decision_margin IS NOT NULL), 0)
                        AS decision_margin,
                      coalesce(sum(audio_seconds) FILTER (WHERE prediction = 'spoof'), 0) /
                        nullif(sum(audio_seconds), 0) AS spoof_audio_fraction
               FROM experiment_predictions WHERE call_id = %s
           ) summary WHERE call_id = %s""", (call_id, call_id),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = None
    try:
        config = json.loads((ROOT / "config/AASIST.conf").read_text())
        model = Model(config["model_config"]).to("cpu")
        checkpoint = torch.load(
            ROOT / config["model_path"], map_location="cpu", weights_only=True
        )
        model.load_state_dict(checkpoint)
        model.eval()
        app.state.model = model
        logger.info("Pretrained AASIST loaded on CPU")
    except Exception:
        logger.exception("Could not load pretrained AASIST")
    await asyncio.to_thread(initialize_predictions)
    admin_engine = await asyncio.to_thread(setup_admin, app)
    server = await asyncio.start_server(stream_call, "0.0.0.0", 9019)
    try:
        async with server:
            yield
    finally:
        app.state.model = None
        admin_engine.dispose()


app = FastAPI(title="AASIST inference", lifespan=lifespan)


class Prediction(BaseModel):
    model: Literal["AASIST"] = "AASIST"
    score: float = Field(
        description="Raw, uncalibrated bonafide logit (output column 1), not a probability."
    )
    prediction: Literal["spoof", "bonafide"]
    decision_margin: float = Field(
        description="Bonafide logit minus spoof logit; positive favors bonafide."
    )


class CallSource(BaseModel):
    source_filename: str = Field(default="microphone", min_length=1, max_length=4096)


class RtpSample(BaseModel):
    stream_id: str = Field(min_length=1, max_length=256)
    sample_index: int = Field(ge=0)
    timestamp_ms: float = Field(ge=0, allow_inf_nan=False)
    packets_sent: int = Field(ge=0)
    bytes_sent: int = Field(ge=0)
    # RTCP permits negative cumulative loss when duplicate packets are received.
    packets_lost: int | None = None
    jitter_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    round_trip_time_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    server_timestamp_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    remote_timestamp_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    remote_server_timestamp_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    clock_sync_rtt_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    packets_received: int | None = Field(default=None, ge=0)
    reported_fraction_lost: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    codec_mime_type: str | None = Field(default=None, max_length=128)
    codec_clock_rate: int | None = Field(default=None, gt=0)
    codec_channels: int | None = Field(default=None, gt=0)


class CallResult(BaseModel):
    call_status: Literal["completed", "failed", "stopped"]
    call_error: str | None = Field(default=None, max_length=4096)
    rtp_error: str | None = Field(default=None, max_length=4096)


@app.post("/experiment/calls", status_code=201)
def register_call(source: CallSource):
    call_id = uuid4()
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO experiment_calls (call_id, source_filename) VALUES (%s, %s)",
            (call_id, source.source_filename),
        )
    return {"call_id": str(call_id)}


@app.get("/experiment/calls/clock")
def server_clock():
    return {"server_timestamp_ms": time() * 1000}


@app.post("/experiment/calls/{call_id}/rtp", status_code=201)
def save_rtp(call_id: UUID, sample: RtpSample):
    with psycopg.connect(connect_timeout=5) as connection:
        if not connection.execute(
            "SELECT 1 FROM experiment_calls WHERE call_id = %s", (call_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="Call not found.")
        connection.execute(
            """INSERT INTO experiment_rtp_samples
               (call_id, stream_id, sample_index, timestamp_ms, packets_sent,
                bytes_sent, packets_lost, jitter_ms, round_trip_time_ms,
                server_timestamp_ms, remote_timestamp_ms, remote_server_timestamp_ms, clock_sync_rtt_ms,
                packets_received, reported_fraction_lost, codec_mime_type,
                codec_clock_rate, codec_channels)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (call_id, stream_id, sample_index) DO NOTHING""",
            (call_id, sample.stream_id, sample.sample_index, sample.timestamp_ms,
             sample.packets_sent, sample.bytes_sent, sample.packets_lost,
             sample.jitter_ms, sample.round_trip_time_ms,
             sample.server_timestamp_ms, sample.remote_timestamp_ms,
             sample.remote_server_timestamp_ms, sample.clock_sync_rtt_ms,
             sample.packets_received, sample.reported_fraction_lost, sample.codec_mime_type,
             sample.codec_clock_rate, sample.codec_channels),
        )
        update_network_summary(connection, call_id)
    return {"status": "saved"}


@app.post("/experiment/calls/{call_id}/result")
def save_call_result(call_id: UUID, result: CallResult):
    with psycopg.connect(connect_timeout=5) as connection:
        row = connection.execute(
            """UPDATE experiment_calls SET call_status = %s, call_error = %s, rtp_error = %s
               WHERE call_id = %s RETURNING call_id""",
            (result.call_status, result.call_error, result.rtp_error, call_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Call not found.")
    return {"status": "saved"}


@app.get("/experiment/calls/{call_id}")
def call_summary(call_id: UUID):
    with psycopg.connect(connect_timeout=5, row_factory=dict_row) as connection:
        call = connection.execute(
            "SELECT * FROM experiment_calls WHERE call_id = %s", (call_id,)
        ).fetchone()
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        rtp = connection.execute(
            """SELECT count(*) AS samples, avg(jitter_ms) AS average_jitter_ms,
                      max(jitter_ms) AS max_jitter_ms,
                      avg(round_trip_time_ms) AS average_round_trip_time_ms
               FROM experiment_rtp_samples WHERE call_id = %s""", (call_id,)
        ).fetchone()
        rtp["streams"] = connection.execute(
            """SELECT DISTINCT ON (stream_id) stream_id, packets_sent, bytes_sent,
                      packets_lost, jitter_ms, round_trip_time_ms
               FROM experiment_rtp_samples WHERE call_id = %s
               ORDER BY stream_id, sample_index DESC""", (call_id,)
        ).fetchall()
        chunks = connection.execute(
            """SELECT chunks.*, predictions.prediction, predictions.score, predictions.decision_margin
               FROM experiment_chunks chunks LEFT JOIN experiment_predictions predictions
                 ON chunks.call_id = predictions.call_id AND chunks.chunk_index = predictions.chunk_index
               WHERE chunks.call_id = %s ORDER BY chunks.chunk_index""", (call_id,),
        ).fetchall()
    return {**call, "rtp": {**rtp, **(call["rtp_summary"] or {})}, "chunks": chunks}


@app.get("/health")
def health():
    loaded = app.state.model is not None
    return JSONResponse(
        status_code=200 if loaded else 503,
        content={"status": "ok" if loaded else "unavailable", "model_loaded": loaded},
    )


@app.post("/predict", response_model=Prediction)
def predict(file: UploadFile):
    try:
        with sf.SoundFile(file.file) as source:
            if source.format not in {"WAV", "WAVEX", "RF64", "FLAC"}:
                raise HTTPException(status_code=400, detail="Upload WAV or FLAC audio.")
            sample_rate = source.samplerate
            audio = source.read(dtype="float64", always_2d=True)
    except (sf.LibsndfileError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid or corrupt audio file.") from exc
    return predict_audio(audio.mean(axis=1), sample_rate)


def predict_audio(audio, sample_rate):
    """Classify mono audio using the pretrained model's input and label conventions."""
    model = app.state.model
    if model is None:
        raise HTTPException(status_code=503, detail="AASIST model is unavailable.")

    if audio.shape[0] == 0:
        raise HTTPException(status_code=400, detail="Audio file is empty.")
    if not np.isfinite(audio).all():
        raise HTTPException(status_code=400, detail="Audio contains nonfinite samples.")

    if sample_rate != SAMPLE_RATE:
        divisor = gcd(sample_rate, SAMPLE_RATE)
        audio = resample_poly(audio, SAMPLE_RATE // divisor, sample_rate // divisor)

    # Apply the energy gate before padding, using only the model's input window.
    audio = audio[:SAMPLES]
    frame_samples = SAMPLE_RATE // 50  # 20 ms frames.
    frames = audio[:len(audio) // frame_samples * frame_samples].reshape(-1, frame_samples)
    active_frames = np.count_nonzero(np.mean(frames ** 2, axis=1) >= MIN_AUDIO_RMS ** 2)
    if active_frames * frame_samples < MIN_ACTIVE_SECONDS * SAMPLE_RATE:
        raise HTTPException(
            status_code=422,
            detail="Insufficient audible audio. Silence and very quiet audio are not classified.",
        )

    # Upstream pad() repeats short audio to fill the 64,600-sample input.
    waveform = torch.as_tensor(pad(audio, SAMPLES), dtype=torch.float32).unsqueeze(0)
    with inference_lock, torch.inference_mode():
        _, output = model(waveform)
    if not torch.isfinite(output).all():
        raise HTTPException(status_code=500, detail="Model returned a nonfinite score.")

    # Upstream labels: 0 = spoof, 1 = bonafide; the score is the column 1 logit.
    return Prediction(
        score=output[0, 1].item(), prediction=LABELS[output.argmax(dim=1).item()],
        decision_margin=(output[0, 1].double() - output[0, 0].double()).item(),
    )
