"""redpilot.contracts — 零依赖共享契约包。

worker(redpilot.worker)与 obs 平台都可安全依赖的中立层:
词汇/快照 schema/脱敏与截断/原子 IO/路径约定/折叠状态机/静态资产表。
约束:只允许 stdlib import(测试 test_purity 强制)。
"""

from .assets import ASSET_RX, ASSET_TYPES, asset_content_type
from .digest import ENTRY_CAP, TEXT_ENTRY_MAX, TEXT_FLUSH, FoldState, fold_rows
from .fsio import atomic_write_json, ensure_dir
from .paths import (DIGESTS_DIR, FLAG_FILES, FLAG_MAX_LINES, HEARTBEAT_PATH,
                    LIVE_DIR, ROSTER_FILENAME, TRANSCRIPT_FILENAME, safe_code)
from .platform import (ATTEMPT_STATES, CANONICAL_EVENT_TYPES,
                       CANONICAL_TERMINAL_STATUSES, EVALUATION_STATES,
                       JOB_STATES, WORKER_STATES, EventEnvelope,
                       is_canonical_event_type,
                       is_canonical_terminal_status, new_id)
from .redact import summarize_args
from .snapshot import LIVE_SNAPSHOT_KEYS, LiveSnapshot
from .text import (ASSISTANT_PREVIEW_MAX, ERROR_HEAD_MAX, ARGS_SUMMARY_MAX,
                   OUTPUT_TAIL_MAX, head_text, tail_text)
from .vocabulary import (ACTIVE_PHASES, CLOSABLE_STATUSES, ENVELOPE_KEYS,
                         LIVE_EVENT_KINDS, FLUSH_KINDS, OUT_OF_BAND_PREFIX,
                         PHASES, RUN_CLOSE_STATUSES, RUN_ID_RX, RUN_STATUSES,
                         SNAPSHOT_KIND, SSE_HEARTBEAT_S, Phase,
                         strip_for_snapshot, strip_out_of_band)

# 版本单源在 redpilot/__init__.py（单发行版）；本模块不再自带 __version__。

__all__ = [
    # text
    "ARGS_SUMMARY_MAX", "OUTPUT_TAIL_MAX", "ASSISTANT_PREVIEW_MAX", "ERROR_HEAD_MAX",
    "tail_text", "head_text",
    # redact
    "summarize_args",
    # fsio
    "atomic_write_json", "ensure_dir",
    # paths
    "FLAG_FILES", "FLAG_MAX_LINES", "safe_code", "LIVE_DIR", "DIGESTS_DIR",
    "ROSTER_FILENAME", "TRANSCRIPT_FILENAME", "HEARTBEAT_PATH",
    # vocabulary
    "PHASES", "Phase", "ACTIVE_PHASES", "RUN_STATUSES", "RUN_CLOSE_STATUSES",
    "CLOSABLE_STATUSES", "RUN_ID_RX", "LIVE_EVENT_KINDS", "FLUSH_KINDS",
    "SNAPSHOT_KIND", "SSE_HEARTBEAT_S", "ENVELOPE_KEYS", "OUT_OF_BAND_PREFIX",
    "strip_out_of_band", "strip_for_snapshot",
    # platform control-plane contract
    "EVALUATION_STATES", "JOB_STATES", "ATTEMPT_STATES", "WORKER_STATES",
    "EventEnvelope", "new_id", "CANONICAL_EVENT_TYPES",
    "CANONICAL_TERMINAL_STATUSES", "is_canonical_event_type",
    "is_canonical_terminal_status",
    # snapshot
    "LiveSnapshot", "LIVE_SNAPSHOT_KEYS",
    # digest
    "FoldState", "fold_rows", "ENTRY_CAP", "TEXT_FLUSH", "TEXT_ENTRY_MAX",
    # assets
    "ASSET_TYPES", "ASSET_RX", "asset_content_type",
]
