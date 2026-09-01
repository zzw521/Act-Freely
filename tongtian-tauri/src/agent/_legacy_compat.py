# -*- coding: utf-8 -*-
"""Legacy compatibility helpers for 1.x → 2.0 migration.

These functions exist solely to support upgrading sessions that were
created before the 2.0 state schema was introduced.
"""

from typing import List

from agentscope.message import Msg


def parse_legacy_memory_state(
    memory_raw: dict,
) -> tuple[List[Msg], str]:
    """Parse a 1.x ``InMemoryMemory.state_dict()`` payload.

    1.x stored ``{"content": [[msg_dict, marks], ...],
    "_compressed_summary": str}``.  2.0 keeps messages on
    ``AgentState.context`` instead, so this helper exists only for
    sessions on disk that pre-date the migration.  ``marks`` are dropped
    (only ``HINT`` / ``COMPRESSED`` were used and neither is reachable
    from the new state schema).

    Returns ``(messages, summary)``; either may be empty.
    """
    messages: List[Msg] = []
    for item in memory_raw.get("content", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            payload = item[0]
        else:
            payload = item
        if isinstance(payload, dict):
            messages.append(Msg.from_dict(payload))
        elif isinstance(payload, Msg):
            messages.append(payload)
    summary = memory_raw.get("_compressed_summary") or ""
    return messages, summary
