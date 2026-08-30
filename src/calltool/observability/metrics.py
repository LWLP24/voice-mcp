from __future__ import annotations

from prometheus_client import Counter, Histogram

CALLS_CREATED = Counter("calltool_calls_created_total", "Created call jobs")
CALLS_FINISHED = Counter(
    "calltool_calls_finished_total", "Finished calls", labelnames=("status", "reason")
)
TOOL_LATENCY = Histogram(
    "calltool_tool_latency_seconds",
    "Local voice tool latency",
    labelnames=("tool",),
    buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.4, 1.0, 2.5),
)
TURN_LATENCY = Histogram(
    "calltool_turn_latency_seconds",
    "Remote speech end to first agent playout",
    buckets=(0.1, 0.25, 0.4, 0.6, 0.8, 1.2, 2.0, 5.0),
)
BARGE_IN_LATENCY = Histogram(
    "calltool_barge_in_stop_latency_seconds",
    "Speech detection to agent output stop",
    buckets=(0.05, 0.1, 0.15, 0.25, 0.4, 0.8, 1.5),
)
NATIVE_METRIC_EVENTS = Counter(
    "calltool_livekit_metric_events_total",
    "LiveKit-native model metric events",
    labelnames=("source", "kind"),
)
NATIVE_METRIC_DURATION = Histogram(
    "calltool_livekit_metric_duration_seconds",
    "Durations reported by LiveKit-native model metrics",
    labelnames=("source", "kind"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15),
)
END_OF_TURN_DELAY = Histogram(
    "calltool_livekit_end_of_turn_delay_seconds",
    "LiveKit per-turn end-of-turn decision delay",
    buckets=(0.01, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 2.5, 5),
)
TRANSCRIPTION_DELAY = Histogram(
    "calltool_livekit_transcription_delay_seconds",
    "LiveKit per-turn final transcription delay",
    buckets=(0.01, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 2.5, 5),
)
INTERRUPTIONS = Counter(
    "calltool_voice_interruptions_total",
    "Native interruption decisions",
    labelnames=("result",),
)
