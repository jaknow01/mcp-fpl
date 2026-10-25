"""`mcp-fpl` command line.

    mcp-fpl serve                       run the MCP server + /join (default)
    mcp-fpl collect [--league ID ...]   today's league history snapshot (CronJob 04:00)
    mcp-fpl warm-cache [--league ID]    refresh the current gameweek's squad scans (every 3 h)
    mcp-fpl backfill [--league ID] [--from-gw N]   rebuild history from the FPL API
    mcp-fpl keys list | keys revoke ENTRY_ID       self-service keys from /join
"""

import argparse
import asyncio
import sys

from jarvis_mcp_kit import run, setup_logging

from mcp_fpl.server import server


def create_app(**join_options):
    from mcp_fpl import keys, tools  # noqa: F401  (register the tools on `server`)
    from mcp_fpl.join import build_join_routes

    return server.app(extra_routes=build_join_routes(server.registry, keys.key_store(), **join_options))


def _keys(args) -> int:
    from mcp_fpl.keys import key_store

    store = key_store()
    if args.action == "list":
        rows = store.active()
        for r in rows:
            print(f"{r.get('entry_id'):>9}  {r.get('team_name') or '?':<28} {r.get('manager_name') or '?':<24} "
                  f"league={r.get('league_id')}  since {str(r.get('created_at'))[:10]}")
        print(f"{len(rows)} active key(s)")
        return 0
    if not args.entry_id:
        print("usage: mcp-fpl keys revoke ENTRY_ID", file=sys.stderr)
        return 2
    print(f"revoked {store.revoke_entry(int(args.entry_id))} key(s) for entry {args.entry_id}")
    return 0


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-fpl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server (default)")
    for name in ("collect", "warm-cache", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--league", type=int, action="append", help="league id (default: FPL_LEAGUES)")
        if name == "backfill":
            p.add_argument("--from-gw", type=int, default=1)
    p_keys = sub.add_parser("keys", help="self-service keys")
    p_keys.add_argument("action", choices=["list", "revoke"])
    p_keys.add_argument("entry_id", nargs="?")
    args = parser.parse_args(argv)
    setup_logging()

    if args.cmd in (None, "serve"):
        run(create_app())
        return
    from mcp_fpl import jobs

    if args.cmd == "collect":
        code = asyncio.run(jobs.collect(args.league))
    elif args.cmd == "warm-cache":
        code = asyncio.run(jobs.warm_cache(args.league))
    elif args.cmd == "backfill":
        code = asyncio.run(jobs.backfill(args.league, args.from_gw))
    else:
        code = _keys(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
