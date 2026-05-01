# Claudenlos

Our goal is to waste less money by analyzing MCPs and tools we use with Claude and look for opportunities to save on tokens. Claudenlos is a static analyzer that identifies opportunities for you (and your favourite AI agent) to automatically trawl through your interaction data and quickly find these opportunities.

Once you have them, you can modify or rewrite existing MCPs using [an MCP Gateway with rewriting skills](https://github.com/gauravmm/mcp_gateway_maker/).

Examples of savings realized through this:

    1. Project management tool Hive [dumps ~8.5k tokens into your context when 3k is sufficient](https://www.gauravmanek.com/blog/2026/jqi/). The difference was just omitting blank or default values. That's it.
    2. Zoho Mail's MCP requires your agent to copy base64-encoded email attachments. That's tens of thousands of tokens when a file path would be sufficient. This is an egregious waste.

## Analysis

The tool is very simple, it accepts one or more `.claude/projects` directories and reads the data there, assembling an idea of what MCPs are called and in what order. From that, we assemble:

1. (MCP, tool) calls.
    a. Distribution of tokens,
    b. Frequency of calling, and
    c. Total tokens spent on MCP calls and responses.
    d. What were models used when the call was performed?

2. Call sequence analysis
    a. A sequence is a set of tool calls that happen contiguously, without human input interleaving.
    b. Once you call a tool, what is the distribution of the remaining length of the session
    c. In a sequence, how often do you call tool B after tool A
    d. How often do you call tool A after a call to tool A fails.

You may need to deduplicate MCP names, which you can specify on the commandline with `--alias mcp_1,mcp_2,mcp_3`. This treats all named mcps as the first.
