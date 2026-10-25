"""Self-service key page: ``GET/POST /join`` (alias ``/mcp/join``) (see docs/MCP.md).

A manager of one of the configured FPL leagues types their entry id, team name or own name,
and gets an API key for this server — already linked to their entry and league, with
ready-to-paste setup for several MCP clients (Claude, ChatGPT, OpenAI API, Cursor, VS Code,
Gemini CLI, self-hosted assistants, anything else that speaks MCP over HTTP), shown as tabs with Claude first.

There is deliberately NO proof of ownership: league tables are public, so this is a
convenience gate ("only people from my league"), not authentication. What keeps it sane:
one active key per entry (a new one revokes the old — that is also how a lost key is
replaced), a confirmation step before replacing a key, a per-IP hourly limit, and a
notification to the owner for every key issued (``JOIN_NOTIFY_WEBHOOK_URL``; revoke with the
admin tool ``revoke_issued_key`` or ``mcp-fpl keys revoke <entry>``).

Leagues: ``FPL_LEAGUES`` (comma list). Switch:
``MCP_JOIN_ENABLED`` (default on). Public base URL for the instructions:
``MCP_PUBLIC_URL`` or else derived from the proxy headers.
"""

import asyncio
import html
import json
import logging
import os
import time
import unicodedata
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

import httpx2
from jarvis_mcp_kit import ClientRegistry, hash_key, new_key

from mcp_fpl.config import env_flag, env_int, env_str, league_ids
from mcp_fpl.fpl_api import list_league_managers

logger = logging.getLogger(__name__)

_MAX_LEAGUE_MANAGERS = 500
_background: set[asyncio.Task] = set()


def join_enabled() -> bool:
    return env_flag("MCP_JOIN_ENABLED", True)


def join_league_ids() -> list[int]:
    return league_ids()


# ── league roster ──────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Case-, accent- and spacing-insensitive form for name matching ('Łukasz' ~ 'lukasz')."""
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


class LeagueRoster:
    """Managers of the join leagues: ``{entry_id: {entry, team, manager, league_id, league}}``.
    Cached ``MCP_JOIN_ROSTER_TTL_SECONDS`` (default 600); refreshed early (at most once a
    minute) when a lookup misses, so someone who just joined the league isn't stuck."""

    def __init__(self, fetch_managers: Callable[..., Awaitable] = list_league_managers,
                 league_ids: Optional[list[int]] = None):
        self._fetch = fetch_managers
        self._league_ids = league_ids  # None → join_league_ids() from the env, read per load
        self._members: dict[int, dict] = {}
        self._loaded_at: Optional[float] = None
        self._lock = asyncio.Lock()

    async def members(self, *, refresh_if_older_than: Optional[float] = None) -> dict[int, dict]:
        ttl = env_int("MCP_JOIN_ROSTER_TTL_SECONDS", 600, minimum=0)
        age = None if self._loaded_at is None else time.monotonic() - self._loaded_at
        stale = age is None or age >= ttl or (refresh_if_older_than is not None and age >= refresh_if_older_than)
        if stale:
            async with self._lock:
                age = None if self._loaded_at is None else time.monotonic() - self._loaded_at
                if age is None or age >= ttl or (refresh_if_older_than is not None and age >= refresh_if_older_than):
                    await self._load()
        return self._members

    async def _load(self) -> None:
        members: dict[int, dict] = {}
        for league_id in (self._league_ids if self._league_ids is not None else join_league_ids()):
            try:
                league_name, rows, _complete = await self._fetch(league_id, _MAX_LEAGUE_MANAGERS)
            except Exception as e:
                logger.error("Join page: could not load league %s: %s", league_id, e)
                continue
            for row in rows:
                entry = row.get("entry")
                if entry is None or entry in members:
                    continue
                members[int(entry)] = {
                    "entry": int(entry), "team": row.get("entry_name") or "",
                    "manager": row.get("player_name") or "", "league_id": league_id,
                    "league": league_name or str(league_id),
                }
        if members or self._loaded_at is None:
            self._members = members
        self._loaded_at = time.monotonic()


def match_members(query: str, members: dict[int, dict]) -> list[dict]:
    """Entry id → that entry; else exact team/manager name; else substring matches."""
    q = (query or "").strip()
    if not q:
        return []
    if q.isdigit():
        hit = members.get(int(q))
        return [hit] if hit else []
    nq = _norm(q)
    exact = [m for m in members.values() if nq in (_norm(m["team"]), _norm(m["manager"]))]
    if exact:
        return exact
    return [m for m in members.values() if nq in _norm(m["team"]) or nq in _norm(m["manager"])]


# ── owner notification ─────────────────────────────────────────────────────────

async def notify_owner(text: str) -> None:
    """Best-effort POST ``{"text": …}`` to ``JOIN_NOTIFY_WEBHOOK_URL`` (bearer
    ``JOIN_NOTIFY_TOKEN``) — e.g. Jarvis's /internal/notify, which forwards it to the owner."""
    logger.info("Join notice: %s", text)
    url = env_str("JOIN_NOTIFY_WEBHOOK_URL")
    if not url:
        return
    headers = {}
    token = env_str("JOIN_NOTIFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx2.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"text": text, "source": "mcp-fpl"}, headers=headers)
        resp.raise_for_status()


# ── page ───────────────────────────────────────────────────────────────────────

_e = html.escape

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--muted:#5d6570;--accent:#37003c;--ok:#0a7a3d;--warn:#9a5b00;--border:#dde1e6}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--fg:#e8eaed;--muted:#a0a7b1;--accent:#e90052;--ok:#3ccf7d;--warn:#f0b04a;--border:#2c3138}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin:16px 0}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 8px}p,li{margin:8px 0;overflow-wrap:anywhere}.muted{color:var(--muted);font-size:14px}
input[type=text]{width:100%;padding:12px;font-size:16px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--fg)}
button{padding:12px 16px;font-size:16px;border:0;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer;margin-top:12px}
button.secondary{background:transparent;color:var(--fg);border:1px solid var(--border)}
code,.box{font-family:ui-monospace,Menlo,monospace;font-size:13px;word-break:break-all}
.box{display:block;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px;margin:6px 0}
.ok{color:var(--ok)}.warn{color:var(--warn)}ol{padding-left:20px}
.choice button{width:100%;text-align:left;margin-top:8px}
pre.box{white-space:pre-wrap;word-break:normal;overflow-wrap:anywhere;margin:6px 0}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 12px}
.tabs button{margin:0;padding:8px 12px;font-size:14px;background:transparent;color:var(--fg);border:1px solid var(--border)}
.tabs button[aria-selected=true]{background:var(--accent);color:#fff;border-color:var(--accent)}
.panel h3{font-size:15px;margin:16px 0 4px}.panel h3:first-child{margin-top:0}
"""

_COPY_JS = """
const tabs = document.querySelectorAll('[role=tab]');
function show(id) {
  for (const t of tabs) {
    const on = t.dataset.tab === id;
    t.setAttribute('aria-selected', on);
    document.getElementById(t.dataset.tab).hidden = !on;
  }
}
for (const t of tabs) t.addEventListener('click', () => show(t.dataset.tab));
if (tabs.length) show(tabs[0].dataset.tab);
for (const b of document.querySelectorAll('[data-copy]')) b.addEventListener('click', async () => {
  const t = document.getElementById(b.dataset.copy).textContent;
  try { await navigator.clipboard.writeText(t); b.textContent = 'Skopiowano ✓'; } catch (e) { b.textContent = 'Zaznacz i skopiuj ręcznie'; }
});
"""

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='pl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>FPL – klucz</title><style>{_CSS}</style></head>"
        f"<body><main><h1>⚽ FPL dla Twojego agenta AI</h1>{body}</main>"
        f"<script>{_COPY_JS}</script></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _form(prefill: str = "", note: str = "") -> str:
    return (
        "<div class='card'>"
        "<p>Podłącz narzędzia FPL (terminarz z FDR, Twoja drużyna, wyniki na żywo, tabela "
        "i własność zawodników w naszej lidze) do swojego agenta AI — działa z każdym agentem, "
        "który obsługuje MCP (Model Context Protocol), w przeglądarce, na telefonie albo w edytorze.</p>"
        f"{note}"
        "<form method='post' action=''>"
        "<label for='team'><b>Twoja drużyna</b></label>"
        f"<input id='team' name='team' type='text' value='{_e(prefill)}' autocomplete='off' required "
        "placeholder='ID drużyny, nazwa drużyny albo imię i nazwisko'>"
        "<p class='muted'>ID to liczba w adresie Twojej drużyny na stronie FPL, np. "
        "…/entry/<b>1234567</b>/event/7</p>"
        "<button type='submit'>Wygeneruj klucz</button></form></div>"
    )


def _member_label(m: dict) -> str:
    return f"{_e(m['team'])} — {_e(m['manager'])} (ID {m['entry']})"


_MAX_CHOICES = 12


def _choices(members: list[dict]) -> str:
    buttons = "".join(
        "<form class='choice' method='post' action=''>"
        f"<input type='hidden' name='team' value='{m['entry']}'>"
        f"<button class='secondary' type='submit'>{_member_label(m)}</button></form>"
        for m in members[:_MAX_CHOICES]
    )
    more = ""
    if len(members) > _MAX_CHOICES:
        more = (f"<p class='muted'>…i {len(members) - _MAX_CHOICES} więcej. Wpisz dokładniejszą "
                "nazwę albo ID drużyny.</p><form method='get' action=''>"
                "<button class='secondary' type='submit'>Szukaj ponownie</button></form>")
    return f"<div class='card'><h2>Którą drużynę masz na myśli?</h2>{buttons}{more}</div>"


def _confirm_replace(m: dict) -> str:
    return (
        "<div class='card'>"
        f"<h2 class='warn'>Ta drużyna ma już klucz</h2><p>{_member_label(m)}</p>"
        "<p>Nowy klucz <b>unieważni poprzedni</b> — wszystkie podłączenia na starym kluczu "
        "przestaną działać. Rób to, jeśli zgubiłeś klucz albo chcesz go zmienić.</p>"
        "<form method='post' action=''>"
        f"<input type='hidden' name='team' value='{m['entry']}'>"
        "<input type='hidden' name='replace' value='1'>"
        "<button type='submit'>Tak, wygeneruj nowy klucz</button></form>"
        "<form method='get' action=''><button class='secondary' type='submit'>Anuluj</button></form>"
        "</div>"
    )


class _Boxes:
    """Copyable code boxes with unique ids on one page."""

    def __init__(self):
        self.n = 0

    def __call__(self, value: str) -> str:
        self.n += 1
        ident = f"b{self.n}"
        return (f"<pre class='box' id='{ident}'>{_e(value)}</pre>"
                f"<button class='secondary' type='button' data-copy='{ident}'>Kopiuj</button>")


def _json(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _python_snippet(endpoint: str, key: str) -> str:
    return (
        "import asyncio\n"
        "from mcp import ClientSession\n"
        "from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client\n\n"
        f'URL = "{endpoint}"\n'
        f'HEADERS = {{"Authorization": "Bearer {key}"}}\n\n'
        "async def main():\n"
        "    async with create_mcp_http_client(headers=HEADERS) as http, \\\n"
        "            streamable_http_client(URL, http_client=http) as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            await session.initialize()\n"
        "            tools = await session.list_tools()\n"
        "            print([t.name for t in tools.tools])\n"
        '            result = await session.call_tool("whoami", {})\n'
        "            print(result.content)\n\n"
        "asyncio.run(main())"
    )


def _provider_tabs(key: str, base: str, box: _Boxes) -> str:
    endpoint = f"{base}/mcp"
    url_with_key = f"{base}/mcp/{key}"
    bearer = {"Authorization": f"Bearer {key}"}
    cli = f'claude mcp add --transport http fpl {endpoint} --header "Authorization: Bearer {key}"'
    openai_tool = {"type": "mcp", "server_label": "fpl", "server_url": endpoint,
                   "headers": bearer, "require_approval": "never"}
    cursor = {"mcpServers": {"fpl": {"url": endpoint, "headers": bearer}}}
    vscode = {"servers": {"fpl": {"type": "http", "url": endpoint, "headers": bearer}}}
    gemini = {"mcpServers": {"fpl": {"httpUrl": endpoint, "headers": bearer}}}

    panels = [
        ("claude", "Claude",
         "<h3>Aplikacja Claude (www, komputer, telefon)</h3><ol>"
         "<li>Na <b>claude.ai</b> albo w aplikacji desktop otwórz <b>Ustawienia → Connectors</b> "
         "→ <b>Add custom connector</b>.</li>"
         "<li>Nazwa: <code>FPL</code>, URL — wklej ten adres (zawiera Twój klucz):</li></ol>"
         f"{box(url_with_key)}"
         "<p class='muted'>Po dodaniu connector działa też w aplikacji mobilnej. "
         "Na telefonie może być potrzebne włączenie go w danej rozmowie.</p>"
         f"<h3>Claude Code</h3>{box(cli)}"),
        ("chatgpt", "ChatGPT",
         "<ol><li>W <b>Ustawieniach → Apps &amp; Connectors → Advanced settings</b> włącz "
         "<b>Developer mode</b>.</li>"
         "<li>Wróć do <b>Apps &amp; Connectors</b> i wybierz <b>Create</b> (dodaj własny connector).</li>"
         "<li>Nazwa: <code>FPL</code>, MCP Server URL — wklej ten adres:</li></ol>"
         f"{box(url_with_key)}"
         "<ol start='4'><li>Authentication: <b>No authentication</b> — klucz jest już w adresie.</li>"
         "<li>W nowej rozmowie włącz connector FPL w menu narzędzi (+).</li></ol>"
         "<p class='muted'>Developer mode jest dostępny w płatnych planach ChatGPT. "
         "Nazwy menu mogą się nieco różnić między wersjami.</p>"),
        ("openai", "OpenAI API",
         "<p>W <b>Responses API</b> dodaj do <code>tools</code> takie narzędzie:</p>"
         f"{box(_json(openai_tool))}"
         "<p class='muted'>Działa tak samo w Agents SDK (<code>HostedMCPTool</code>) "
         "z tymi samymi polami.</p>"),
        ("cursor", "Cursor",
         "<p>Dodaj do <code>~/.cursor/mcp.json</code> (albo <code>.cursor/mcp.json</code> w projekcie):</p>"
         f"{box(_json(cursor))}"),
        ("vscode", "VS Code",
         "<p>GitHub Copilot w trybie Agent: dodaj do <code>.vscode/mcp.json</code> "
         "(albo przez <b>MCP: Add Server…</b> w palecie poleceń):</p>"
         f"{box(_json(vscode))}"),
        ("gemini", "Gemini CLI",
         "<p>Dodaj do <code>~/.gemini/settings.json</code>:</p>"
         f"{box(_json(gemini))}"),
        ("local", "Lokalnie",
         "<p>Asystent uruchomiony u siebie (np. z modelem z Ollamy albo LM Studio) łączy się z tym "
         "serwerem przez internet — model może działać lokalnie, narzędzia FPL są tutaj.</p>"
         "<h3>LM Studio</h3>"
         "<p>Zakładka <b>Program → Install → Edit mcp.json</b>, dodaj:</p>"
         f"{box(_json(cursor))}"
         "<h3>Open WebUI</h3><ol>"
         "<li><b>Admin Settings → External Tools → +</b>, typ <b>MCP (Streamable HTTP)</b>.</li>"
         "<li>URL:</li></ol>"
         f"{box(endpoint)}"
         "<ol start='3'><li>Auth: <b>Bearer</b>, klucz:</li></ol>"
         f"{box(key)}"
         "<h3>Własny kod (Python, oficjalne SDK <code>mcp</code> 2.x)</h3>"
         f"{box(_python_snippet(endpoint, key))}"
         "<p class='muted'>Ollama sama nie obsługuje MCP — użyj jej przez klienta, który to robi "
         "(LM Studio, Open WebUI, własny kod).</p>"),
        ("other", "Inny agent",
         "<p>Każdy klient MCP, który obsługuje transport <b>Streamable HTTP</b>:</p>"
         f"<h3>Adres</h3>{box(endpoint)}"
         f"<h3>Nagłówek</h3>{box(f'Authorization: Bearer {key}')}"
         "<p class='muted'>Zamiast nagłówka można użyć <code>X-API-Key</code>. Jeśli klient nie "
         "pozwala ustawić nagłówków, użyj adresu z kluczem w środku (wtedy bez uwierzytelniania):</p>"
         f"{box(url_with_key)}"),
    ]
    buttons = "".join(
        f"<button role='tab' type='button' data-tab='p-{pid}' aria-controls='p-{pid}' "
        f"aria-selected='{'true' if i == 0 else 'false'}'>{_e(label)}</button>"
        for i, (pid, label, _) in enumerate(panels))
    bodies = "".join(f"<div class='panel' role='tabpanel' id='p-{pid}'>{body}</div>"
                     for pid, _, body in panels)
    return (f"<div class='card'><h2>Podłącz do swojego agenta</h2>"
            f"<div class='tabs' role='tablist'>{buttons}</div>{bodies}</div>")


def _issued(m: dict, key: str, base: str) -> str:
    box = _Boxes()
    return (
        "<div class='card'>"
        f"<h2 class='ok'>Gotowe ✓</h2><p>{_member_label(m)} · liga {_e(m['league'])}</p>"
        "<p class='warn'><b>Zapisz to teraz — klucz pokazujemy tylko raz.</b> "
        "Traktuj go jak hasło i nie wklejaj nigdzie publicznie.</p>"
        f"<p><b>Twój klucz</b></p>{box(key)}</div>"
        f"{_provider_tabs(key, base, box)}"
        "<div class='card'><h2>Spróbuj</h2>"
        "<p>„Jak poszło mojej drużynie w tej kolejce?”, „Kto w naszej lidze ma Haalanda?”, "
        "„Jaki terminarz ma Arsenal na 5 kolejek?”</p>"
        "<p class='muted'>Domyślnie „moja drużyna” to drużyna z tego klucza, a „liga” to nasza liga.</p></div>"
    )


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _public_base(request: Request) -> str:
    configured = env_str("MCP_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


class _HourlyLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = {}

    def allow(self, ip: str, now: Optional[float] = None) -> bool:
        limit = env_int("MCP_JOIN_PER_HOUR", 10, minimum=1)
        now = time.monotonic() if now is None else now
        window = self._hits.setdefault(ip, deque())
        while window and now - window[0] >= 3600:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


class JoinService:
    def __init__(self, registry: ClientRegistry, store: Any, roster: Optional[LeagueRoster] = None,
                 notify: Callable[[str], Awaitable[None]] = notify_owner):
        self.registry = registry
        self.store = store
        self.roster = roster or LeagueRoster()
        self.notify = notify
        self.limiter = _HourlyLimiter()

    async def endpoint(self, request: Request) -> HTMLResponse:
        if not join_enabled() or not join_league_ids():
            return _page("<div class='card'><p>Samodzielne pobieranie kluczy jest wyłączone.</p></div>", 503)
        if request.method != "POST":
            return _page(_form())
        if not self.limiter.allow(_client_ip(request)):
            return _page(_form(note="<p class='warn'>Za dużo prób — spróbuj ponownie za godzinę.</p>"), 429)
        form = await request.form()
        query = str(form.get("team", ""))[:100]
        replace = form.get("replace") == "1"

        members = await self.roster.members()
        found = match_members(query, members)
        if not found:  # maybe they just joined the league: refresh (at most once a minute) and retry
            found = match_members(query, await self.roster.members(refresh_if_older_than=60))
        if not found:
            note = ("<p class='warn'>Nie znalazłem takiej drużyny w naszej lidze. Wpisz ID drużyny "
                    "(z adresu strony FPL) albo dokładną nazwę.</p>")
            return _page(_form(query, note), 404)
        if len(found) > 1:
            return _page(_choices(found))
        member = found[0]

        active = await asyncio.to_thread(self.store.active)
        if any(int(r.get("entry_id", -1)) == member["entry"] for r in active) and not replace:
            return _page(_confirm_replace(member))

        key = new_key()
        replaced = await asyncio.to_thread(
            lambda: self.store.issue(key_sha256=hash_key(key), entry_id=member["entry"],
                                     league_id=member["league_id"], manager_name=member["manager"],
                                     team_name=member["team"], servers=["fpl"]))
        self.registry.invalidate()
        logger.info("MCP join: issued key for entry %s (replaced %d)", member["entry"], replaced)
        text = (f"🔑 Nowy klucz MCP (FPL): {member['manager']} — {member['team']} (ID {member['entry']}), "
                f"liga {member['league']}." + (" Zastąpił poprzedni klucz." if replaced else "")
                + f"\nOdwołanie: /mcp revoke {member['entry']}")
        task = asyncio.create_task(self._notify_quietly(text))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return _page(_issued(member, key, _public_base(request)))

    async def _notify_quietly(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:
            logger.warning("MCP join: owner notification failed", exc_info=True)


def build_join_routes(registry: ClientRegistry, store: Any, **kwargs) -> list[Route]:
    service = JoinService(registry, store, **kwargs)
    # /join on the server's own subdomain; /mcp/join kept for older links.
    return [Route("/join", service.endpoint, methods=["GET", "POST"]),
            Route("/mcp/join", service.endpoint, methods=["GET", "POST"])]
