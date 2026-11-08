import asyncio
import json
import logging
from contextlib import asynccontextmanager
from math import gcd
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

import numpy as np
import psycopg
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from scipy.signal import resample_poly

from admin import setup_admin
from data_utils import pad
from models.AASIST import Model


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


def predict_stream_chunk(call_id, chunk_index, audio):
    """Classify an AudioSocket chunk and save its prediction unless it is too quiet."""
    # Normalize little-endian PCM16 to match SoundFile's floating-point samples.
    waveform = np.frombuffer(audio, dtype="<i2").astype(np.float64) / 32768
    try:
        result = predict_audio(waveform, STREAM_SAMPLE_RATE)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        logger.info("Experiment %s chunk %s skipped: %s", call_id, chunk_index, exc.detail)
        return

    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            """INSERT INTO experiment_predictions
               (call_id, chunk_index, audio_seconds, model, prediction, score, source_filename)
               VALUES (%s, %s, %s, %s, %s, %s,
                       (SELECT source_filename FROM experiment_calls WHERE call_id = %s))""",
            (call_id, chunk_index, len(audio) / (STREAM_SAMPLE_RATE * 2),
             result.model, result.prediction, result.score, call_id),
        )
    logger.info("Experiment %s chunk %s: %s score=%s",
                call_id, chunk_index, result.prediction, result.score)


async def stream_call(reader, writer):
    """Read an AudioSocket call and classify full chunks plus any final audio."""
    call_id = None
    audio = bytearray()
    chunk_index = 0
    try:
        header = await reader.readexactly(3)
        if header[0] != 0x01 or int.from_bytes(header[1:], "big") != 16:
            raise ValueError("AudioSocket must start with a UUID")
        call_id = UUID(bytes=await reader.readexactly(16))
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
            audio.extend(payload)
            while len(audio) >= STREAM_CHUNK_BYTES:
                chunk = bytes(audio[:STREAM_CHUNK_BYTES])
                del audio[:STREAM_CHUNK_BYTES]
                await asyncio.to_thread(predict_stream_chunk, call_id, chunk_index, chunk)
                chunk_index += 1
        if audio:
            await asyncio.to_thread(predict_stream_chunk, call_id, chunk_index, bytes(audio))
    except Exception:
        logger.exception("Experiment stream failed for call %s", call_id)
    finally:
        writer.close()
        await writer.wait_closed()


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


class CallSource(BaseModel):
    source_filename: str = Field(min_length=1, max_length=4096)


@app.post("/experiment/calls", status_code=201)
def register_call(source: CallSource):
    call_id = uuid4()
    with psycopg.connect(connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO experiment_calls (call_id, source_filename) VALUES (%s, %s)",
            (call_id, source.source_filename),
        )
    return {"call_id": str(call_id)}


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
        score=output[0, 1].item(), prediction=LABELS[output.argmax(dim=1).item()]
    )
