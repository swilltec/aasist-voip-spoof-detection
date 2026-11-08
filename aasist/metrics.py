import numpy as np
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def metric_summary(samples, reports, intervals, start_ms=None, end_ms=None):
    def inside(timestamp):
        return start_ms is None or (timestamp is not None and start_ms <= timestamp < end_ms)

    selected = [row for row in samples if start_ms is None or inside(row["server_timestamp_ms"])]
    measured = [row for row in reports if inside(row["remote_server_timestamp_ms"])]
    codec_samples = list(selected)
    if start_ms is not None:
        previous = {}
        for row in samples:
            if row["codec_mime_type"] and row["server_timestamp_ms"] is not None and row["server_timestamp_ms"] < start_ms:
                previous[row["stream_id"]] = row
        codec_samples.extend(previous.values())
    codecs = sorted({
        (row["codec_mime_type"], row["codec_clock_rate"], row["codec_channels"])
        for row in codec_samples if row["codec_mime_type"]
    }, key=str)
    summary = {
        "samples": len(selected),
        "receiver_reports": len(measured),
        "codecs": [dict(zip(("mime_type", "clock_rate", "channels"), codec)) for codec in codecs],
        "packet_loss_percent": None,
        "loss_packets": None,
        "expected_packets": None,
        "loss_counter_basis": None,
        "loss_allocation": "whole_report_intervals" if start_ms is None else "interval_overlap_estimate",
        "loss_interval_count": 0,
        "alignment_is_approximate": True,
    }
    for field, label in (("jitter_ms", "jitter"), ("round_trip_time_ms", "round_trip_time")):
        values = [row[field] for row in measured if row[field] is not None]
        summary[f"average_{label}_ms"] = float(np.mean(values)) if values else None
        summary[f"p95_{label}_ms"] = float(np.percentile(values, 95)) if values else None
        summary[f"max_{label}_ms"] = max(values) if values else None

    lost = expected = 0
    bases = set()
    for interval in intervals:
        weight = 1
        if start_ms is not None:
            duration = interval["end_ms"] - interval["start_ms"]
            overlap = min(end_ms, interval["end_ms"]) - max(start_ms, interval["start_ms"])
            if overlap <= 0 or duration <= 0:
                continue
            weight = overlap / duration
        lost += interval["lost"] * weight
        expected += interval["expected"] * weight
        bases.add(interval["basis"])
        summary["loss_interval_count"] += 1
    if expected > 0:
        summary.update(
            loss_packets=lost,
            expected_packets=expected,
            # Negative cumulative loss adjustments can reflect recovered/duplicate packets.
            packet_loss_percent=100 * min(1, max(0, lost / expected)),
            loss_counter_basis=next(iter(bases)) if len(bases) == 1 else "mixed",
        )
    return summary


def update_network_summary(connection, call_id):
    """Persist call and chunk summaries; serialize writers on the call row."""
    with connection.cursor(row_factory=dict_row) as cursor:
        call = cursor.execute(
            "SELECT audio_started_at_ms FROM experiment_calls WHERE call_id = %s FOR NO KEY UPDATE",
            (call_id,),
        ).fetchone()
        if call is None:
            return
        samples = cursor.execute(
            "SELECT * FROM experiment_rtp_samples WHERE call_id = %s ORDER BY stream_id, sample_index",
            (call_id,),
        ).fetchall()
        chunks = cursor.execute(
            "SELECT * FROM experiment_chunks WHERE call_id = %s ORDER BY chunk_index", (call_id,),
        ).fetchall()

    reports = []
    intervals = []
    previous = {}
    resets = 0
    for row in samples:
        report_timestamp = row["remote_timestamp_ms"] or row["remote_server_timestamp_ms"]
        if report_timestamp is None:
            continue
        prior = previous.get(row["stream_id"])
        # Repeated getStats() snapshots often contain the same RTCP report.
        if prior and report_timestamp <= (prior["remote_timestamp_ms"] or prior["remote_server_timestamp_ms"]):
            continue
        timestamp = row["remote_server_timestamp_ms"]
        reports.append(row)
        previous[row["stream_id"]] = row
        if row["packets_lost"] is None:
            continue

        def counter(sample):
            if sample["packets_received"] is not None:
                return sample["packets_received"] + sample["packets_lost"], "receiver"
            return sample["packets_sent"], "sender_estimate"

        expected, basis = counter(row)
        lost = row["packets_lost"]
        start = call["audio_started_at_ms"]
        if prior:
            if prior["packets_lost"] is None:
                continue
            prior_expected, prior_basis = counter(prior)
            if prior_basis != basis:
                continue
            expected -= prior_expected
            lost -= prior["packets_lost"]
            start = prior["remote_server_timestamp_ms"]
        if expected < 0 or (prior and row["packets_sent"] < prior["packets_sent"]):
            resets += 1
            continue
        if expected > 0 or lost != 0:
            intervals.append(dict(start_ms=start, end_ms=timestamp, lost=lost, expected=expected, basis=basis))

    summary = metric_summary(samples, reports, intervals)
    summary["counter_resets"] = resets
    summary["alignment"] = "clock_synced_report_reception_and_audio_sample_offsets"
    summary["alignment_is_approximate"] = True
    summary["loss_observed_until_ms"] = max(
        (row["end_ms"] for row in intervals if row["end_ms"] is not None), default=None,
    )
    summary["clock_sync_rtt_ms"] = max(
        (row["clock_sync_rtt_ms"] for row in samples if row["clock_sync_rtt_ms"] is not None), default=None,
    )
    codec_history = []
    prior_codecs = {}
    for row in samples:
        codec = (row["codec_mime_type"], row["codec_clock_rate"], row["codec_channels"])
        if codec[0] and prior_codecs.get(row["stream_id"]) != codec:
            codec_history.append(dict(
                stream_id=row["stream_id"], server_timestamp_ms=row["server_timestamp_ms"],
                mime_type=codec[0], clock_rate=codec[1], channels=codec[2],
                is_change=row["stream_id"] in prior_codecs,
            ))
            prior_codecs[row["stream_id"]] = codec
    summary["codec_history"] = codec_history
    summary["codec_change_count"] = sum(row["is_change"] for row in codec_history)
    worst_loss = []
    for chunk in chunks:
        metrics = None
        if call["audio_started_at_ms"] is not None:
            start = call["audio_started_at_ms"] + chunk["audio_start_seconds"] * 1000
            end = start + chunk["audio_seconds"] * 1000
            # Reports arriving after the last audio frame stay in the call summary.
            usable = [interval for interval in intervals
                      if interval["start_ms"] is not None and interval["end_ms"] is not None]
            metrics = metric_summary(samples, reports, usable, start, end)
            if metrics["packet_loss_percent"] is not None:
                worst_loss.append(metrics["packet_loss_percent"])
        connection.execute(
            "UPDATE experiment_chunks SET rtp_summary = %s WHERE call_id = %s AND chunk_index = %s",
            (Jsonb(metrics) if metrics is not None else None, call_id, chunk["chunk_index"]),
        )
    summary["worst_chunk_packet_loss_percent"] = max(worst_loss) if worst_loss else None
    connection.execute(
        "UPDATE experiment_calls SET rtp_summary = %s WHERE call_id = %s", (Jsonb(summary), call_id),
    )
