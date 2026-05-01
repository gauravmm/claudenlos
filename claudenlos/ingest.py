import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from .models import Store, ToolCall, Sequence

logger = logging.getLogger("claudenlos.ingest")

_intern: dict[str, str] = {}


def _i(s: str) -> str:
    try:
        return _intern[s]
    except KeyError:
        _intern[s] = s
        return s


def _parse_tool_name(name: str, aliases: dict[str, str]) -> tuple[str, str]:
    if "__" in name:
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            server = aliases.get(parts[1], parts[1])
            return _i(server), _i(parts[2])
    return _i("claude-code"), _i(name)


def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return 0


def _is_human_turn(content) -> bool:
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _process_file(path: str, aliases: dict[str, str]) -> tuple[
    list[tuple[str, int, str, str, str, int, int, int, bool, float, int, int]],
    list[tuple[int, str, float, int]],
    float,
]:
    """
    Run in a subprocess: read + parse + walk session chain.

    Returns:
      raw_calls: list of flat tuples (tool_use_id, seq_local, server, method, model,
                  session_id, output_tokens, result_chars, is_error, timestamp,
                  call_line, result_line)
      raw_seqs:  list of (seq_local_id, session_id, start_ts, call_count)
      duration:  wall-clock seconds spent in this worker
    """
    t0 = time.perf_counter()

    # ── Read ──────────────────────────────────────────────────────────────────
    records: list[tuple[int, dict]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append((lineno, json.loads(line)))
            except json.JSONDecodeError:
                pass

    # ── Build uuid map + find main branch ─────────────────────────────────────
    by_uuid: dict[str, tuple[int, dict]] = {}
    for lineno, rec in records:
        uid = rec.get("uuid")
        if uid and uid not in by_uuid:
            by_uuid[uid] = (lineno, rec)

    if not by_uuid:
        return [], [], time.perf_counter() - t0

    referenced = {rec.get("parentUuid") for _, rec in records if rec.get("parentUuid")}
    leaves = [u for u in by_uuid if u not in referenced]
    if not leaves:
        return [], [], time.perf_counter() - t0

    latest_leaf = max(leaves, key=lambda u: by_uuid[u][1].get("timestamp", ""))

    chain: list[tuple[int, dict]] = []
    cur: str | None = latest_leaf
    visited: set[str] = set()
    while cur and cur not in visited:
        if cur not in by_uuid:
            break
        visited.add(cur)
        chain.append(by_uuid[cur])
        cur = by_uuid[cur][1].get("parentUuid")
    chain.reverse()

    # ── Walk chain ────────────────────────────────────────────────────────────
    raw_calls: list[tuple] = []
    raw_seqs: list[tuple] = []
    pending_calls: dict[str, dict] = {}
    pending_results: dict[str, tuple[int, int, bool]] = {}

    local_seq_id = 0
    cur_seq_id: int | None = None
    cur_seq_calls: list[str] = []
    seq_start_ts: float = 0.0
    seq_session_id: str = ""

    def _flush():
        nonlocal cur_seq_id, cur_seq_calls, local_seq_id
        if cur_seq_id is None or not cur_seq_calls:
            cur_seq_id = None
            cur_seq_calls = []
            return
        raw_seqs.append((cur_seq_id, seq_session_id, seq_start_ts, len(cur_seq_calls)))
        for pos, tuid in enumerate(cur_seq_calls):
            meta = pending_calls.get(tuid)
            if meta is None:
                continue
            rc, rl, is_err = pending_results.get(tuid, (-1, -1, False))
            raw_calls.append((
                tuid, cur_seq_id,
                meta["server"], meta["method"], meta["model"], meta["session_id"],
                meta["output_tokens"], rc, is_err, meta["timestamp"],
                meta["call_line"], rl,
            ))
        cur_seq_id = None
        cur_seq_calls = []

    for lineno, rec in chain:
        rtype = rec.get("type")
        if rtype not in ("assistant", "user"):
            continue
        msg = rec.get("message") or {}

        if rtype == "user":
            content = msg.get("content")
            if _is_human_turn(content):
                _flush()
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tuid = block.get("tool_use_id", "")
                        is_err = bool(block.get("is_error") or False)
                        rc = _result_chars(block.get("content", ""))
                        pending_results[tuid] = (rc, lineno, is_err)

        elif rtype == "assistant":
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if not tool_uses:
                continue

            usage = msg.get("usage") or {}
            total_out = usage.get("output_tokens") or 0
            model = msg.get("model") or ""
            session_id = rec.get("sessionId") or ""
            ts = _parse_ts(rec.get("timestamp") or "")

            if cur_seq_id is None:
                cur_seq_id = local_seq_id
                local_seq_id += 1
                seq_start_ts = ts
                seq_session_id = session_id

            n = len(tool_uses)
            tokens_each = total_out // n if n else 0

            for tu in tool_uses:
                tuid = tu.get("id", "")
                server, method = _parse_tool_name(tu.get("name", ""), aliases)
                pending_calls[tuid] = {
                    "server": server, "method": method, "model": model,
                    "session_id": session_id, "output_tokens": tokens_each,
                    "call_line": lineno, "timestamp": ts,
                }
                cur_seq_calls.append(tuid)

    _flush()
    return raw_calls, raw_seqs, time.perf_counter() - t0


# Top-level wrapper so ProcessPoolExecutor can pickle it
def _process_file_args(args: tuple) -> tuple:
    return _process_file(*args)


def discover_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for base in paths:
        files.extend(base.rglob("*.jsonl"))
    return sorted(files)


async def ingest(
    paths: list[Path],
    aliases: dict[str, str],
    chars_per_token: float = 3.5,
) -> Store:
    store = Store()
    files = discover_files(paths)
    if not files:
        return store

    loop = asyncio.get_event_loop()
    n_workers = max(4, os.cpu_count() or 4)
    executor = ProcessPoolExecutor(max_workers=n_workers)

    next_seq_id = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task("Ingesting…", total=len(files))

        async def process_one(fpath: Path) -> None:
            nonlocal next_seq_id

            raw_calls, raw_seqs, duration = await loop.run_in_executor(
                executor, _process_file, str(fpath), aliases
            )

            # UUID dedup in asyncio thread (lock-free — single-threaded event loop)
            # We track tool_use_ids that came from already-seen UUIDs to filter them.
            # Since _process_file already deduplicated within the file, we only need
            # to handle cross-file mirroring (rare: ~744 records per the spec).
            # Strategy: deduplicate on tool_use_id, which is globally unique.
            deduped_calls = []
            for call_tuple in raw_calls:
                tuid = call_tuple[0]
                if tuid not in store.seen_uuids:
                    store.seen_uuids.add(tuid)
                    deduped_calls.append(call_tuple)

            # Assign global sequence IDs atomically
            seq_offset = next_seq_id
            next_seq_id += len(raw_seqs)

            for (lsid, session_id, start_ts, call_count) in raw_seqs:
                store.sequences.append(Sequence(
                    sequence_id=lsid + seq_offset,
                    session_id=_i(session_id),
                    start_ts=start_ts,
                    call_count=call_count,
                ))

            path_str = _i(str(fpath))
            for tuid, lsid, server, method, model, session_id, output_tokens, result_chars, is_error, timestamp, call_line, result_line in deduped_calls:
                store.calls.append(ToolCall(
                    tool_use_id=tuid,
                    server=_i(server),
                    method=_i(method),
                    model=_i(model),
                    session_id=_i(session_id),
                    sequence_id=lsid + seq_offset,
                    position_in_seq=0,  # recomputed below
                    output_tokens=output_tokens,
                    result_chars=result_chars,
                    is_error=is_error,
                    timestamp=timestamp,
                    call_file=path_str,
                    call_line=call_line,
                    result_line=result_line,
                ))

            store.durations.append(duration)
            progress.advance(task_id)

        await asyncio.gather(*[process_one(f) for f in files])

    executor.shutdown(wait=False)

    # Recompute position_in_seq globally (local positions were lost in dedup filtering)
    _recompute_positions(store)

    return store


def _recompute_positions(store: Store) -> None:
    """Assign correct position_in_seq after cross-file dedup may have reordered calls."""
    from collections import defaultdict
    by_seq: dict[int, list[ToolCall]] = defaultdict(list)
    for call in store.calls:
        by_seq[call.sequence_id].append(call)
    for seq_calls in by_seq.values():
        seq_calls.sort(key=lambda c: c.timestamp)
        for pos, call in enumerate(seq_calls):
            call.position_in_seq = pos
