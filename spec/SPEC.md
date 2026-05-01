# Claudenlos — Implementation Specification

## 1. Data Source Structure

Claude Code stores conversation history as JSONL files under `~/.claude/projects/`. The directory structure is:

```
~/.claude/projects/
  <project-slug>/              # project name encoded as path with / → -
    <session-uuid>.jsonl       # one file per conversation session
    <session-uuid>/
      subagents/
        <agent-id>.jsonl       # subagent sessions (isSidechain=true)
```

### Record Types

Each line in a JSONL file is a record. Only two types matter for analysis:

| `.type`     | Meaning                                      |
|-------------|----------------------------------------------|
| `assistant` | Claude's turn; contains tool_use blocks      |
| `user`      | Human or tool feedback; contains tool_result |
| others      | `attachment`, `queue-operation`, `file-history-snapshot`, `ai-title`, `last-prompt`, `system`, `permission-mode` — skip |

### Key Fields

**All records:**

- `.uuid` — unique ID for this record (744 duplicates exist across files from subagent mirroring)
- `.parentUuid` — links to previous record, forming a DAG (branching is possible)
- `.sessionId` — UUID of the owning session file
- `.isSidechain` — `true` for subagent sessions; `false`/`null` for main sessions
- `.timestamp` — ISO 8601

**Assistant records (`.type == "assistant"`):**

- `.message.content[]` — array of content blocks; tool calls have `.type == "tool_use"`:
  - `.id` — unique tool call ID (e.g. `toolu_01...`)
  - `.name` — tool name; MCP tools use format `mcp__<server>__<method>`
  - `.input` — JSON object of arguments
- `.message.usage` — token counts for this turn:
  - `.input_tokens` — fresh input tokens (usually tiny due to caching)
  - `.cache_creation_input_tokens` — tokens written to cache
  - `.cache_read_input_tokens` — tokens read from cache
  - `.output_tokens` — tokens generated (includes tool call JSON)
  - `.iterations[]` — per-iteration breakdown (usually length 1)

**User records (`.type == "user"`) — tool results:**

- `.message.content[]` with `.type == "tool_result"`:
  - `.tool_use_id` — matches the `.id` from the assistant's tool_use block
  - `.is_error` — `true` if the tool call failed
  - `.content` — string or array of `{type, text}` blocks; this is the response body

**User records — human input:**

- `.message.content[]` with `.type == "text"` — marks a genuine human turn, breaking a sequence

### Parsing Complexity

The `parentUuid` chain is a linked list but may branch (conversation resumption creates new branches). For sequence analysis, follow the linear chain of the most-recent branch from session root. Deduplication: index all records by `.uuid` on first encounter, skip on re-encounter.

---

## 2. Derived Concepts

### Tool Namespace

MCP tool names are `mcp__<server>__<method>`. The "server" component serves as the MCP name. The `--alias` flag maps multiple server names to a canonical name before all analysis (simple string substitution at ingest time).

Built-in tools (Bash, Read, Edit, Write, Grep, Glob, etc.) have no prefix; treat them as server `"claude-code"`.

### Sequence

A **sequence** is a maximal run of assistant+tool-result turns with no human-input turn interleaving. Formally: starting from a human-input record, collect all (assistant → tool_result)* turns until the next human-input record or session end.

Detection: traverse the `parentUuid` chain. A human turn is any `user` record whose `message.content` contains a `text` block. A tool-return turn is a `user` record whose content contains only `tool_result` blocks.

### Token Attribution

Usage counters are per-assistant-turn, not per-tool-call. Approximations used:

- **Tool call cost** ≈ output tokens for an assistant turn that calls exactly one tool. For multi-tool turns, distribute output_tokens evenly among tools in that turn.
- **Tool result cost** ≈ character length of `.content` as a proxy (exact token count requires a tokenizer). Store raw character lengths; convert with a configurable `chars_per_token` ratio (default: 3.5).
- **True input tokens** = `input_tokens + cache_read_input_tokens` (what the model actually processed); `cache_creation_input_tokens` is a write cost.

---

## 3. Analysis Steps

### 3.1 Per-(MCP, tool) Statistics

For each `(server, method)` pair, collect:

- **Call count** — increment once per tool_use block
- **Output token distribution** — output_tokens of the assistant turn / tools_in_turn
- **Result character length distribution** — length of `.content` in the matching tool_result
- **Error rate** — fraction of calls where `is_error == true`
- **Total estimated result tokens** — sum of result char lengths / chars_per_token

**Difficulty: Easy.** Straightforward streaming aggregation. The only tricky part is joining tool_use (in assistant records) to tool_result (in user records) via `tool_use_id`. Build a hash map `tool_use_id → tool_name` during the assistant-record pass, then look it up during the user-record pass.

### 3.2 Call Sequence Analysis

#### 3.2.1 Post-call session length distribution

"Once tool A is called, how many more tool calls remain in this sequence?" Emit one sample per tool call: `(tool_name, remaining_calls_in_sequence)`. Aggregate as a histogram per tool.

**Difficulty: Easy**, given sequences are already identified.

#### 3.2.2 Tool transition matrix (A → B)

Within each sequence, emit `(prev_tool, next_tool)` for every adjacent pair. Compute a normalized transition matrix. Highlight high-probability pairs as actionable findings (e.g., `notion-search` almost always followed by `notion-fetch`).

**Difficulty: Easy.**

#### 3.2.3 Retry rate (A after A failure)

For each tool call, check if the immediately preceding call in the sequence was the same tool with `is_error == true`. Emit `(tool_name, is_retry: bool)`. Report retry fraction per tool.

**Difficulty: Easy**, with the error flag already on the tool_result record.

#### 3.2.4 Model used per call

The model that made each tool call is in `.message.model` on the assistant record (e.g. `"claude-sonnet-4-6"`, `"claude-opus-4-7"`). Store it on `ToolCall` as an interned string. This enables:

- Per-tool model distribution: which tools are called disproportionately by expensive models
- Cost estimation: multiply token counts by per-model pricing to get dollar estimates
- Model-split histograms: do result sizes differ by calling model? (proxy for task complexity)

Add `.model: str` to `ToolCall`. Interning keeps memory cost negligible (~5 distinct values in practice).

**Difficulty: Trivial** — field is already present on every assistant record.

---

## 4. In-Memory Storage Model

### Design Goals

- Fast ingest from 231+ JSONL files (potentially thousands)
- Deduplication by UUID
- Efficient downstream aggregation without materializing full call records
- Minimal peak memory; streaming where possible

### Data Structures

```python
# Immutable after ingest; all integers to minimize boxing

@dataclass(slots=True)
class ToolCall:
    tool_use_id: str        # e.g. "toolu_01..."
    server: str             # interned string (MCP server name or "claude-code")
    method: str             # interned string
    model: str              # interned string (e.g. "claude-sonnet-4-6")
    session_id: str         # interned UUID
    sequence_id: int        # assigned integer; same value = same sequence
    position_in_seq: int    # 0-based index within the sequence
    output_tokens: int      # assistant turn output_tokens / tools_in_turn
    result_chars: int       # len(content) of matching tool_result, -1 if missing
    is_error: bool
    timestamp: float        # Unix seconds
    # Source location for linking back to examples:
    call_file: str          # interned path to the JSONL file
    call_line: int          # 1-based line of the assistant record containing this tool_use
    result_line: int        # 1-based line of the user record containing the tool_result (-1 if missing)

# String interning: use a single dict[str, str] across all calls
# Typical servers: ~10-20; typical methods: ~50-100; file paths: ~231; UUIDs: hundreds of thousands

@dataclass(slots=True)
class Sequence:
    sequence_id: int
    session_id: str
    start_ts: float
    call_count: int         # filled after ingest

# Top-level store
@dataclass
class Store:
    calls: list[ToolCall]              # append-only during ingest
    sequences: list[Sequence]
    seen_uuids: set[str]               # deduplication
    tool_use_id_to_tool: dict[str, tuple[str,str]]  # id → (server, method)
    # cleared after ingest to free memory:
    _pending_results: dict[str, int]   # tool_use_id → result_chars
```

### Ingest Pipeline

```
File discovery (glob)
    │
    ▼ (asyncio + ThreadPoolExecutor, N=cpu_count workers)
Per-file reader (reads lines with enumerate, yields (lineno, raw_dict))
    │ UUID dedup check (shared set, lock-free via asyncio single-thread)
    ▼
Record classifier (type dispatch)
    ├── assistant → extract tool_use blocks + lineno, update tool_use_id_to_tool
    └── user      → extract tool_result blocks + lineno, update _pending_results
    │
    ▼ (post-file: sequence assignment)
Sequence detector (walk parentUuid chain, assign sequence_ids)
    │
    ▼
ToolCall materialization (join tool_use + tool_result + sequence)
    │
    ▼
Store.calls (numpy arrays for numeric fields after ingest)
```

**Threading model**: Use `asyncio` as the scheduler with `loop.run_in_executor(ThreadPoolExecutor)` for file I/O and JSON parsing (CPU-bound). Each worker reads one file and returns a list of raw dicts. The main asyncio loop handles UUID deduplication (single-threaded, no locks needed) and sequence linking. Target: parse all 231 files in <1s on typical hardware.

**Post-ingest compaction**: After all files are loaded, drop `seen_uuids` and `_pending_results`. Aggregations run directly over `Store.calls` (list of dataclasses). If profiling shows this is a bottleneck, convert to a `numpy` record array or `polars` DataFrame at that point.

**Per-file timing**: Record wall-clock duration for each file (from open to ToolCall materialization) using `time.perf_counter`. After all files are processed, emit a timing summary to stderr:

```
Processed 231 files in 0.84s
Parse time per file: p25=1.2ms  p50=2.1ms  p75=4.8ms  p95=18.3ms  max=47.1ms
```

Store durations in a plain `list[float]` alongside `Store`; compute percentiles with `statistics.quantiles` (stdlib, no numpy needed). This makes it easy to spot outlier files worth investigating.

**Critical path note**: The `parentUuid` chain walk (Phase 2) requires all records from a session to be in memory before sequence IDs can be assigned. This means per-file ingest must complete before materialization — a two-pass approach per file, or a deferred join.

### Alias Resolution

Apply `--alias` as a string-substitution step in the string interning function before any `ToolCall` is created. Zero overhead at query time.

---

## 5. Visualization

### 5.1 Output Format

A single plain-text format serves both humans and AI agents. No JSON, no ANSI color. Two tab-separated tables, each preceded by a section header.

#### Result Size Distribution

Header row followed by one row per `(server, method)`, sorted by `est_tokens` descending. Values use SI suffixes (k/M) rounded to one decimal place. The `example_large` and `example_error` columns are `file:line` source links (empty cell if not applicable).

```tsv
## Result Size Distribution (chars)
tool  n  p25  p50  p75  p95  max  est_tokens  example_large  example_error
mcp__test-proxy__notion-fetch  90  1.4k  3.6k  5.3k  15.0k  15.0k  87k  82269df7.jsonl:47  
mcp__test-proxy__notion-search  16  2.4k  2.5k  2.8k  3.0k  3.0k  9k  82269df7.jsonl:12  
```

(columns separated by a single tab character in actual output; shown here with spaces)

#### Transition Matrix

Rows are "from" tools, columns are "to" tools, truncated to top-N tools by call count. Probabilities are rounded to 0.01. First column is the row label ("from"), subsequent columns are destinations ("to"). Zero-probability cells are left blank. Tool names truncated to the method component (after the last `__`).

```tsv
## Transitions (from -> to)
from\to  notion-fetch  notion-search  notion-get-users
notion-search  0.78  0.12  0.08
notion-fetch  0.22  0.61
```

(columns separated by a single tab character in actual output; shown here with spaces)

#### Findings

Top-5 findings by token impact, one per line, tab-separated, purely computable fields:

```
## Findings
high_variance_result  mcp__test-proxy__notion-fetch  p95/p50=4.14  example=82269df7.jsonl:47
chained_calls  mcp__test-proxy__notion-search -> mcp__test-proxy__notion-fetch  prob=0.78  n=70
```

(columns separated by a single tab character in actual output; shown here with spaces)

---

## 6. Automated Findings

Findings are emitted in priority order — highest estimated token savings first. Each finding maps to exactly one `type` string and is derivable purely from the collected statistics; no content inspection required.

| Type | Trigger | Signal |
|------|---------|--------|
| `high_result_volume` | Tool's `est_total_tokens` is in the top-3 and exceeds 10k | Biggest optimization targets by absolute spend |
| `high_variance_result` | `p95 / p50 > 3.0` for a tool with `n >= 10` | Large optional/conditional fields bloating some responses; example link provided |
| `dominated_result` | One tool accounts for > 50% of all estimated tokens | Single point of leverage |
| `chained_calls` | Transition probability A → B > 0.70 with `n >= 5` | Near-deterministic sequence; candidate for a combined tool |
| `high_retry_rate` | Retry rate > 0.20 for a tool with `n >= 5` | Repeated failures; MCP may need better error messages or the agent prompt needs guidance |
| `always_errors` | Error rate > 0.50 for a tool with `n >= 3` | Tool is mostly broken in practice |
| `expensive_model` | > 30% of calls to a high-result-volume tool come from Opus | Cheaper model may suffice for this tool's use case |
| `single_call_sequences` | > 60% of sequences contain exactly one tool call | Agent is making many small round-trips; batching opportunity |

A finding is suppressed if the underlying `n` is below the threshold — low-sample statistics are noise. The top-5 findings by estimated token impact are included in the output.

---

## 7. CLI Interface

```
claudenlos [--alias mcp_a,mcp_b,mcp_c] [--chars-per-token 3.5] [PATH ...]

PATH: one or more ~/.claude/projects directories. Defaults to ~/.claude/projects.
--alias: comma-separated list; first name is canonical, rest are aliases.
         Repeatable.
--verbose (-v): set log level to DEBUG.
```

### Logging

Use the stdlib `logging` module. Two loggers:

- `claudenlos` — application-level events (startup, file count, alias resolution, findings). Default level `WARNING`; `-v` sets it to `DEBUG`.
- `claudenlos.ingest` — per-file debug output (lines parsed, UUIDs skipped, sequence counts). Only active at `DEBUG`.

All log output goes to stderr so stdout stays clean for TSV output. No third-party logging library needed; stdlib is sufficient for a single-process CLI.

### Progress Bar

Use [`rich.progress`](https://rich.readthedocs.io/en/stable/progress.html) for the ingest progress bar. It handles asyncio cleanly, outputs to stderr, and renders well in both terminals and CI (auto-detects TTY and falls back to plain text).
