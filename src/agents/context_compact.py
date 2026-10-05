"""Conservatively compact exported agent transcripts without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any


MIN_DUPLICATE_CHARS = 512
RECENT_MESSAGES = 4


def compact_messages(messages: list[Any]) -> tuple[list[Any], int]:
    """Replace only old, exact duplicate tool text with a retained-copy pointer."""
    seen: dict[tuple[str, str], tuple[int, str]] = {}
    compacted: list[Any] = []
    saved = 0
    cutoff = max(0, len(messages) - RECENT_MESSAGES)

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            compacted.append(message)
            continue
        role = message.get("role")
        name = message.get("name")
        content = message.get("content")
        status = message.get("status")
        if (
            role != "tool"
            or not isinstance(name, str)
            or not name
            or not isinstance(content, str)
            or len(content) < MIN_DUPLICATE_CHARS
            or bool(message.get("isError"))
            or bool(message.get("is_error"))
            or bool(message.get("error"))
            or message.get("success") is False
            or (status is not None and status not in ("ok", "success", "completed"))
        ):
            compacted.append(message)
            continue

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = (name, digest)
        original = seen.get(key)
        if original is None:
            seen[key] = (index, content)
            compacted.append(message)
            continue

        original_index, original_content = original
        pointer = f"[REQL exact duplicate of tool result at message {original_index}; sha256:{digest}]"
        if index >= cutoff or content != original_content or len(pointer) >= len(content):
            compacted.append(message)
            continue
        compacted.append({**message, "content": pointer})
        saved += len(content) - len(pointer)

    return compacted, saved


def compact_transcript(transcript: Any) -> tuple[Any, int]:
    """Compact a JSON message list or an object with a messages list."""
    if isinstance(transcript, list):
        return compact_messages(transcript)
    if isinstance(transcript, dict) and isinstance(transcript.get("messages"), list):
        messages, saved = compact_messages(transcript["messages"])
        return {**transcript, "messages": messages}, saved
    raise ValueError("expected a JSON message array or an object with a messages array")


def main(argv: list[str] | None = None) -> int:
    """Read one transcript from stdin and write the compacted JSON to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", action="store_true", help="Print character savings to stderr")
    args = parser.parse_args(argv)
    try:
        transcript = json.load(sys.stdin)
        result, saved = compact_transcript(transcript)
    except (ValueError, UnicodeError) as exc:
        print(f"REQL context compact: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    if args.stats:
        print(f"REQL context compact: saved {saved} content characters", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
