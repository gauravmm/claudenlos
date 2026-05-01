import argparse
import asyncio
import logging
import statistics
import sys
import time
from pathlib import Path

from .ingest import ingest
from .analysis import compute_tool_stats, compute_sequence_stats, compute_findings, _tool_full_name
from .output import render

logger = logging.getLogger("claudenlos")


def _build_aliases(alias_args: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for spec in alias_args or []:
        parts = [p.strip() for p in spec.split(",")]
        if len(parts) < 2:
            continue
        canonical = parts[0]
        for alt in parts[1:]:
            aliases[alt] = canonical
    return aliases


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claudenlos",
        description="Analyse Claude Code tool-call history.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="~/.claude/projects directories to analyse (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--alias",
        action="append",
        metavar="a,b,c",
        help="Comma-separated list; first name is canonical, rest are aliases. Repeatable.",
    )
    parser.add_argument(
        "--chars-per-token",
        type=float,
        default=3.5,
        metavar="N",
        help="Characters per token for result-size estimation (default: 3.5)",
    )
    parser.add_argument(
        "--include-builtins",
        action="store_true",
        help="Include built-in Claude Code tools (Bash, Read, Edit, …) in the analysis.",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=5,
        metavar="N",
        help="Exclude tools with fewer than N calls from analysis (default: 5)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(stream=sys.stderr, level=log_level, format="%(name)s: %(message)s")

    if args.paths:
        paths = [Path(p).expanduser() for p in args.paths]
    else:
        paths = sorted(
            p for p in Path("~").expanduser().glob(".claude*/projects")
            if p.is_dir()
        )

    if not paths:
        print("No project directories found.", file=sys.stderr)
        sys.exit(1)

    aliases = _build_aliases(args.alias)
    if aliases:
        logger.debug("Aliases: %s", aliases)

    t_ingest = time.perf_counter()
    store = asyncio.run(ingest(paths, aliases, args.chars_per_token))
    wall = time.perf_counter() - t_ingest

    if not store.calls:
        print("No tool calls found.", file=sys.stderr)
        sys.exit(0)

    # Timing summary to stderr
    if store.durations:
        d = store.durations
        n = len(d)
        if n >= 2:
            qs = statistics.quantiles(d, n=20)
            p25, p50, p75, p95 = qs[4], qs[9], qs[14], qs[18]
        else:
            p25 = p50 = p75 = p95 = d[0]
        mx = max(d)
        print(f"Processed {n} files in {wall:.2f}s", file=sys.stderr)
        print(
            f"Parse time per file: p25={p25*1000:.1f}ms  p50={p50*1000:.1f}ms"
            f"  p75={p75*1000:.1f}ms  p95={p95*1000:.1f}ms  max={mx*1000:.1f}ms",
            file=sys.stderr,
        )

    calls = store.calls if args.include_builtins else [c for c in store.calls if c.server != "claude-code"]
    tool_stats = compute_tool_stats(store, calls)

    excluded_count = 0
    if args.min_count > 1:
        qualifying = {name for name, s in tool_stats.items() if s.n >= args.min_count}
        excluded_count = len(tool_stats) - len(qualifying)
        calls = [c for c in calls if _tool_full_name(c) in qualifying]
        tool_stats = {k: v for k, v in tool_stats.items() if k in qualifying}

    seq_stats = compute_sequence_stats(store, calls)
    findings = compute_findings(tool_stats, seq_stats, args.chars_per_token)

    render(tool_stats, seq_stats, findings, args.chars_per_token, excluded_count, args.min_count)


if __name__ == "__main__":
    main()
