# QueryDoctor MCP server

QueryDoctor's tools are available to any MCP client over stdio: Claude Code, Claude Desktop, Cursor, and others. The server is `querydoctor/mcp_server.py`, built on the official `mcp` Python SDK v2 (`MCPServer`).

## Tools
| Tool | What it does | Writes anything? |
|---|---|---|
| `list_slow_queries(limit)` | Slowest workload queries (pg_stat_statements, measured), mapped to `q01`–`q22` | no |
| `explain_query(query)` | Plan summary (planner estimates) + tables and existing indexes | no |
| `what_if_index(query, index_sql)` | HypoPG estimate for one `CREATE INDEX` (safety-checked, never created) | no |
| `index_hygiene()` | Unused, duplicate and redundant indexes + suggested DROP/recreate SQL | no |
| `recommend_indexes(query, threshold?, max_iterations?, real_validate?, calls_per_day?)` | Runs the agent and **stops at human approval**; returns `thread_id` | no |
| `recommend_workload(top_n?, max_indexes?, min_gain_pct?, real_validate?, calls_per_day?)` | Workload mode, stops at approval | no |
| `approve_recommendation(thread_id, approved, feedback?)` | Resumes with the **user's** decision; returns report + migration/rollback SQL | no (\*) |

(\*) Only optional real validation (`real_validate=true`, `ENABLE_REAL_VALIDATION=1`, local DB) builds an index, inside a transaction that is always rolled back. QueryDoctor never runs the migration; humans do.

## Claude Code
```bash
claude mcp add querydoctor -- /Users/rony/Desktop/101/MLH_Hack/QueryDoctor/.venv/bin/python -m querydoctor.mcp_server
```
Run it from the project directory (or set `cwd`), so `.env` and `data/query_map.json` are found.

## Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "querydoctor": {
      "command": "/Users/rony/Desktop/101/MLH_Hack/QueryDoctor/.venv/bin/python",
      "args": ["-m", "querydoctor.mcp_server"],
      "cwd": "/Users/rony/Desktop/101/MLH_Hack/QueryDoctor"
    }
  }
}
```

## Demo prompt
> "Why is q14 slow, and what index would help? Show me the estimate before approving anything."

Expected flow: `explain_query` → `recommend_indexes` → the client shows the approval request → you approve → `approve_recommendation` returns the migration and rollback SQL.
