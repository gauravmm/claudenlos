# Claudenlos

Our goal is to waste less money by analyzing MCPs and tools we use with Claude and look for opportunities to save on tokens. Claudenlos is a static analyzer that identifies opportunities for you (and your favourite AI agent) to automatically trawl through your interaction data and quickly find these opportunities.

Once you have them, you can modify or rewrite existing MCPs using [an MCP Gateway with rewriting skills](https://github.com/gauravmm/mcp_gateway_maker/).

Examples of savings realized through this:

1. Project management tool Hive [dumps ~8.5k tokens into your context when 3k is sufficient](https://www.gauravmanek.com/blog/2026/jqi/). The difference was just omitting blank or default values. That's it.
2. Zoho Mail's MCP requires your agent to copy base64-encoded email attachments. That's tens of thousands of tokens when a file path would be sufficient. This is an egregious waste.

## Usage

```
uv run claudenlos [--alias a,b,c] [--chars-per-token 3.5] [-v] [PATH ...]
```

**PATH** — one or more `projects` directories to analyse. Defaults to all `~/.claude*/projects` directories found on the system (covering primary, secondary, and any other Claude profiles).

**`--alias a,b,c`** — treat MCP server names `b` and `c` as aliases for `a`. Repeatable. Useful when the same MCP is registered under different names across profiles.

**`--chars-per-token N`** — characters-per-token ratio used to estimate result token costs (default: 3.5).

**`--min-count N`** — exclude tools called fewer than N times from all analysis (default: 5). Use `--min-count 1` to include every tool. The output header notes how many tools were excluded.

**`-v`** — verbose logging to stderr.

Output goes to stdout as tab-separated text; timing goes to stderr. Pipe into a file or a spreadsheet:

```sh
uv run claudenlos > report.tsv
uv run claudenlos --alias gateway,test-proxy > report.tsv
```

### Directory discovery

By default, claudenlos globs `~/.claude*/projects` and analyses every matching directory. On a machine with a primary and a secondary Claude profile this picks up both automatically:

```
~/.claude/projects
~/.claude-secondary/projects
```

Pass explicit paths to override:

```sh
uv run claudenlos ~/.claude/projects ~/.claude-work/projects
```

## Analysis

The tool reads JSONL conversation files from the discovered directories and assembles statistics on tool and MCP usage. From that, we produce:

1. **(MCP, tool) call statistics** — result-size distribution (p25/p50/p75/p95/max), estimated token cost, call count.

2. **Call sequence analysis** — a *sequence* is a maximal run of tool calls with no human input interleaving.
   - Transition matrix: how often is tool B called immediately after tool A?
   - Retry rate: how often is tool A called again after it fails?

3. **Automated findings** — all opportunities above per-type thresholds, ranked by estimated token impact: high-volume tools, high-variance results, near-deterministic call chains (≥ 80% probability), high retry rates, broken tools, expensive-model usage, single-call sequence patterns. Each finding includes a file:line pointer to an example call.

You may need to deduplicate MCP names, which you can specify on the command line with `--alias mcp_1,mcp_2,mcp_3`. This treats all named MCPs as the first.
