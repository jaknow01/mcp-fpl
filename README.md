# mcp-fpl

Fantasy Premier League tools as an [MCP](https://modelcontextprotocol.io) server — plus a
small page where members of your mini-league get their own API key, already linked to their
team. Data comes live from the keyless public FPL API.

| Tool | |
|---|---|
| `get_fpl_fixtures` | gameweek schedule with FDR difficulty and kickoff times |
| `get_fpl_live` | live scores, events, and how *your* players are doing right now |
| `get_pl_teams` | Premier League teams and strength ratings |
| `get_fpl_player` | any player: price, form, ownership, availability, next fixtures |
| `get_my_fpl_squad` | your picked squad for a gameweek |
| `get_my_fpl_leagues` | your classic mini-leagues |
| `get_fpl_league_standings` | a mini-league table |
| `get_league_history` | how standings/squads in a league changed over time (needs Postgres + `collect`) |
| `who_owns_player_in_league` / `get_league_ownership` | ownership inside a mini-league |
| `whoami` | your key's client and default team/league |
| `list_issued_keys` / `revoke_issued_key` | admin only |

Endpoints: `/mcp` (key in header) or `/mcp/<key>`, `/join`; the longer `/mcp/fpl[/<key>]`
and `/mcp/join` keep working.

**"My" team and league** come from the caller's key: `fpl: {entry_id, league_id}` in the
clients file, or the team a `/join` key was issued for. An explicit `entry_id` /
`league_id` argument always wins. There is no server-wide default, so nobody gets someone
else's team by accident.

Part of the Jarvis tool servers; built on
[jarvis-mcp-kit](https://github.com/jaknow01/jarvis-mcp-kit) (API keys, rate limits, logs).

## Self-service keys — `/join`

Members of the leagues in `FPL_LEAGUES` open `https://<host>/join`, type their entry id,
team name or name, and get a key with a ready-to-paste connector URL and Claude Code command.
No proof of ownership (league tables are public) — instead: one active key per team (a new one
revokes the old, after a confirmation), 10 attempts per IP per hour, and a notification for
every key to `JOIN_NOTIFY_WEBHOOK_URL`. Revoke with the admin tool or
`mcp-fpl keys revoke <entry_id>`; list with `mcp-fpl keys list`.

## Storage and jobs

With `DATABASE_URL` (its own Postgres database and login) the server caches reference data and
league squad scans, keeps a permanent daily league history, and stores issued keys. Without it
caches stay in memory, history is unavailable and keys go to `$MCP_FPL_DATA_DIR/keys.json`.

```bash
mcp-fpl collect          # today's league snapshot for every league in FPL_LEAGUES (cron 04:00)
mcp-fpl warm-cache       # refresh the current gameweek's squad scans (every few hours)
mcp-fpl backfill         # rebuild per-gameweek history from the FPL API (once, after deploy)
```

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
| `FPL_LEAGUES` | leagues to collect/warm and to accept on `/join` (comma list) |
| `DATABASE_URL` | Postgres for caches, history, issued keys |
| `MCP_PUBLIC_URL` | base URL shown on the join page |
| `JOIN_NOTIFY_WEBHOOK_URL` / `JOIN_NOTIFY_TOKEN` | where to POST `{"text": …}` for each issued key |
| `MCP_JOIN_ENABLED`, `MCP_JOIN_PER_HOUR`, `MCP_JOIN_ROSTER_TTL_SECONDS` | join page switches |
| `FPL_CACHE_TTL_SECONDS`, `FPL_LEAGUE_CACHE_TTL_SECONDS` | cache freshness |
