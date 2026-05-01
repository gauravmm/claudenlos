import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .models import Store, ToolCall


def _tool_full_name(call: ToolCall) -> str:
    if call.server == "claude-code":
        return call.method
    return f"mcp__{call.server}__{call.method}"


def _tool_short_name(full: str) -> str:
    """Return the method component (after last __)."""
    return full.rsplit("__", 1)[-1]


def _loc(call: ToolCall) -> str:
    return f"{os.path.basename(call.call_file)}:{call.call_line}"


def _percentiles(values: list[int | float], qs: list[float]) -> list[float]:
    if not values:
        return [0.0] * len(qs)
    s = sorted(values)
    n = len(s)
    result = []
    for q in qs:
        idx = q * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        result.append(s[lo] + (s[hi] - s[lo]) * (idx - lo))
    return result


# ── Per-tool stats ───────────────────────────────────────────────────────────

@dataclass
class ToolStats:
    name: str
    n: int = 0
    output_tokens: list[int] = field(default_factory=list)
    result_chars: list[int] = field(default_factory=list)
    error_count: int = 0
    max_result_chars: int = -1
    example_large: str = ""
    example_error: str = ""
    models: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def est_tokens(self, chars_per_token: float) -> float:
        return sum(self.result_chars) / chars_per_token

    def error_rate(self) -> float:
        return self.error_count / self.n if self.n else 0.0

    def pcts(self) -> tuple[float, float, float, float, float]:
        if not self.result_chars:
            return (0, 0, 0, 0, 0)
        p25, p50, p75, p95 = _percentiles(self.result_chars, [0.25, 0.50, 0.75, 0.95])
        return p25, p50, p75, p95, float(max(self.result_chars))


def compute_tool_stats(store: Store) -> dict[str, ToolStats]:
    stats: dict[str, ToolStats] = {}
    for call in store.calls:
        name = _tool_full_name(call)
        s = stats.setdefault(name, ToolStats(name=name))
        s.n += 1
        s.output_tokens.append(call.output_tokens)
        s.models[call.model] += 1
        if call.result_chars >= 0:
            s.result_chars.append(call.result_chars)
            if call.result_chars > s.max_result_chars:
                s.max_result_chars = call.result_chars
                s.example_large = _loc(call)
        if call.is_error:
            s.error_count += 1
            if not s.example_error:
                s.example_error = _loc(call)
    return stats


# ── Sequence stats ───────────────────────────────────────────────────────────

@dataclass
class SequenceStats:
    # {tool_name: [remaining_calls_after_this_call]}
    post_call_remaining: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    # {(from, to): count}
    transitions: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    # {tool_name: [is_retry]}
    retry_flags: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    # sequence length distribution
    seq_lengths: list[int] = field(default_factory=list)


def compute_sequence_stats(store: Store) -> SequenceStats:
    ss = SequenceStats()

    # Group calls by sequence_id
    by_seq: dict[int, list[ToolCall]] = defaultdict(list)
    for call in store.calls:
        by_seq[call.sequence_id].append(call)

    for seq_id, seq_calls in by_seq.items():
        seq_calls.sort(key=lambda c: c.position_in_seq)
        n = len(seq_calls)
        ss.seq_lengths.append(n)

        names = [_tool_full_name(c) for c in seq_calls]
        errors = [c.is_error for c in seq_calls]

        for i, call in enumerate(seq_calls):
            name = names[i]
            remaining = n - i - 1
            ss.post_call_remaining[name].append(remaining)

            # Retry: same tool immediately after an error of the same tool
            is_retry = (
                i > 0
                and names[i - 1] == name
                and errors[i - 1]
            )
            ss.retry_flags[name].append(is_retry)

        for i in range(n - 1):
            ss.transitions[(names[i], names[i + 1])] += 1

    return ss


# ── Findings ─────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    type: str
    description: str
    detail: str
    est_token_impact: float


def compute_findings(
    tool_stats: dict[str, ToolStats],
    seq_stats: SequenceStats,
    chars_per_token: float,
) -> list[Finding]:
    findings: list[Finding] = []
    total_tokens = sum(s.est_tokens(chars_per_token) for s in tool_stats.values())

    # Sort tools by estimated result tokens descending
    by_tokens = sorted(tool_stats.values(), key=lambda s: s.est_tokens(chars_per_token), reverse=True)

    # high_result_volume: top-3 tools exceeding 10k est tokens
    for s in by_tokens[:3]:
        et = s.est_tokens(chars_per_token)
        if et > 10_000:
            findings.append(Finding(
                type="high_result_volume",
                description=s.name,
                detail=f"est_tokens={_si(et)}",
                est_token_impact=et,
            ))

    # dominated_result: one tool > 50% of all tokens
    if total_tokens > 0:
        for s in by_tokens[:1]:
            frac = s.est_tokens(chars_per_token) / total_tokens
            if frac > 0.5:
                findings.append(Finding(
                    type="dominated_result",
                    description=s.name,
                    detail=f"frac={frac:.0%}",
                    est_token_impact=s.est_tokens(chars_per_token),
                ))

    # high_variance_result: p95/p50 > 3.0 for n>=10
    for s in tool_stats.values():
        if s.n < 10 or not s.result_chars:
            continue
        _, p50, _, p95, _ = s.pcts()
        if p50 > 0 and p95 / p50 > 3.0:
            findings.append(Finding(
                type="high_variance_result",
                description=s.name,
                detail=f"p95/p50={p95/p50:.2f}  example={s.example_large}",
                est_token_impact=s.est_tokens(chars_per_token),
            ))

    # chained_calls: A→B > 0.70 with n>=5
    from_counts: dict[str, int] = defaultdict(int)
    for (a, b), cnt in seq_stats.transitions.items():
        from_counts[a] += cnt
    for (a, b), cnt in seq_stats.transitions.items():
        total_from = from_counts[a]
        if total_from < 5:
            continue
        prob = cnt / total_from
        if prob > 0.70:
            # token impact: cost of the chained call times expected count
            b_stats = tool_stats.get(b)
            impact = b_stats.est_tokens(chars_per_token) if b_stats else float(cnt)
            findings.append(Finding(
                type="chained_calls",
                description=f"{a} -> {b}",
                detail=f"prob={prob:.2f}  n={cnt}",
                est_token_impact=impact,
            ))

    # high_retry_rate: retry rate > 0.20 for n>=5
    for name, flags in seq_stats.retry_flags.items():
        if len(flags) < 5:
            continue
        rate = sum(flags) / len(flags)
        if rate > 0.20:
            s = tool_stats.get(name)
            impact = s.est_tokens(chars_per_token) if s else 0.0
            findings.append(Finding(
                type="high_retry_rate",
                description=name,
                detail=f"retry_rate={rate:.0%}",
                est_token_impact=impact,
            ))

    # always_errors: error rate > 0.50 for n>=3
    for s in tool_stats.values():
        if s.n < 3:
            continue
        if s.error_rate() > 0.50:
            findings.append(Finding(
                type="always_errors",
                description=s.name,
                detail=f"error_rate={s.error_rate():.0%}  example={s.example_error}",
                est_token_impact=s.est_tokens(chars_per_token),
            ))

    # expensive_model: >30% of calls to high-result-volume tool from Opus
    for s in by_tokens[:3]:
        total_calls = s.n
        if total_calls == 0:
            continue
        opus_calls = sum(v for k, v in s.models.items() if "opus" in k.lower())
        if opus_calls / total_calls > 0.30:
            findings.append(Finding(
                type="expensive_model",
                description=s.name,
                detail=f"opus_frac={opus_calls/total_calls:.0%}",
                est_token_impact=s.est_tokens(chars_per_token),
            ))

    # single_call_sequences: >60% of sequences have exactly one call
    if seq_stats.seq_lengths:
        frac = sum(1 for l in seq_stats.seq_lengths if l == 1) / len(seq_stats.seq_lengths)
        if frac > 0.60:
            findings.append(Finding(
                type="single_call_sequences",
                description="(all tools)",
                detail=f"frac={frac:.0%}  n={len(seq_stats.seq_lengths)}",
                est_token_impact=total_tokens * frac,
            ))

    # Sort by impact, return top 5
    findings.sort(key=lambda f: f.est_token_impact, reverse=True)
    return findings[:5]


# ── Transition matrix ────────────────────────────────────────────────────────

def top_n_tools(tool_stats: dict[str, ToolStats], n: int = 15) -> list[str]:
    return [s.name for s in sorted(tool_stats.values(), key=lambda s: s.n, reverse=True)[:n]]


def transition_matrix(
    seq_stats: SequenceStats,
    tools: list[str],
) -> dict[str, dict[str, float]]:
    tool_set = set(tools)
    from_counts: dict[str, int] = defaultdict(int)
    for (a, b), cnt in seq_stats.transitions.items():
        if a in tool_set and b in tool_set:
            from_counts[a] += cnt

    matrix: dict[str, dict[str, float]] = {}
    for (a, b), cnt in seq_stats.transitions.items():
        if a not in tool_set or b not in tool_set:
            continue
        total = from_counts[a]
        if total == 0:
            continue
        matrix.setdefault(a, {})[b] = round(cnt / total, 2)

    return matrix


def _si(n: float) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(int(n))
