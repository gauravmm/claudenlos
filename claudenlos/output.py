import sys
from .analysis import ToolStats, SequenceStats, Finding, top_n_tools, transition_matrix, _si, _tool_short_name, _percentiles


def _fmt(n: float) -> str:
    return _si(n)


def render(
    tool_stats: dict[str, "ToolStats"],
    seq_stats: "SequenceStats",
    findings: list["Finding"],
    chars_per_token: float,
    out=None,
) -> None:
    if out is None:
        out = sys.stdout

    # ── Result Size Distribution ─────────────────────────────────────────────
    print("## Result Size Distribution (chars)", file=out)
    cols = ["tool", "n", "p25", "p50", "p75", "p95", "max", "est_tokens", "example_large", "example_error"]
    print("\t".join(cols), file=out)

    by_tokens = sorted(tool_stats.values(), key=lambda s: s.est_tokens(chars_per_token), reverse=True)
    for s in by_tokens:
        if not s.result_chars:
            p25 = p50 = p75 = p95 = mx = 0.0
        else:
            p25, p50, p75, p95, mx = s.pcts()
        et = s.est_tokens(chars_per_token)
        row = [
            s.name,
            str(s.n),
            _fmt(p25),
            _fmt(p50),
            _fmt(p75),
            _fmt(p95),
            _fmt(mx),
            _fmt(et),
            s.example_large,
            s.example_error,
        ]
        print("\t".join(row), file=out)

    print(file=out)

    # ── Transition Matrix ────────────────────────────────────────────────────
    print("## Transitions (from -> to)", file=out)

    tools = top_n_tools(tool_stats, n=15)
    if tools:
        matrix = transition_matrix(seq_stats, tools)
        short = [_tool_short_name(t) for t in tools]
        # Only include rows that have at least one outgoing transition to a top tool
        rows_to_show = [t for t in tools if t in matrix]

        if rows_to_show:
            header = ["from\\to"] + short
            print("\t".join(header), file=out)
            for from_tool in rows_to_show:
                row_map = matrix[from_tool]
                row = [_tool_short_name(from_tool)]
                for to_tool in tools:
                    prob = row_map.get(to_tool, 0.0)
                    row.append(f"{prob:.2f}" if prob > 0 else "")
                print("\t".join(row), file=out)

    print(file=out)

    # ── Findings ─────────────────────────────────────────────────────────────
    print("## Findings", file=out)
    for f in findings:
        print(f"{f.type}\t{f.description}\t{f.detail}", file=out)
