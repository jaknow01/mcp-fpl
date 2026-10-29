"""Self-service API keys issued by /join — one active key per person.

A key links a classic FPL team (``entry_id`` + ``league_id``), an FPL Draft team
(``draft_entry_id`` + ``draft_league_id``), or both. Issuing a key revokes every active key
that shares either team.

Stored as SHA-256 only, in Postgres (``mcp_api_keys``) when ``DATABASE_URL`` is set, else in
``$MCP_FPL_DATA_DIR/keys.json`` (default ``data/``). Issuing a key for an entry revokes its
previous one. ``issued_clients()`` feeds the kit's ClientRegistry, so these keys work like
keys from the clients file, with the linked teams and leagues as the caller's FPL defaults.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from jarvis_mcp_kit import Client, require_admin

from mcp_fpl.server import server

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonKeyStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path(os.getenv("MCP_FPL_DATA_DIR") or "data") / "keys.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"keys": []}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Key store %s unreadable: %s", self.path, e)
            return {"keys": []}
        return data if isinstance(data, dict) and isinstance(data.get("keys"), list) else {"keys": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _revoke(data: dict, entry_id: Optional[int], draft_entry_id: Optional[int]) -> int:
        n = 0
        for row in data["keys"]:
            if row.get("revoked_at"):
                continue
            if ((entry_id is not None and row.get("entry_id") == entry_id)
                    or (draft_entry_id is not None and row.get("draft_entry_id") == draft_entry_id)):
                row["revoked_at"] = _now()
                n += 1
        return n

    def issue(self, *, key_sha256: str, entry_id: Optional[int] = None, league_id: Optional[int] = None,
              draft_entry_id: Optional[int] = None, draft_league_id: Optional[int] = None,
              manager_name: str = "", team_name: Optional[str] = None,
              draft_team_name: Optional[str] = None, servers: list[str]) -> int:
        if entry_id is None and draft_entry_id is None:
            raise ValueError("a key needs a classic or a draft team")
        with self._lock:
            data = self._load()
            replaced = self._revoke(data, entry_id, draft_entry_id)
            data["keys"].append({"key_sha256": key_sha256, "entry_id": entry_id, "league_id": league_id,
                                 "draft_entry_id": draft_entry_id, "draft_league_id": draft_league_id,
                                 "manager_name": manager_name, "team_name": team_name,
                                 "draft_team_name": draft_team_name,
                                 "servers": list(servers), "created_at": _now(), "revoked_at": None})
            self._save(data)
            return replaced

    def active(self) -> list[dict]:
        return [r for r in self._load()["keys"] if not r.get("revoked_at")]

    def revoke_entry(self, entry_id: int, game: str = "classic") -> int:
        with self._lock:
            data = self._load()
            n = (self._revoke(data, None, entry_id) if game == "draft"
                 else self._revoke(data, entry_id, None))
            if n:
                self._save(data)
            return n


class PostgresKeyStore:
    def issue(self, **kwargs) -> int:
        from mcp_fpl.db import key_repo
        return key_repo.issue(**kwargs)

    def active(self) -> list[dict]:
        from mcp_fpl.db import key_repo
        return key_repo.active()

    def revoke_entry(self, entry_id: int, game: str = "classic") -> int:
        from mcp_fpl.db import key_repo
        return key_repo.revoke_entry(entry_id, game)


_store = None


def key_store():
    """The store chosen on first use: Postgres when DATABASE_URL is set, else JSON."""
    global _store
    if _store is None:
        from mcp_fpl.db import connection as dbconn
        _store = PostgresKeyStore() if dbconn.is_configured() else JsonKeyStore()
        logger.info("Issued-key store: %s", type(_store).__name__)
    return _store


def set_key_store(store) -> None:
    """Override the store (tests)."""
    global _store
    _store = store


def _int_or_none(value) -> Optional[int]:
    return int(value) if value is not None and str(value).strip().lstrip("-").isdigit() else None


def client_from_row(row: dict) -> Client:
    entry = _int_or_none(row.get("entry_id"))
    draft_entry = _int_or_none(row.get("draft_entry_id"))
    teams = [t for t in (row.get("team_name"), row.get("draft_team_name")) if t]
    name = row.get("manager_name") or (f"FPL {entry}" if entry is not None else f"FPL Draft {draft_entry}")
    if teams:
        name = f"{name} ({' / '.join(teams)})"
    # The classic team keeps its historic id (fpl-<entry>); a draft-only key gets fpl-d<entry>.
    client_id = f"fpl-{entry}" if entry is not None else f"fpl-d{draft_entry}"
    settings = {"entry_id": entry, "league_id": _int_or_none(row.get("league_id")),
                "draft_entry_id": draft_entry, "draft_league_id": _int_or_none(row.get("draft_league_id"))}
    return Client(client_id=client_id, name=name, key_sha256=str(row["key_sha256"]).lower(),
                  servers=frozenset(row.get("servers") or ["fpl"]),
                  settings={"fpl": settings}, source="join")


def issued_clients() -> list[Client]:
    return [client_from_row(r) for r in key_store().active()]


@server.tool(read_only=True, open_world=False, admin_only=True)
async def list_issued_keys() -> dict:
    """
    Description:
        Admin only. Lists the active self-service keys issued by the /join page: manager, the
        linked classic team (entry id, league) and/or FPL Draft team (draft entry id, draft
        league), and when each key was issued (never the keys themselves).

    Output:
        JSON with `count` and `keys` (manager_name, team_name, entry_id, league_id,
        draft_team_name, draft_entry_id, draft_league_id, created_at).
    """
    require_admin()
    rows = key_store().active()
    fields = ("manager_name", "team_name", "entry_id", "league_id",
              "draft_team_name", "draft_entry_id", "draft_league_id", "created_at")
    keys = [{k: r.get(k) for k in fields} for r in rows]
    return {"count": len(keys), "keys": keys}


@server.tool(read_only=False, destructive=True, open_world=False, admin_only=True)
async def revoke_issued_key(entry_id: int, game: Literal["classic", "draft"] = "classic") -> dict:
    """
    Description:
        Admin only. Revokes the active self-service key linked to an FPL team; requests with
        it are rejected within the key refresh interval (about 15 seconds).

    Parameters:
    entry_id: int
        The team (entry) id whose key to revoke.

    game: "classic" | "draft" = "classic"
        Whether entry_id is a classic FPL team or an FPL Draft team (separate id spaces).

    Output:
        JSON with `revoked` (how many keys were revoked, 0 if the team had none).
    """
    require_admin()
    n = key_store().revoke_entry(int(entry_id), game)
    server.registry.invalidate()
    logger.info("Revoked %d self-service key(s) for %s entry %s", n, game, entry_id)
    return {"revoked": n, "entry_id": int(entry_id), "game": game}
