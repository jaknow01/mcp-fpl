# mcp-fpl

Fantasy Premier League tools as an [MCP](https://modelcontextprotocol.io) server — for classic
FPL and FPL Draft — plus a small page where members of your leagues get their own API key,
already linked to their team(s). Data comes live from the keyless public FPL and FPL Draft APIs.

| Tool | |
|---|---|
| `get_fpl_fixtures` | gameweek schedule with FDR difficulty and kickoff times |
| `get_fpl_live` | live scores, events, and how *your* players are doing right now |
| `get_pl_teams` | Premier League teams and strength ratings |
| `get_fpl_player` | any player: price, form, ownership, availability, next fixtures |
| `get_my_fpl_squad` | your picked squad for a gameweek (Draft: XI + bench, points, auto subs) |
| `get_my_fpl_leagues` | your classic and/or Draft mini-leagues |
| `get_fpl_league_standings` | a mini-league table (Draft H2H: plus the gameweek's matches) |
| `get_league_history` | how standings/squads in a classic league changed over time (needs Postgres + `collect`) |
| `who_owns_player_in_league` | who has a player in a mini-league (Draft: the owner, or that he is free) |
| `get_league_ownership` | most-owned / most-captained players in a classic mini-league |
| `get_draft_free_agents` | Draft: best unowned players (waiver / free-agent targets) |
| `get_draft_activity` | Draft: waivers and free-agent moves, trades, draft picks |
| `whoami` | your key's client and default teams/leagues |
| `list_issued_keys` / `revoke_issued_key` | admin only |

Endpoints: `/mcp` (key in header) or `/mcp/<key>`, `/join`; the longer `/mcp/fpl[/<key>]`
and `/mcp/join` keep working.

**"My" team and league** come from the caller's key: `fpl: {entry_id, league_id}` (classic)
and/or `fpl: {draft_entry_id, draft_league_id}` (Draft) in the clients file, or the team(s) a
`/join` key was issued for. An explicit `entry_id` / `league_id` argument always wins. There is
no server-wide default, so nobody gets someone else's team by accident.

**Classic vs Draft** are separate games with separate ids. The tools that serve both take
`game="classic"|"draft"`; without it they use the one game the key is linked to, and a key
linked to both asks for it. Player ids differ between the two APIs for some players, so they
are matched on the player's `code`.

Part of the Jarvis tool servers; built on
[jarvis-mcp-kit](https://github.com/jaknow01/jarvis-mcp-kit) (API keys, rate limits, logs).

## Self-service keys — `/join`

Members of the leagues in `FPL_LEAGUES` (classic) and `FPL_DRAFT_LEAGUES` (Draft) open
`https://<host>/join` and enter their team — classic, Draft or both, in two separate fields —
as the team's id or its exact name (case-insensitive; no partial matches, no candidate lists).
They get one key linked to those teams, with setup for Claude, ChatGPT, the OpenAI API, Cursor,
VS Code, Gemini CLI and self-hosted assistants.

One key per team: a team that already has a key gets none, and the page answers that with the
same message as an unknown team, so it never reveals which teams are taken. A lost key is
replaced after the admin revokes it (admin tool `revoke_issued_key` or
`mcp-fpl keys revoke <entry_id> [--draft]`; list with `mcp-fpl keys list`). No proof of
ownership (league tables are public) — instead: 10 attempts per IP per hour and a notification
for every key to `JOIN_NOTIFY_WEBHOOK_URL`.

## Storage and jobs

With `DATABASE_URL` (its own Postgres database and login) the server caches reference data and
league squad scans, keeps a permanent daily league history, stores issued keys, and logs every
tool call to `fpl_tool_calls` (client, tool, arguments, outcome, error, duration, result size,
server version — not the results, and never the users' conversations, which the server does
not see) to show how the tools are used and where they fail. Without it
caches stay in memory, history is unavailable and keys go to `$MCP_FPL_DATA_DIR/keys.json`.

```bash
mcp-fpl collect          # today's league snapshot for every league in FPL_LEAGUES (cron 04:00)
mcp-fpl warm-cache       # refresh the current gameweek's squad scans (every few hours)
mcp-fpl backfill         # rebuild per-gameweek history from the FPL API (once, after deploy)
mcp-fpl calls --days 7   # tool-call log: calls, clients, failures and timing per tool
```

Logged calls are kept for good; with `MCP_CALL_LOG_DAYS` set, `collect` deletes older ones.

## Run

```bash
cp .env.example .env                              # FPL_LEAGUES, DATABASE_URL, MCP_PUBLIC_URL, …
cp config/clients.example.yaml config/clients.yaml
uv run jarvis-mcp-keys new me --servers fpl --set fpl.entry_id=1234567 --set fpl.league_id=765432
uv run --env-file .env mcp-fpl serve              # http://localhost:8010/mcp, /join
```

Kubernetes: `deploy/kubernetes.yaml` is a generic example (Deployment, Service, CronJobs,
Ingress); give the server its own hostname and expose `/mcp` and `/join`.

## Connect

- **Claude app (custom connector):** easiest via `/join`; or URL `https://<host>/mcp/jvk_…`
- **Claude Code:** `claude mcp add --transport http fpl https://<host>/mcp --header "Authorization: Bearer jvk_…"`
- **Jarvis:** an entry in its `config/subagents.yaml`.

| Variable | |
|---|---|
| `FPL_LEAGUES` | classic leagues to collect/warm and to accept on `/join` (comma list) |
| `FPL_DRAFT_LEAGUES` | FPL Draft leagues to accept on `/join` (comma list; no field on the page when empty) |
| `DATABASE_URL` | Postgres for caches, history, issued keys |
| `MCP_PUBLIC_URL` | base URL shown on the join page |
| `JOIN_NOTIFY_WEBHOOK_URL` / `JOIN_NOTIFY_TOKEN` | where to POST `{"text": …}` for each issued key |
| `MCP_JOIN_ENABLED`, `MCP_JOIN_PER_HOUR`, `MCP_JOIN_ROSTER_TTL_SECONDS` | join page switches |
| `FPL_CACHE_TTL_SECONDS`, `FPL_LEAGUE_CACHE_TTL_SECONDS` | cache freshness |
| `MCP_CALL_LOG`, `MCP_CALL_LOG_DAYS` | tool-call log on/off (default on with a database) and optional retention in days (default: keep all) |
