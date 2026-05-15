#!/usr/bin/env python3
"""
mcp_server.py — MCP-server för LiU:s SOU-databas och dokumentkedjor.

Exponerar upp till fyra verktyg (styrda via .env-flaggor):
  search_sou               Fritextsökning i SOU:er 1922–idag (LiU:s Solr-API)
  get_sou                  Metadata för specifik SOU, t.ex. "2025:108"
  fetch_sou_content        Laddar ned och extraherar text ur SOU-PDF (med OCR-fallback)
  find_document_relations  Tvåriktad kedjesökning:
                             - SOU YYYY:N → propositioner/betänkanden/skrivelser som behandlar den
                             - Prop/skr/bet → SOU:er som nämns i dokumenttexten

Transport styrs via MCP_TRANSPORT i .env: stdio (standard) eller http.
Databas styrs via DATABASE_URL: postgresql://... (standard) eller sqlite:///...
SOU_SOKNING_AKTIV / SOU_HAMTNING_AKTIV styr om sök- resp. hämtningsverktyg exponeras.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
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

DATABASE_URL        = os.getenv("DATABASE_URL", "")
PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "1"))
SOU_SOKNING_AKTIV   = os.getenv("SOU_SOKNING_AKTIV",  "true").lower() == "true"
SOU_HAMTNING_AKTIV  = os.getenv("SOU_HAMTNING_AKTIV", "true").lower() == "true"

RIKSDAG_DOK_BASE  = "https://data.riksdagen.se/dokumentlista/"
RIKSDAG_TEXT_BASE = "https://data.riksdagen.se/dokument/"

HEADERS = {"User-Agent": "liu-sou-mcp/1.1 (akademiskt projekt; kontakt via GitHub)"}

# Årsöversikter som nämner i stort sett alla SOU:er — filtreras bort som brus.
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


# ── Databas: dubbelt backend-mönster (PostgreSQL / SQLite) ────────────────────

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Returnerar en ny databasanslutning."""
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    # SQLite: tolka DATABASE_URL eller använd standardfil bredvid skriptet
    if DATABASE_URL.startswith("sqlite:///"):
        sokvag = DATABASE_URL.replace("sqlite:///", "")
    else:
        sokvag = "liu_sou_cache.db"
    if not Path(sokvag).is_absolute():
        sokvag = str(_SCRIPT_DIR / sokvag)
    return sqlite3.connect(sokvag)


def _prefix() -> str:
    """Returnerar schema-prefix för tabellnamn: 'liu_sou.' eller ''."""
    return "liu_sou." if _ar_postgres() else ""


def _ph() -> str:
    """Returnerar platshållarsyntax för parametrar: %s (Postgres) eller ? (SQLite)."""
    return "%s" if _ar_postgres() else "?"


def _now() -> str:
    """Returnerar SQL-uttryck för aktuell tidsstämpel."""
    return "NOW()" if _ar_postgres() else "datetime('now')"


def _initialisera_schema() -> None:
    """Skapar schema och tabeller om de inte finns. Körs vid serveruppstart."""
    try:
        conn = _hamta_db()
        cur  = conn.cursor()

        if _ar_postgres():
            cur.execute("CREATE SCHEMA IF NOT EXISTS liu_sou")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS liu_sou.pdf_cache (
                    sou_beteckning TEXT PRIMARY KEY,
                    titel          TEXT,
                    ar             INTEGER,
                    url            TEXT,
                    fulltext_md    TEXT,
                    pdf_sokvag     TEXT,
                    hamtad_ts      TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS liu_sou.sync_status (
                    nyckel TEXT PRIMARY KEY,
                    varde  TEXT
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pdf_cache (
                    sou_beteckning TEXT PRIMARY KEY,
                    titel          TEXT,
                    ar             INTEGER,
                    url            TEXT,
                    fulltext_md    TEXT,
                    pdf_sokvag     TEXT,
                    hamtad_ts      TEXT DEFAULT (datetime('now'))
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_status (
                    nyckel TEXT PRIMARY KEY,
                    varde  TEXT
                )
            """)

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Databasschema liu_sou initialiserat")
    except Exception as e:
        logger.warning("Kunde inte initialisera databasschema: %s", e)


def _spara_i_pdf_cache(
    sou_beteckning: str,
    url: str,
    fulltext_md: str,
    pdf_sokvag: Optional[str],
    titel: Optional[str] = None,
    ar: Optional[int] = None,
) -> None:
    """Sparar eller uppdaterar en post i pdf_cache-tabellen."""
    ph = _ph()
    p  = _prefix()
    try:
        conn = _hamta_db()
        cur  = conn.cursor()
        if _ar_postgres():
            cur.execute(
                f"""INSERT INTO {p}pdf_cache
                        (sou_beteckning, titel, ar, url, fulltext_md, pdf_sokvag, hamtad_ts)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},NOW())
                    ON CONFLICT (sou_beteckning) DO UPDATE SET
                        fulltext_md = EXCLUDED.fulltext_md,
                        pdf_sokvag  = EXCLUDED.pdf_sokvag,
                        hamtad_ts   = NOW()
                """,
                (sou_beteckning, titel, ar, url, fulltext_md, pdf_sokvag),
            )
        else:
            cur.execute(
                f"""INSERT INTO {p}pdf_cache
                        (sou_beteckning, titel, ar, url, fulltext_md, pdf_sokvag, hamtad_ts)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},datetime('now'))
                    ON CONFLICT (sou_beteckning) DO UPDATE SET
                        fulltext_md = excluded.fulltext_md,
                        pdf_sokvag  = excluded.pdf_sokvag,
                        hamtad_ts   = datetime('now')
                """,
                (sou_beteckning, titel, ar, url, fulltext_md, pdf_sokvag),
            )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Sparade fulltext i DB-cache för SOU %s", sou_beteckning)
    except Exception as e:
        logger.warning("Kunde inte spara i pdf_cache: %s", e)


def _hamta_fran_pdf_cache(sou_beteckning: str) -> Optional[str]:
    """Returnerar cachad fulltext_md för en SOU-beteckning, eller None."""
    try:
        conn = _hamta_db()
        cur  = conn.cursor()
        cur.execute(
            f"SELECT fulltext_md FROM {_prefix()}pdf_cache WHERE sou_beteckning = {_ph()}",
            (sou_beteckning,),
        )
        rad = cur.fetchone()
        cur.close()
        conn.close()
        if rad and rad[0]:
            return rad[0]
    except Exception as e:
        logger.warning("Kunde inte läsa från pdf_cache: %s", e)
    return None


def _nolla_pdf_sokvag(sou_beteckning: str) -> None:
    """Sätter pdf_sokvag = NULL efter att filen raderats."""
    try:
        conn = _hamta_db()
        cur  = conn.cursor()
        cur.execute(
            f"UPDATE {_prefix()}pdf_cache SET pdf_sokvag = NULL WHERE sou_beteckning = {_ph()}",
            (sou_beteckning,),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("Kunde inte nolla pdf_sokvag för %s: %s", sou_beteckning, e)


def stada_pdf_cache() -> dict:
    """Raderar PDF-filer vars fulltext finns i databasen och som är äldre
    än PDF_CACHE_TTL_DAGAR dagar (standard: 1 dag).

    Filer där fulltext_md IS NULL lämnas kvar för retry.
    Returnerar statistik: {raderade, bevarade, fel}.
    """
    gransvarde = datetime.utcnow() - timedelta(days=PDF_CACHE_TTL_DAGAR)
    raderade = bevarade = fel = 0

    try:
        conn = _hamta_db()
        cur  = conn.cursor()
        cur.execute(
            f"SELECT sou_beteckning, pdf_sokvag FROM {_prefix()}pdf_cache "
            f"WHERE fulltext_md IS NOT NULL AND pdf_sokvag IS NOT NULL"
        )
        rader = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("stada_pdf_cache: kunde inte läsa från DB: %s", e)
        return {"raderade": 0, "bevarade": 0, "fel": 1}

    for beteckning, sokvag_str in rader:
        if not sokvag_str:
            continue
        fil = Path(sokvag_str)
        if not fil.exists():
            _nolla_pdf_sokvag(beteckning)
            continue
        try:
            andrad = datetime.utcfromtimestamp(fil.stat().st_mtime)
            if andrad > gransvarde:
                bevarade += 1
                continue
        except Exception:
            pass
        try:
            fil.unlink()
            _nolla_pdf_sokvag(beteckning)
            raderade += 1
        except Exception as e:
            logger.warning("Kunde inte radera %s: %s", fil.name, e)
            fel += 1

    logger.info(
        "PDF-cache städad: %d raderade, %d bevarade (yngre än %d dag(ar)), %d fel",
        raderade, bevarade, PDF_CACHE_TTL_DAGAR, fel,
    )
    return {"raderade": raderade, "bevarade": bevarade, "fel": fel}


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
    """Laddar ned PDF och sparar i filcache. Returnerar sökväg."""
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_fil = PDF_CACHE_DIR / f"{cache_nyckel}.pdf"
    if cache_fil.exists():
        logger.info("Filcache träff: %s", cache_nyckel)
        return cache_fil
    logger.info("Laddar ned PDF: %s", url)
    pdf_bytes = _get(url, timeout=60)
    cache_fil.write_bytes(pdf_bytes)
    logger.info("PDF cachad lokalt: %s (%d bytes)", cache_nyckel, len(pdf_bytes))
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

# Verktygsdefinitioner — filtreras vid lista_verktyg() baserat på .env-flaggor

_TOOL_SEARCH_SOU = Tool(
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
)

_TOOL_GET_SOU = Tool(
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
)

_TOOL_FETCH_SOU_CONTENT = Tool(
    name="fetch_sou_content",
    description=(
        "Laddar ned och extraherar text ur en SOU-PDF. "
        "Hanterar automatiskt moderna digitala SOU:er (1997+) och äldre KB-digitaliserade "
        "skanningar (1922–1996) med OCR-fallback. "
        "PDF-URL:en hämtas med get_sou eller search_sou. "
        "Fulltext för hela dokument cachas i databasen — efterföljande anrop returnerar "
        "direkt från cache utan ny nedladdning. "
        "Stora dokument kan ta 10–30 sekunder vid första hämtning."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "url": {
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
)

_TOOL_FIND_RELATIONS = Tool(
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
)


@server.list_tools()
async def lista_verktyg() -> list[Tool]:
    """Returnerar aktiva verktyg baserat på SOU_SOKNING_AKTIV och SOU_HAMTNING_AKTIV."""
    verktyg: list[Tool] = []
    if SOU_SOKNING_AKTIV:
        verktyg.extend([_TOOL_SEARCH_SOU, _TOOL_GET_SOU])
    if SOU_HAMTNING_AKTIV:
        verktyg.append(_TOOL_FETCH_SOU_CONTENT)
    verktyg.append(_TOOL_FIND_RELATIONS)  # alltid aktiv — söker riksdagens API, inte SOU-PDF:er
    return verktyg


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
    if not SOU_SOKNING_AKTIV:
        return [TextContent(type="text", text="SOU-sökning är inaktiverad på denna server (SOU_SOKNING_AKTIV=false i .env).")]

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
    if not SOU_SOKNING_AKTIV:
        return [TextContent(type="text", text="SOU-sökning är inaktiverad på denna server (SOU_SOKNING_AKTIV=false i .env).")]

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
    if not SOU_HAMTNING_AKTIV:
        return [TextContent(type="text", text="SOU-hämtning är inaktiverad på denna server (SOU_HAMTNING_AKTIV=false i .env).")]

    # Kontrollera DB-cache för hela dokument (inte delsidor — de är tillfälliga förfrågningar)
    if sidor is None:
        cachad_text = _hamta_fran_pdf_cache(namn)
        if cachad_text:
            logger.info("DB-cache träff för SOU %s", namn)
            return [TextContent(type="text", text=f"# SOU {namn}\n\n{cachad_text}")]

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

    # Spara hela dokument i DB-cache och radera PDF-filen direkt
    if sidor is None:
        _spara_i_pdf_cache(namn, url, text, str(pdf_vag))
        try:
            pdf_vag.unlink()
            _nolla_pdf_sokvag(namn)
            logger.info("PDF raderad direkt efter extraktion: %s", pdf_vag.name)
        except Exception as e:
            logger.warning("Kunde inte radera PDF %s: %s", pdf_vag.name, e)

    sidor_info = f"sidor {sidor}" if sidor else f"alla {antal_sidor} sidor"
    return [TextContent(type="text", text=f"# SOU {namn} ({sidor_info})\n\n{text}")]


async def _find_document_relations(
    beteckning: str,
    doktyper: Optional[list[str]] = None,
) -> list[TextContent]:
    """Tvåriktad kedjesökning: SOU→riksdagsdokument eller riksdagsdok→SOU:er."""

    # ── Riktning 1: SOU → riksdagsdokument ──────────────────────────────────
    if re.match(r"^\d{4}:\d+$", beteckning.strip()):
        return await _sou_till_riksdagsdok(beteckning.strip(), doktyper)

    # ── Riktning 2: Riksdagsdokument → SOU-beteckningar ─────────────────────
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

    docs = _riksdag_sok(beteckning, antal=10)

    exakta = [
        d for d in docs
        if d.get("beteckning", "").strip() == beteckning.split(":")[-1].strip()
        or beteckning in (d.get("rm", "") + "/" + d.get("beteckning", ""))
    ]
    if not exakta:
        exakta = docs

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
    _initialisera_schema()
    stada_pdf_cache()  # städar eventuella rester från tidigare körning

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
