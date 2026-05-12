#!/usr/bin/env python3
"""
mcp_server.py — MCP-server för LiU:s SOU-databas och dokumentkedjor.

Exponerar fyra verktyg:
  search_sou             Fritextsökning i SOU:er 1922–idag (LiU:s Solr-API)
  get_sou                Metadata för specifik SOU, t.ex. "2025:108"
  fetch_sou_content      Laddar ned och extraherar text ur SOU-PDF (med OCR-fallback)
  find_document_relations  Tvåriktad kedjesökning:
                           - SOU YYYY:N → propositioner/betänkanden/skrivelser som behandlar den
                           - Prop/skr/bet → SOU:er som nämns i dokumenttexten

Transport styrs via MCP_TRANSPORT i .env: stdio (standard) eller http.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import pymupdf
import pymupdf4llm
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

LIU_API_KEY  = os.getenv("LIU_API_KEY", "test")
LIU_API_BASE = os.getenv("LIU_API_BASE", "https://www2.bibl.liu.se/api/sou_api/getdata.aspx")
PDF_CACHE_DIR = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
# Ankra relativa sökvägar mot skriptets mapp (inte processens cwd)
if not PDF_CACHE_DIR.is_absolute():
    PDF_CACHE_DIR = _SCRIPT_DIR / PDF_CACHE_DIR
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")

RIKSDAG_DOK_BASE  = "https://data.riksdagen.se/dokumentlista/"
RIKSDAG_TEXT_BASE = "https://data.riksdagen.se/dokument/"

HEADERS = {"User-Agent": "liu-sou-mcp/0.1 (akademiskt projekt; kontakt via GitHub)"}

# Årsöversikter som nämner i stort sett alla SOU:er — filtreras bort som brus.
# Dessa är Regeringens skrivelser av årsrapportskaraktär, inte substantiella svar.
BRUS_TITLAR = [
    "Kommittéberättelse",
    "Riksdagens skrivelser till regeringen – åtgärder",
]

# ── Loggning ───────────────────────────────────────────────────────────────────

LOG_DIR = _SCRIPT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ── FD-skydd: förhindrar att C-bibliotek skriver skräp på MCP:s stdout ────────

@contextlib.contextmanager
def _tysta_subprocess_stdout():
    """Omdirigerar FD 1+2 till loggfil under C-bundna biblioteksanrop.
    Nödvändigt för att skydda MCP stdio-protokollet mot diagnostikutskrifter."""
    log_path = LOG_DIR / "subprocess.log"
    spara_ut  = os.dup(1)
    spara_fel = os.dup(2)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        yield
    finally:
        os.dup2(spara_ut, 1)
        os.dup2(spara_fel, 2)
        os.close(spara_ut)
        os.close(spara_fel)
        os.close(log_fd)


# ── HTTP-hjälpfunktioner ───────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _liu_sok(params: dict) -> dict:
    """Anropar LiU:s Solr-API och returnerar svaret som dict."""
    params["api_key"] = LIU_API_KEY
    params["wt"]      = "json"
    url = f"{LIU_API_BASE}?{urllib.parse.urlencode(params)}"
    logger.info("LiU API: %s", url)
    return json.loads(_get(url))


def _riksdag_sok(sok: str, doktyp: str = "", antal: int = 20) -> list[dict]:
    """Söker i riksdagens dokumentlista. Returnerar lista med dokument."""
    params: dict = {
        "sok":      sok,
        "utformat": "json",
        "a":        "s",
        "antal":    str(antal),
    }
    if doktyp:
        params["doktyp"] = doktyp
    url = f"{RIKSDAG_DOK_BASE}?{urllib.parse.urlencode(params)}"
    logger.info("Riksdag API: %s", url)
    data = json.loads(_get(url))
    docs = data.get("dokumentlista", {}).get("dokument", [])
    if not isinstance(docs, list):
        docs = [docs] if docs else []
    return docs


def _hamta_riksdag_text(dok_id: str) -> str:
    """Hämtar HTML-texten för ett riksdagsdokument."""
    url = f"{RIKSDAG_TEXT_BASE}{dok_id}.html"
    return _get(url, timeout=20).decode("utf-8", errors="replace")


def _hamta_pdf_url_fran_kb_urn(urn_url: str) -> Optional[str]:
    """Löser KB URN-URL (urn.kb.se) → direktlänk till PDF på weburn.kb.se.

    Äldre SOU:er (1922–1996) är KB-digitaliserade och lagras bakom ett
    tvåstegs-redirect: URN-resolver → metadata-HTML → PDF-länk.
    """
    html = _get(urn_url, timeout=20).decode("utf-8", errors="replace")
    lankar = re.findall(
        r'href=["\']+(https://weburn\.kb\.se/[^"\']+\.pdf)["\']', html
    )
    return lankar[0] if lankar else None


# ── Filterhjälp ────────────────────────────────────────────────────────────────

def _ar_brus(titel: str) -> bool:
    """Returnerar True om dokumentet är en årsöversikt som nämner alla SOU:er."""
    return any(b.lower() in titel.lower() for b in BRUS_TITLAR)


def _formatera_riksdagsdok(d: dict) -> str:
    subtyp   = d.get("subtyp", d.get("typ", "?"))
    rm       = d.get("rm", "")
    beteckn  = d.get("beteckning", "")
    titel    = d.get("titel", "")
    datum    = d.get("datum", "")[:10]
    ref = f"{rm}/{beteckn}".strip("/")
    return f"[{subtyp}] {ref} ({datum}) — {titel}"


# ── PDF-pipeline ───────────────────────────────────────────────────────────────

def _hamta_och_casha_pdf(url: str, cache_nyckel: str) -> Path:
    """Laddar ned PDF och sparar i cache. Returnerar sökväg."""
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_fil = PDF_CACHE_DIR / f"{cache_nyckel}.pdf"
    if cache_fil.exists():
        logger.info("PDF-cache träff: %s", cache_nyckel)
        return cache_fil
    logger.info("Laddar ned PDF: %s", url)
    pdf_bytes = _get(url, timeout=60)
    cache_fil.write_bytes(pdf_bytes)
    logger.info("PDF cachad: %s (%d bytes)", cache_nyckel, len(pdf_bytes))
    return cache_fil


def _extrahera_text(pdf_vag: Path, sidor: Optional[list[int]] = None) -> str:
    """Extraherar text ur PDF med pymupdf4llm. OCR körs automatiskt vid behov."""
    with _tysta_subprocess_stdout():
        kwargs = {}
        if sidor is not None:
            kwargs["pages"] = sidor
        return pymupdf4llm.to_markdown(str(pdf_vag), **kwargs)


# ── MCP-server ─────────────────────────────────────────────────────────────────

server = Server("liu-sou")

VERKTYG = [
    Tool(
        name="search_sou",
        description=(
            "Söker i Linköpings universitetsbiblioteks fulltextdatabas över svenska statliga "
            "offentliga utredningar (SOU 1922–idag). Returnerar beteckning, titel, år och PDF-URL. "
            "Använd get_sou för att hämta metadata för en specifik beteckning, eller "
            "fetch_sou_content för att läsa innehållet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Fritextsökning i SOU-fulltext, t.ex. 'miljöbalken skadestånd'"
                },
                "year_from": {"type": "integer", "description": "Filtrera från och med detta år"},
                "year_to":   {"type": "integer", "description": "Filtrera till och med detta år"},
                "max_results": {
                    "type": "integer",
                    "description": "Max antal träffar (standard 10)",
                    "default": 10
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_sou",
        description=(
            "Hämtar metadata för en specifik SOU baserat på beteckning, t.ex. '2025:108' eller '1969:46'. "
            "Returnerar beteckning, titel, år, ISBN och PDF-URL. "
            "En SOU kan ha flera delar — alla returneras."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "namn": {
                    "type": "string",
                    "description": "SOU-beteckning, t.ex. '2025:108'"
                }
            },
            "required": ["namn"],
        },
    ),
    Tool(
        name="fetch_sou_content",
        description=(
            "Laddar ned och extraherar text ur en SOU-PDF. "
            "Hanterar automatiskt moderna digitala SOU:er (1997+) och äldre KB-digitaliserade "
            "skanningar (1922–1996) med OCR-fallback. "
            "PDF-URL:en hämtas med get_sou eller search_sou. "
            "Stora dokument kan ta 10–30 sekunder."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url":  {
                    "type": "string",
                    "description": "PDF-URL från get_sou eller search_sou"
                },
                "namn": {
                    "type": "string",
                    "description": "SOU-beteckning, t.ex. '2025:108' — används som cache-nyckel"
                },
                "sidor": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Sidnummer att extrahera (0-indexerat). Utelämna för hela dokumentet."
                },
            },
            "required": ["url", "namn"],
        },
    ),
    Tool(
        name="find_document_relations",
        description=(
            "Tvåriktad kedjesökning för att knyta ihop riksdagens dokumentkedja.\n\n"
            "Om beteckning är en SOU (format YYYY:N, t.ex. '2025:108'):\n"
            "  → Söker i riksdagen efter propositioner, betänkanden och "
            "regeringsskrivelser som behandlar SOU:n. "
            "Substantiella svar returneras; årsöversikter (Kommittéberättelse m.fl.) filtreras bort.\n\n"
            "Om beteckning är ett riksdagsdokument (prop/skr/bet, format YYYY/YY:N, t.ex. '2025/26:136'):\n"
            "  → Hämtar dokumentets text från riksdagen och extraherar alla SOU-beteckningar "
            "som nämns i texten.\n\n"
            "Möjliggör traversering av hela kedjan: "
            "prejudikat → lagparagraf → proposition → SOU → remissvar."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "beteckning": {
                    "type": "string",
                    "description": (
                        "SOU-beteckning (t.ex. '2025:108') eller riksdagsdokumentets beteckning "
                        "(t.ex. '2025/26:136')"
                    )
                },
                "doktyper": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Dokumenttyper att inkludera vid SOU-sökning. "
                        "Standard: ['prop', 'skr', 'bet', 'dir']. "
                        "Möjliga värden: prop, skr, bet, dir, rir, komm."
                    )
                },
            },
            "required": ["beteckning"],
        },
    ),
]


@server.list_tools()
async def lista_verktyg():
    return VERKTYG


@server.call_tool()
async def anropa_verktyg(name: str, arguments: dict):
    try:
        if name == "search_sou":
            return await _search_sou(**arguments)
        elif name == "get_sou":
            return await _get_sou(**arguments)
        elif name == "fetch_sou_content":
            return await _fetch_sou_content(**arguments)
        elif name == "find_document_relations":
            return await _find_document_relations(**arguments)
        else:
            return [TextContent(type="text", text=f"Okänt verktyg: {name}")]
    except Exception as e:
        logger.exception("Fel i verktyg %s", name)
        return [TextContent(type="text", text=f"FEL: {e}")]


# ── Verktygsimplementationer ───────────────────────────────────────────────────

async def _search_sou(
    query: str,
    year_from: Optional[int] = None,
    year_to: Optional[int]   = None,
    max_results: int = 10,
) -> list[TextContent]:
    params: dict = {
        "q":    f"fritext:{query}",
        "fl":   "namn,titel,ar,url,isbn",
        "rows": str(min(max_results, 50)),
        "sort": "ar desc",
    }
    if year_from is not None and year_to is not None:
        params["fq"] = f"ar:[{year_from} TO {year_to}]"
    elif year_from is not None:
        params["fq"] = f"ar:[{year_from} TO *]"
    elif year_to is not None:
        params["fq"] = f"ar:[* TO {year_to}]"

    svar = _liu_sok(params)
    docs  = svar["response"]["docs"]
    total = svar["response"]["numFound"]

    if not docs:
        return [TextContent(type="text", text="Inga träffar.")]

    rader = [f"Hittade {total} SOU:er (visar {len(docs)}):\n"]
    for doc in docs:
        isbn_del = f" | ISBN {doc['isbn']}" if "isbn" in doc else ""
        rader.append(
            f"**SOU {doc['namn']}** ({doc['ar']}) — {doc['titel']}{isbn_del}\n"
            f"PDF: {doc['url']}"
        )
    return [TextContent(type="text", text="\n\n".join(rader))]


async def _get_sou(namn: str) -> list[TextContent]:
    escaped = namn.replace(":", "\\:")
    svar = _liu_sok({"q": f"namn:{escaped}", "fl": "id,namn,titel,ar,nummer,isbn,url"})
    docs = svar["response"]["docs"]

    if not docs:
        return [TextContent(type="text", text=f"SOU {namn} hittades inte.")]

    rader = []
    for doc in docs:
        isbn_del = f"\nISBN: {doc['isbn']}" if "isbn" in doc else ""
        rader.append(
            f"**SOU {doc['namn']}** — {doc['titel']}\n"
            f"År: {doc['ar']} | Löpnummer: {doc.get('nummer', '—')}{isbn_del}\n"
            f"PDF: {doc['url']}"
        )
    return [TextContent(type="text", text="\n\n".join(rader))]


async def _fetch_sou_content(
    url: str,
    namn: str,
    sidor: Optional[list[int]] = None,
) -> list[TextContent]:
    # Äldre SOU:er (1922–1996) har KB URN-adresser som kräver upplösning
    if "urn.kb.se" in url:
        logger.info("Löser KB URN: %s", url)
        pdf_url = _hamta_pdf_url_fran_kb_urn(url)
        if not pdf_url:
            return [TextContent(
                type="text",
                text=f"FEL: Kunde inte lösa PDF-URL från KB URN: {url}"
            )]
        url = pdf_url

    cache_nyckel = namn.replace(":", "_")
    if sidor:
        cache_nyckel += "_sid" + "_".join(str(s) for s in sidor)

    pdf_vag = _hamta_och_casha_pdf(url, cache_nyckel)

    doc = pymupdf.open(str(pdf_vag))
    antal_sidor = doc.page_count
    doc.close()

    text = _extrahera_text(pdf_vag, sidor)

    sidor_info = f"sidor {sidor}" if sidor else f"alla {antal_sidor} sidor"
    return [TextContent(type="text", text=f"# SOU {namn} ({sidor_info})\n\n{text}")]


async def _find_document_relations(
    beteckning: str,
    doktyper: Optional[list[str]] = None,
) -> list[TextContent]:
    """Tvåriktad kedjesökning: SOU→riksdagsdokument eller riksdagsdok→SOU:er."""

    # ── Riktning 1: SOU → riksdagsdokument ──────────────────────────────────
    # SOU-beteckningar har formatet YYYY:N (t.ex. 2025:108)
    if re.match(r"^\d{4}:\d+$", beteckning.strip()):
        return await _sou_till_riksdagsdok(beteckning.strip(), doktyper)

    # ── Riktning 2: Riksdagsdokument → SOU-beteckningar ─────────────────────
    # Propositioner, skrivelser m.fl. har formatet YYYY/YY:N (t.ex. 2025/26:136)
    return await _riksdagsdok_till_souer(beteckning.strip())


async def _sou_till_riksdagsdok(
    sou_beteckning: str,
    doktyper: Optional[list[str]],
) -> list[TextContent]:
    """SOU YYYY:N → riksdagsdokument som behandlar SOU:n."""

    if doktyper is None:
        doktyper = ["prop", "skr", "bet", "dir"]

    alla_docs: list[dict] = []
    for doktyp in doktyper:
        docs = _riksdag_sok(sou_beteckning, doktyp=doktyp, antal=20)
        alla_docs.extend(docs)

    # Deduplicera på dok-id
    sedda: set[str] = set()
    unika: list[dict] = []
    for d in alla_docs:
        dok_id = d.get("id", "")
        if dok_id not in sedda:
            sedda.add(dok_id)
            unika.append(d)

    # Filtrera bort årsöversikter och SOU:n själv
    relevanta = [
        d for d in unika
        if not _ar_brus(d.get("titel", ""))
        and d.get("typ", "") != "sou"
    ]

    if not relevanta:
        return [TextContent(
            type="text",
            text=f"Inga riksdagsdokument hittade som behandlar SOU {sou_beteckning}."
        )]

    # Gruppera per dokumenttyp
    grupper: dict[str, list[dict]] = {}
    typ_ordning = ["prop", "skr", "bet", "dir", "rir", "komm"]
    typ_etiketter = {
        "prop": "Propositioner",
        "skr":  "Regeringens skrivelser",
        "bet":  "Riksdagsbetänkanden",
        "dir":  "Kommittédirektiv",
        "rir":  "Riksrevisionens rapporter",
        "komm": "Kommittéer",
    }
    for d in relevanta:
        t = d.get("subtyp") or d.get("typ", "övrigt")
        grupper.setdefault(t, []).append(d)

    rader = [f"## Riksdagsdokument som behandlar SOU {sou_beteckning}\n"]
    for t in typ_ordning + sorted(set(grupper) - set(typ_ordning)):
        if t not in grupper:
            continue
        etikett = typ_etiketter.get(t, t.upper())
        rader.append(f"### {etikett}")
        for d in grupper[t]:
            rader.append(_formatera_riksdagsdok(d))

    rader.append(
        "\n*Tips: Anropa find_document_relations med en propositionsbeteckning "
        "för att se vilka SOU:er som nämns i den.*"
    )
    return [TextContent(type="text", text="\n".join(rader))]


async def _riksdagsdok_till_souer(beteckning: str) -> list[TextContent]:
    """Riksdagsdokument YYYY/YY:N → SOU-beteckningar som nämns i texten."""

    # Hitta dokumentet i riksdagens API
    docs = _riksdag_sok(beteckning, antal=10)

    # Filtrera till det dokument vars beteckning matchar exakt
    # (sökningen kan returnera dokument som nämner beteckningen i texten)
    exakta = [
        d for d in docs
        if d.get("beteckning", "").strip() == beteckning.split(":")[-1].strip()
        or beteckning in (d.get("rm", "") + "/" + d.get("beteckning", ""))
    ]
    if not exakta:
        exakta = docs  # Fallback: ta första träffen

    if not exakta:
        return [TextContent(
            type="text",
            text=f"Hittade inget riksdagsdokument med beteckning {beteckning}."
        )]

    huvud_dok = exakta[0]
    dok_id    = huvud_dok.get("id", "")
    titel     = huvud_dok.get("titel", "")
    rm        = huvud_dok.get("rm", "")
    beteckn   = huvud_dok.get("beteckning", "")
    subtyp    = huvud_dok.get("subtyp") or huvud_dok.get("typ", "")

    if not dok_id:
        return [TextContent(
            type="text",
            text=f"Kunde inte hämta dokument-id för {beteckning}."
        )]

    # Hämta dokumenttext och extrahera SOU-beteckningar
    try:
        html = _hamta_riksdag_text(dok_id)
    except urllib.error.URLError as e:
        return [TextContent(type="text", text=f"FEL vid hämtning av dokumenttext: {e}")]

    sou_refs = sorted(set(re.findall(r"\bSOU\s+(\d{4}:\d+)\b", html)))

    if not sou_refs:
        return [TextContent(
            type="text",
            text=(
                f"**{subtyp} {rm}/{beteckn}** — {titel}\n\n"
                f"Inga SOU-beteckningar hittades i dokumenttexten."
            )
        )]

    rader = [
        f"## SOU:er nämnda i {subtyp} {rm}/{beteckn}",
        f"*{titel}*\n",
        f"Hittade {len(sou_refs)} SOU-beteckning(ar):\n",
    ]
    for sou in sou_refs:
        rader.append(f"- **SOU {sou}** — anropa get_sou('{sou}') för metadata och PDF-länk")

    rader.append(
        "\n*Tips: Anropa find_document_relations med en SOU-beteckning "
        "för att se vilka propositioner som behandlar den.*"
    )
    return [TextContent(type="text", text="\n".join(rader))]


# ── Startpunkt ─────────────────────────────────────────────────────────────────

async def _kor_stdio():
    async with stdio_server() as (las, skriv):
        await server.run(las, skriv, server.create_initialization_options())


def main():
    if MCP_TRANSPORT == "http":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response
        from starlette.routing import Route
        import uvicorn

        api_nyckel = os.getenv("MCP_API_KEY")

        class NyckelKontroll(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if api_nyckel:
                    if request.headers.get("Authorization") != f"Bearer {api_nyckel}":
                        return Response("Otillåten", status_code=401)
                return await call_next(request)

        transport = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with transport.connect_sse(
                request.scope, request.receive, request._send
            ) as (las, skriv):
                await server.run(las, skriv, server.create_initialization_options())

        app = Starlette(
            routes=[Route("/sse", endpoint=handle_sse)],
            middleware=[Middleware(NyckelKontroll)],
        )
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", "8004"))
        logger.info("Startar HTTP-server på %s:%s", host, port)
        uvicorn.run(app, host=host, port=port)
    else:
        logger.info("Startar stdio-server")
        asyncio.run(_kor_stdio())


if __name__ == "__main__":
    main()
