"""Static configuration and constants for Observatory."""
from __future__ import annotations

APP_NAME = "Observatory"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

# ---------------------------------------------------------------------------
# Safe local default provider
# ---------------------------------------------------------------------------
DEFAULT_PROVIDER_NAME = "Local llama.cpp"
DEFAULT_PROVIDER_TYPE = "llama.cpp"
DEFAULT_PROVIDER_URL = "http://127.0.0.1:8080"
DEFAULT_PROVIDER_AGENT_URL = "http://127.0.0.1:8091"

# ---------------------------------------------------------------------------
# Collection cadence
# ---------------------------------------------------------------------------
LLAMA_POLL_S = 1.0        # live metrics interval (health + /metrics)
PROPS_POLL_S = 30.0       # /props refresh (router-level build info)
MODELS_POLL_S = 15.0      # /v1/models refresh (detects model load/unload)
AGENT_POLL_S = 10.0       # host agent interval
STALE_AFTER_S = 20.0      # provider marked STALE after this long without success
OFFLINE_AFTER_FAILS = 3   # consecutive failed polls before OFFLINE
SESSION_END_DELAY_S = 8.0  # no token activity for this long -> session ended
SAMPLE_DT_MAX_S = 30.0    # gaps larger than this close a session first

# ---------------------------------------------------------------------------
# Storage / retention
# ---------------------------------------------------------------------------
DB_PATH_DEFAULT = "data/observatory.db"
DB_PATH_DEMO = "data/observatory_demo.db"

RETENTION_RAW_S = 2 * 3600      # fine-grained raw samples kept 2 hours
RETENTION_MID_S = 7 * 86400     # 10s buckets kept 7 days
RETENTION_FULL_S = 30 * 86400   # 60s buckets kept 30 days
RETENTION_SWEEP_S = 300         # run retention sweep every 5 minutes
BUCKET_MID_S = 10.0             # medium-resolution bucket size
BUCKET_FULL_S = 60.0            # old-resolution bucket size

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
MODEL_COLORS = ["#4b8de8", "#d8733e", "#48a77c", "#d29b25", "#8a7bc8", "#4fa3a5"]

# known llama.cpp prometheus metric names (first match wins).
# `llama_server_*` = classic per-server build; `llamacpp:*` = multi-model router
# (per-model /metrics?model=<name> on the spawned llama-server).
METRIC_ALIASES = {
    "tokens_total": ("llama_server_tokens_total", "llamacpp:tokens_total"),
    "prompt_total": ("llama_server_prompt_tokens_total", "llamacpp:prompt_tokens_total"),
    "gen_total": ("llama_server_generation_tokens_total", "llamacpp:tokens_predicted_total"),
    "prompt_seconds_total": ("llama_server_prompt_seconds_total",
                             "llamacpp:prompt_seconds_total"),
    "gen_seconds_total": ("llama_server_generation_seconds_total",
                          "llama_server_tokens_predicted_seconds_total",
                          "llamacpp:tokens_predicted_seconds_total"),
    "context_used": ("llama_server_context_used", "llama_server_ctx_used",
                     "llamacpp:n_tokens_max"),
    "context_max": ("llama_server_context_length", "llama_server_n_ctx"),
    "n_slots": ("llama_server_n_slots",),
    "slots_processing": ("llama_server_n_slots_processing", "llama_server_n_slots_busy",
                         "llamacpp:requests_processing"),
    "gen_tps": ("llama_server_token_generation_tps", "llama_server_generation_tps",
                "llama_server_tokens_per_second", "llamacpp:predicted_tokens_seconds"),
    "prompt_tps": ("llama_server_prompt_processing_tps", "llama_server_prompt_tps",
                   "llamacpp:prompt_tokens_seconds"),
    "mtp_proposed_total": ("llama_server_mtp_proposed_total", "llama_server_draft_tokens_proposed_total",
                           "llama_server_speculative_proposed_total",
                           "llamacpp:spec_decode_num_draft_tokens_total"),
    "mtp_accepted_total": ("llama_server_mtp_accepted_total", "llama_server_draft_tokens_accepted_total",
                           "llama_server_speculative_accepted_total",
                           "llamacpp:spec_decode_num_accepted_tokens_total"),
    "mtp_rejected_total": ("llama_server_mtp_rejected_total", "llama_server_draft_tokens_rejected_total",
                           "llama_server_speculative_rejected_total"),
    "mtp_avg_acc": ("llama_server_mtp_acceptance_rate", "llama_server_speculative_acceptance_rate"),
    "gpu_util": ("llama_server_gpu_utilization", "llama_server_gpu_util"),
    "vram_used_mb": ("llama_server_vram_used_mb",),
    "vram_total_mb": ("llama_server_vram_total_mb",),
}

# metric families we can be missing (used for "telemetry availability")
TELEMETRY_GROUPS = {
    "counters": ("tokens_total", "prompt_total", "gen_total"),
    "speeds": ("gen_tps", "prompt_tps"),
    "context": ("context_used", "context_max"),
    "mtp": ("mtp_proposed_total", "mtp_accepted_total", "mtp_avg_acc"),
    "gpu": ("gpu_util", "vram_used_mb"),
}

# quantization token vocabulary (longest first at match time)
QUANT_TOKENS = [
    "MXFP4", "MXFP8",
    "IQ2_XXS", "IQ2_XS", "IQ2_S",
    "IQ3_XXS", "IQ3_XS", "IQ3_S", "IQ3_M",
    "IQ4_XS", "IQ4_S", "IQ4_NL", "IQ4_ML", "IQ4_M",
    "IQ5_XS", "IQ5_S", "IQ5_M",
    "IQ6_XS", "IQ7_RS", "IQ8_0",
    "Q2_K_S", "Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L",
    "Q4_0", "Q4_1", "Q4_K_S", "Q4_K_M",
    "Q5_0", "Q5_1", "Q5_K_S", "Q5_K_M",
    "Q6_K", "Q8_0", "Q8_1",
    "F16", "BF16", "F32",
]

# tokens that are not part of a model family name
FAMILY_TAG_TOKENS = {
    "ad", "instruct", "chat", "gguf", "ggml", "unsafetensors",
    "finetune", "ft", "rl", "sft", "dpo", "base", "bf16",
}

RANGE_KEYS = ["today", "2d", "3d", "5d", "7d", "30d", "all"]

# chart bucket sizes (seconds) per range for detail graphs
RANGE_BUCKETS = {
    "1m": 2, "5m": 10, "15m": 30, "1h": 60, "session": 2,
    "24h": 300, "7d": 3600, "30d": 14400,
}
