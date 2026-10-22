"""Self-service API keys issued by /mcp/join — one active key per FPL entry.

Stored as SHA-256 only, in Postgres (``mcp_api_keys``) when ``DATABASE_URL`` is set, else in
``$MCP_FPL_DATA_DIR/keys.json`` (default ``data/``). Issuing a key for an entry revokes its
previous one. ``issued_clients()`` feeds the kit's ClientRegistry, so these keys work like
keys from the clients file, with the entry and league as the caller's FPL defaults.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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

    def _revoke(self, data: dict, entry_id: int) -> int:
        n = 0
        for row in data["keys"]:
            if row.get("entry_id") == entry_id and not row.get("revoked_at"):
                row["revoked_at"] = _now()
                n += 1
        return n

    def issue(self, *, key_sha256: str, entry_id: int, league_id: Optional[int], manager_name: str,
              team_name: str, servers: list[str]) -> int:
        with self._lock:
            data = self._load()
            replaced = self._revoke(data, entry_id)
            data["keys"].append({"key_sha256": key_sha256, "entry_id": entry_id, "league_id": league_id,
                                 "manager_name": manager_name, "team_name": team_name,
                                 "servers": list(servers), "created_at": _now(), "revoked_at": None})
            self._save(data)
            return replaced

    def active(self) -> list[dict]:
        return [r for r in self._load()["keys"] if not r.get("revoked_at")]

    def revoke_entry(self, entry_id: int) -> int:
        with self._lock:
            data = self._load()
            n = self._revoke(data, entry_id)
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

    def revoke_entry(self, entry_id: int) -> int:
        from mcp_fpl.db import key_repo
        return key_repo.revoke_entry(entry_id)


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


def client_from_row(row: dict) -> Client:
    entry = int(row["entry_id"])
    name = row.get("manager_name") or f"FPL {entry}"
    if row.get("team_name"):
        name = f"{name} ({row['team_name']})"
    return Client(client_id=f"fpl-{entry}", name=name, key_sha256=str(row["key_sha256"]).lower(),
                  servers=frozenset(row.get("servers") or ["fpl"]),
                  settings={"fpl": {"entry_id": entry, "league_id": row.get("league_id")}}, source="join")


def issued_clients() -> list[Client]:
    return [client_from_row(r) for r in key_store().active()]


@server.tool(read_only=True, open_world=False)
async def list_issued_keys() -> dict:
    """
    Description:
        Admin only. Lists the active self-service keys issued by the /mcp/join page: team,
        manager, entry id, league and when each key was issued (never the keys themselves).

    Output:
        JSON with `count` and `keys` (team_name, manager_name, entry_id, league_id, created_at).
    """
    require_admin()
    rows = key_store().active()
    keys = [{k: r.get(k) for k in ("team_name", "manager_name", "entry_id", "league_id", "created_at")}
            for r in rows]
    return {"count": len(keys), "keys": keys}


@server.tool(read_only=False, destructive=True, open_world=False)
async def revoke_issued_key(entry_id: int) -> dict:
    """
    Description:
        Admin only. Revokes the active self-service key of an FPL entry; requests with it are
        rejected within the key refresh interval (about 15 seconds).

    Parameters:
    entry_id: int
        The FPL entry (team) id whose key to revoke.

    Output:
        JSON with `revoked` (how many keys were revoked, 0 if the entry had none).
    """
    require_admin()
    n = key_store().revoke_entry(int(entry_id))
    server.registry.invalidate()
    logger.info("Revoked %d self-service key(s) for entry %s", n, entry_id)
    return {"revoked": n, "entry_id": int(entry_id)}
