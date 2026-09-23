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

Transport styrs via MCP_TRANSPORT i .env: stdio eller http (se mcp_transport.py).
Databas styrs via DATABASE_URL: postgresql://... eller sqlite:///...
SOU_SOKNING_AKTIV / SOU_HAMTNING_AKTIV styr om sök- resp. hämtningsverktyg exponeras.
"""

import contextlib
import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Literal, NotRequired, Optional, TypedDict

from dotenv import load_dotenv

# ── Konfiguration ──────────────────────────────────────────────────────────────

# .env läses före importen av db.py, som läser DATABASE_URL vid modulinläsning.
# Servern ärver inte klientens shell-miljö, så filen bredvid skriptet är den
# enda konfigurationskällan i stdio-läget.
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

import pymupdf  # noqa: E402
import pymupdf4llm  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402

from db import (  # noqa: E402
    _hamta_fran_pdf_cache,
    _initialisera_schema,
    _nolla_pdf_sokvag,
    _spara_i_pdf_cache,
    stada_pdf_cache,
)
from mcp_annotationer import CACHE_HINTAR, LASNING_EXTERN  # noqa: E402
from mcp_transport import starta  # noqa: E402

LIU_API_KEY  = os.getenv("LIU_API_KEY", "test")
LIU_API_BASE = os.getenv("LIU_API_BASE", "https://www2.bibl.liu.se/api/sou_api/getdata.aspx")
PDF_CACHE_DIR = Path(os.getenv("PDF_CACHE_DIR", str(_SCRIPT_DIR / "pdf_cache")))
# Ankra relativa sökvägar mot skriptets mapp (inte processens cwd)
if not PDF_CACHE_DIR.is_absolute():
    PDF_CACHE_DIR = _SCRIPT_DIR / PDF_CACHE_DIR

SOU_SOKNING_AKTIV   = os.getenv("SOU_SOKNING_AKTIV",  "true").lower() == "true"
SOU_HAMTNING_AKTIV  = os.getenv("SOU_HAMTNING_AKTIV", "true").lower() == "true"

RIKSDAG_DOK_BASE  = "https://data.riksdagen.se/dokumentlista/"
RIKSDAG_TEXT_BASE = "https://data.riksdagen.se/dokument/"

HEADERS = {"User-Agent": "mcp-for-LiU-sok-SOU/1.0 (+https://github.com/MagnusKolsjo/mcp-for-LiU-sok-SOU)"}

# Årsöversikter som nämner i stort sett alla SOU:er — filtreras bort som brus.
BRUS_TITLAR = [
    "Kommittéberättelse",
    "Riksdagens skrivelser till regeringen – åtgärder",
]

# Versionen följer senaste släppta version i CHANGELOG.md.
SERVERVERSION = "2.0.0"

# ── Loggning ───────────────────────────────────────────────────────────────────

LOG_DIR = _SCRIPT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
# Varningar och fel även på stderr: ett avbrutet http-läge (saknad MCP_API_KEY)
# ska synas i terminalen, inte bara i loggfilen. stderr är aldrig protokollkanalen.
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setLevel(logging.WARNING)
_stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_stderr)
logger = logging.getLogger(__name__)

# Standardtak för SOU-fulltext. En utredning kan vara över en miljon tecken och
# överskrida MCP-protokollets storleksgräns, vilket får anropet att misslyckas
# helt. Anroparen kan höja taket eller sätta 0 för hela texten.
SOU_MAX_TECKEN = int(os.getenv("SOU_MAX_TECKEN", "60000"))

# Synkrona verktyg körs på arbetstrådar, så två PDF-hämtningar kan pågå samtidigt.
# Låset serialiserar hela PDF-kedjan: filcachen (två trådar får inte skriva och
# radera samma fil) och fd-omdirigeringen i _tysta_subprocess_stdout, som gäller
# hela processen och annars kan återställas i fel ordning.
_pdf_las = threading.Lock()


def _skar_ut_text(text, max_tecken: int, fran_tecken: int = 0, anvisning: str = "") -> str:
    """
    Skär ut ett textutdrag och markera alltid när något kapats.

    Trunkering utan markör är ett tyst datafel — svaret ser ut att vara hela
    utredningen. max_tecken <= 0 betyder ingen trunkering; klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    kapad = bool(max_tecken and max_tecken > 0 and len(rest) > max_tecken)
    if kapad:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag = utdrag.rstrip()
    else:
        utdrag = rest

    if not kapad and start == 0:
        return utdrag

    slut  = start + len(utdrag)
    noter = [f"Visar tecken {start + 1}–{slut} av {totalt}"]
    if anvisning:
        noter.append(anvisning)
    return utdrag + "\n\n[" + ". ".join(noter) + "]"


# ── FD-skydd: samlar C-bibliotekens diagnostik i en loggfil ────────────────────

@contextlib.contextmanager
def _tysta_subprocess_stdout():
    """Omdirigerar FD 1+2 till loggfil under C-bundna biblioteksanrop.

    pymupdf och OCR-steget skriver diagnostik direkt på filbeskrivarna, förbi
    Pythons loggning. Omdirigeringen samlar den i logs/subprocess.log i stället
    för att fylla klientens stderr-logg. Den gäller hela processen och får bara
    köras under _pdf_las.
    """
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


def _hamta(url: str, kalla: str, timeout: int = 15) -> bytes:
    """Som _get, men nätverks- och HTTP-fel blir ToolError som namnger källan."""
    try:
        return _get(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ToolError(f"{kalla} svarade med HTTP {e.code}. Försök igen senare.") from e
    except (urllib.error.URLError, TimeoutError) as e:
        orsak = getattr(e, "reason", e)
        raise ToolError(f"{kalla} gick inte att nå ({orsak}). Försök igen senare.") from e


def _liu_sok(params: dict) -> dict:
    """Anropar LiU:s Solr-API och returnerar svaret som dict.

    En avvisad nyckel ger HTTP 200 med en kort HTML-text i stället för JSON.
    Utan särskild kontroll blir det ett obegripligt JSON-tolkningsfel, så
    svaret känns igen och översätts till ett fel som säger vad som behöver göras.
    """
    params["api_key"] = LIU_API_KEY
    params["wt"]      = "json"
    url = f"{LIU_API_BASE}?{urllib.parse.urlencode(params)}"
    # Nyckeln hör inte hemma i loggen
    logger.info("LiU API: %s", url.replace(urllib.parse.quote_plus(LIU_API_KEY), "***"))
    radata = _hamta(url, "LiU:s SOU-databas")
    try:
        return json.loads(radata)
    except ValueError:
        text = radata.decode("utf-8", errors="replace")
        if "api_key" in text:
            raise ToolError(
                "LiU:s SOU-databas avvisade API-nyckeln (LIU_API_KEY). Nyckeln kan ha "
                "upphört att gälla. Begär en ny genom att mejla ep@ep.liu.se med ämnet "
                "'SOU API-nyckel'. Testnyckeln 'test' fungerar under tiden men ger "
                "högst fem träffar."
            ) from None
        raise ToolError(
            "LiU:s SOU-databas svarade inte med JSON. Tjänsten kan vara tillfälligt "
            "otillgänglig; försök igen senare."
        ) from None


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
    try:
        data = json.loads(_hamta(url, "Riksdagens öppna data"))
    except ValueError as e:
        raise ToolError(
            "Riksdagens öppna data svarade inte med JSON. Försök igen senare."
        ) from e
    docs = (data.get("dokumentlista") or {}).get("dokument") or []
    if not isinstance(docs, list):
        docs = [docs]
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
    html = _hamta(urn_url, "KB:s URN-resolver", timeout=20).decode("utf-8", errors="replace")
    lankar = re.findall(
        r'href=["\']+(https://weburn\.kb\.se/[^"\']+\.pdf)["\']', html
    )
    return lankar[0] if lankar else None


# ── Filterhjälp ────────────────────────────────────────────────────────────────

def _ar_brus(titel: str) -> bool:
    """Returnerar True om dokumentet är en årsöversikt som nämner alla SOU:er."""
    return any(b.lower() in titel.lower() for b in BRUS_TITLAR)


# ── PDF-pipeline ───────────────────────────────────────────────────────────────

def _hamta_och_casha_pdf(url: str, cache_nyckel: str) -> Path:
    """Laddar ned PDF och sparar i filcache. Returnerar sökväg."""
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_fil = PDF_CACHE_DIR / f"{cache_nyckel}.pdf"
    if cache_fil.exists():
        logger.info("Filcache träff: %s", cache_nyckel)
        return cache_fil
    logger.info("Laddar ned PDF: %s", url)
    pdf_bytes = _hamta(url, "PDF-källan", timeout=60)
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


# ── Svarstyper ─────────────────────────────────────────────────────────────────
#
# LiU:s index saknar ibland fält för enskilda poster (ISBN finns bara för
# nyare SOU:er), så allt utom beteckningen är valfritt. Ett fält som typen
# kräver men som saknas i svaret får hela anropet att misslyckas.

class SouPost(TypedDict):
    """En SOU (eller en del av en flerdelad SOU) i LiU:s SOU-databas."""
    namn: str
    titel: NotRequired[str]
    ar: NotRequired[int]
    url: NotRequired[str]
    isbn: NotRequired[str]
    nummer: NotRequired[int]
    id: NotRequired[str]


class SouSokresultat(TypedDict):
    """Svar från search_sou."""
    totalt: int
    visade: int
    traffar: list[SouPost]


class SouUppslag(TypedDict):
    """Svar från get_sou."""
    namn: str
    delar: list[SouPost]


class Riksdagsdokument(TypedDict):
    """Ett dokument i Riksdagens öppna data, i den form kedjesökningen behöver."""
    dok_id: str
    typ: str
    rm: str
    beteckning: str
    datum: str
    titel: str


class Dokumentrelationer(TypedDict):
    """Svar från find_document_relations.

    Riktningen avgör vilka fält som finns: från en SOU kommer
    `riksdagsdokument`, från ett riksdagsdokument kommer `dokument` och
    `sou_beteckningar`. `anmarkning` förklarar ett tomt resultat.
    """
    riktning: Literal["sou_till_riksdagsdokument", "riksdagsdokument_till_sou"]
    beteckning: str
    riksdagsdokument: NotRequired[list[Riksdagsdokument]]
    dokument: NotRequired[Riksdagsdokument]
    sou_beteckningar: NotRequired[list[str]]
    anmarkning: NotRequired[str]


def _sou_post(doc: dict) -> SouPost:
    """Plockar ut de fält som finns i en Solr-post, med typer som svaret lovar."""
    post: SouPost = {"namn": str(doc.get("namn", ""))}
    for falt in ("titel", "url", "id"):
        if doc.get(falt) is not None:
            post[falt] = str(doc[falt])
    for falt in ("ar", "nummer"):
        if doc.get(falt) is not None:
            post[falt] = int(doc[falt])
    isbn = doc.get("isbn")
    if isbn:
        post["isbn"] = ", ".join(map(str, isbn)) if isinstance(isbn, list) else str(isbn)
    return post


def _riksdagsdokument(d: dict) -> Riksdagsdokument:
    return {
        "dok_id":     str(d.get("id") or ""),
        "typ":        str(d.get("subtyp") or d.get("typ") or ""),
        "rm":         str(d.get("rm") or ""),
        "beteckning": str(d.get("beteckning") or ""),
        "datum":      str(d.get("datum") or "")[:10],
        "titel":      str(d.get("titel") or ""),
    }


# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "liu-sou",
    instructions=(
        "MCP-server för statens offentliga utredningar (SOU) 1922–idag via Linköpings "
        "universitetsbiblioteks SOU-databas, och för kedjan mellan SOU:er och "
        "riksdagsdokument via Riksdagens öppna data. "
        "ARBETSORDNING: search_sou (fritext) eller get_sou (känd beteckning, t.ex. "
        "'2025:108') ger PDF-URL:en; fetch_sou_content läser texten ur PDF:en med URL:en "
        "och beteckningen. find_document_relations går åt båda hållen: från en SOU till "
        "propositioner, betänkanden och skrivelser som behandlar den, eller från ett "
        "riksdagsdokument ('2025/26:136') till SOU:erna som nämns i det. "
        "TÄCKNING: LiU:s databas uppdateras med viss eftersläpning, så de senast "
        "utgivna SOU:erna kan saknas där. Ger get_sou inget svar för en ny beteckning, "
        "eller behövs det allra senaste, finns SOU:erna i Riksdagens öppna data "
        "(dokumenttyp 'sou'). "
        "SVARSSTORLEK: fetch_sou_content kapar texten vid max_tecken (standard 60 000). "
        "Ett kapat svar avslutas med en rad som anger teckenintervallet; läs vidare med "
        "fran_tecken. Citera aldrig ordagrant ur ett kapat utdrag utan att ha hämtat hela "
        "passagen. "
        "Äldre SOU:er (1922–1996) är OCR-lästa skanningar och kan ha felaktiga tecken."
    ),
    version=SERVERVERSION,
    cache_hints=CACHE_HINTAR,
)


@mcp.tool(title="Sök i SOU-databasen", annotations=LASNING_EXTERN)
def search_sou(
    query: Annotated[str, Field(
        description="Fritextsökning i SOU-fulltext, t.ex. 'miljöbalken skadestånd'",
    )],
    year_from: Annotated[Optional[int], Field(
        description="Filtrera från och med detta år",
    )] = None,
    year_to: Annotated[Optional[int], Field(
        description="Filtrera till och med detta år",
    )] = None,
    max_results: Annotated[int, Field(
        description="Max antal träffar (standard 10)",
    )] = 10,
) -> SouSokresultat:
    """Söker i Linköpings universitetsbiblioteks fulltextdatabas över svenska statliga offentliga utredningar (SOU 1922–idag). Returnerar beteckning, titel, år och PDF-URL. Använd get_sou för att hämta metadata för en specifik beteckning, eller fetch_sou_content för att läsa innehållet."""
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

    traffar = [_sou_post(doc) for doc in docs]
    return {"totalt": int(total), "visade": len(traffar), "traffar": traffar}


@mcp.tool(title="Hämta metadata för en SOU", annotations=LASNING_EXTERN)
def get_sou(
    namn: Annotated[str, Field(description="SOU-beteckning, t.ex. '2025:108'")],
) -> SouUppslag:
    """Hämtar metadata för en specifik SOU baserat på beteckning, t.ex. '2025:108' eller '1969:46'. Returnerar beteckning, titel, år, ISBN och PDF-URL. En SOU kan ha flera delar — alla returneras."""
    escaped = namn.replace(":", "\\:")
    svar = _liu_sok({"q": f"namn:{escaped}", "fl": "id,namn,titel,ar,nummer,isbn,url"})
    docs = svar["response"]["docs"]

    if not docs:
        raise ToolError(
            f"SOU {namn} finns inte i LiU:s SOU-databas. Kontrollera beteckningen "
            "(formen ÅÅÅÅ:N). Databasen uppdateras med viss eftersläpning, så en "
            "nyligen utgiven SOU kan i stället finnas i Riksdagens öppna data."
        )

    return {"namn": namn, "delar": [_sou_post(doc) for doc in docs]}


@mcp.tool(
    title="Läs text ur en SOU",
    annotations=LASNING_EXTERN,
    structured_output=False,
)
def fetch_sou_content(
    url: Annotated[str, Field(description="PDF-URL från get_sou eller search_sou")],
    namn: Annotated[str, Field(
        description="SOU-beteckning, t.ex. '2025:108' — används som cache-nyckel",
    )],
    sidor: Annotated[Optional[list[int]], Field(
        description="Sidnummer att extrahera (0-indexerat). Utelämna för hela dokumentet.",
    )] = None,
    max_tecken: Annotated[int, Field(
        description=(
            "Teckentak för texten (standard 60 000, 0 = hela texten). "
            "En utredning kan vara över en miljon tecken; utan tak "
            "misslyckas anropet mot svarsgränsen. Ett kapat svar avslutas "
            "med en rad som anger hur mycket som visas och hur resten hämtas."
        ),
    )] = SOU_MAX_TECKEN,
    fran_tecken: Annotated[int, Field(
        description="Börja texten vid denna teckenposition — för att läsa vidare.",
    )] = 0,
) -> str:
    """Laddar ned och extraherar text ur en SOU-PDF. Hanterar automatiskt moderna digitala SOU:er (1997+) och äldre KB-digitaliserade skanningar (1922–1996) med OCR-fallback. PDF-URL:en hämtas med get_sou eller search_sou. Fulltext för hela dokument cachas i databasen — efterföljande anrop returnerar direkt från cache utan ny nedladdning. Stora dokument kan ta 10–30 sekunder vid första hämtning."""
    # Kontrollera DB-cache för hela dokument (inte delsidor — de är tillfälliga förfrågningar)
    if sidor is None:
        cachad_text = _hamta_fran_pdf_cache(namn)
        if cachad_text:
            logger.info("DB-cache träff för SOU %s", namn)
            anvisning = (f'Läs vidare: fetch_sou_content(namn="{namn}", '
                         f"fran_tecken={fran_tecken + max_tecken})")
            utdrag = _skar_ut_text(cachad_text, max_tecken, fran_tecken, anvisning)
            return f"# SOU {namn}\n\n{utdrag}"

    # Äldre SOU:er (1922–1996) har KB URN-adresser som kräver upplösning
    if "urn.kb.se" in url:
        logger.info("Löser KB URN: %s", url)
        pdf_url = _hamta_pdf_url_fran_kb_urn(url)
        if not pdf_url:
            raise ToolError(
                f"Kunde inte lösa PDF-URL från KB URN: {url}. KB:s metadatasida "
                "innehöll ingen PDF-länk."
            )
        url = pdf_url

    cache_nyckel = namn.replace(":", "_")
    if sidor:
        cache_nyckel += "_sid" + "_".join(str(s) for s in sidor)

    with _pdf_las:
        pdf_vag = _hamta_och_casha_pdf(url, cache_nyckel)

        try:
            doc = pymupdf.open(str(pdf_vag))
            antal_sidor = doc.page_count
            doc.close()
        except Exception as e:
            # En felaktig fil i cachen skulle annars ge samma fel vid varje nytt försök
            pdf_vag.unlink(missing_ok=True)
            raise ToolError(
                f"Filen på {url} gick inte att läsa som PDF. Kontrollera URL:en med get_sou."
            ) from e

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
    # DB-cachen har alltid hela texten — trunkeringen gäller bara svaret.
    anvisning = (f'Läs vidare: fetch_sou_content(namn="{namn}", '
                 f"fran_tecken={fran_tecken + max_tecken})")
    utdrag = _skar_ut_text(text, max_tecken, fran_tecken, anvisning)
    return f"# SOU {namn} ({sidor_info})\n\n{utdrag}"


@mcp.tool(title="Hitta dokumentrelationer SOU–riksdag", annotations=LASNING_EXTERN)
def find_document_relations(
    beteckning: Annotated[str, Field(
        description=(
            "SOU-beteckning (t.ex. '2025:108') eller riksdagsdokumentets beteckning "
            "(t.ex. '2025/26:136')"
        ),
    )],
    doktyper: Annotated[Optional[list[str]], Field(
        description=(
            "Dokumenttyper att inkludera vid SOU-sökning. "
            "Standard: ['prop', 'skr', 'bet', 'dir']. "
            "Möjliga värden: prop, skr, bet, dir, rir, komm."
        ),
    )] = None,
) -> Dokumentrelationer:
    """Tvåriktad kedjesökning för att knyta ihop riksdagens dokumentkedja.

    Om beteckning är en SOU (format YYYY:N, t.ex. '2025:108'):
      → Söker i riksdagen efter propositioner, betänkanden och regeringsskrivelser som behandlar SOU:n. Substantiella svar returneras; årsöversikter (Kommittéberättelse m.fl.) filtreras bort.

    Om beteckning är ett riksdagsdokument (prop/skr/bet, format YYYY/YY:N, t.ex. '2025/26:136'):
      → Hämtar dokumentets text från riksdagen och extraherar alla SOU-beteckningar som nämns i texten.

    Möjliggör traversering av hela kedjan: prejudikat → lagparagraf → proposition → SOU → remissvar."""
    beteckning = beteckning.strip()

    # ── Riktning 1: SOU → riksdagsdokument ──────────────────────────────────
    if re.match(r"^\d{4}:\d+$", beteckning):
        return _sou_till_riksdagsdok(beteckning, doktyper)

    # ── Riktning 2: Riksdagsdokument → SOU-beteckningar ─────────────────────
    return _riksdagsdok_till_souer(beteckning)


# Ordningen dokumenttyperna redovisas i; okända typer läggs sist i bokstavsordning.
_TYP_ORDNING = ["prop", "skr", "bet", "dir", "rir", "komm"]


def _sou_till_riksdagsdok(
    sou_beteckning: str,
    doktyper: Optional[list[str]],
) -> Dokumentrelationer:
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
        if not _ar_brus(d.get("titel") or "")
        and d.get("typ", "") != "sou"
    ]

    # Verifikationsfas: kontrollera att "SOU YYYY:N" faktiskt nämns i texten.
    # De första åtta kandidaterna hämtas två åt gången, resten en i taget —
    # en avvägning mellan svarstid och belastning på riksdagens servrar.
    # Vid nätverksfel behålls kandidaten — bättre falsk positiv än missad träff.
    _PARALLELL_GRANS = 8
    _SAMTIDIGA       = 2

    sou_monster = re.compile(r"\bSOU\s+" + re.escape(sou_beteckning) + r"\b")

    def _verifiera(d: dict) -> dict | None:
        dok_id = d.get("id", "")
        if not dok_id:
            return None
        try:
            html = _hamta_riksdag_text(dok_id)
            return d if sou_monster.search(html) else None
        except Exception:
            return d

    with ThreadPoolExecutor(max_workers=_SAMTIDIGA) as pool:
        verifierade = [r for r in pool.map(_verifiera, relevanta[:_PARALLELL_GRANS]) if r is not None]
    for d in relevanta[_PARALLELL_GRANS:]:
        r = _verifiera(d)
        if r is not None:
            verifierade.append(r)

    dokument = [_riksdagsdokument(d) for d in verifierade]
    ovriga = sorted({d["typ"] for d in dokument} - set(_TYP_ORDNING))
    ordning = {t: i for i, t in enumerate(_TYP_ORDNING + ovriga)}
    # sorted är stabil: inom en typ behålls riksdagens relevansordning
    dokument.sort(key=lambda d: ordning[d["typ"]])

    svar: Dokumentrelationer = {
        "riktning": "sou_till_riksdagsdokument",
        "beteckning": sou_beteckning,
        "riksdagsdokument": dokument,
    }
    if not dokument:
        svar["anmarkning"] = (
            f"Inga riksdagsdokument hittade som behandlar SOU {sou_beteckning}."
        )
    return svar


def _riksdagsdok_till_souer(beteckning: str) -> Dokumentrelationer:
    """Riksdagsdokument YYYY/YY:N → SOU-beteckningar som nämns i texten."""

    docs = _riksdag_sok(beteckning, antal=10)

    exakta = [
        d for d in docs
        if (d.get("beteckning") or "").strip() == beteckning.split(":")[-1].strip()
        or beteckning in ((d.get("rm") or "") + ":" + (d.get("beteckning") or ""))
    ]
    if not exakta:
        exakta = docs

    if not exakta:
        raise ToolError(
            f"Hittade inget riksdagsdokument med beteckning {beteckning}. "
            "Kontrollera beteckningen (formen ÅÅÅÅ/ÅÅ:N)."
        )

    huvud_dok = _riksdagsdokument(exakta[0])
    if not huvud_dok["dok_id"]:
        raise ToolError(f"Riksdagens svar saknade dokument-id för {beteckning}.")

    try:
        html = _hamta_riksdag_text(huvud_dok["dok_id"])
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(
            f"Kunde inte hämta dokumenttexten för {beteckning} från Riksdagens "
            f"öppna data ({getattr(e, 'reason', e)}). Försök igen senare."
        ) from e

    sou_refs = sorted(set(re.findall(r"\bSOU\s+(\d{4}:\d+)\b", html)))

    svar: Dokumentrelationer = {
        "riktning": "riksdagsdokument_till_sou",
        "beteckning": beteckning,
        "dokument": huvud_dok,
        "sou_beteckningar": sou_refs,
    }
    if not sou_refs:
        svar["anmarkning"] = (
            "Inga prefixade SOU-beteckningar (formen \"SOU YYYY:N\") hittades i "
            "dokumenttexten. Dokumentet kan referera till SOU:er via parentesform "
            "\"(YYYY:N)\" eller via betänkandets titel — verifiera mot dokumentets "
            "referenslista på riksdagen.se."
        )
    return svar


# Verktygslistan styrs av .env-flaggorna. find_document_relations är alltid
# aktiv, eftersom den söker i riksdagens API och inte i SOU-PDF:erna.
if not SOU_SOKNING_AKTIV:
    mcp.remove_tool("search_sou")
    mcp.remove_tool("get_sou")
if not SOU_HAMTNING_AKTIV:
    mcp.remove_tool("fetch_sou_content")


# ── Startpunkt ─────────────────────────────────────────────────────────────────

def _initiera() -> None:
    """Skapar databasschemat och städar PDF-rester från en avbruten körning."""
    _initialisera_schema()
    stada_pdf_cache()


if __name__ == "__main__":
    starta(mcp, standardport=8004, initiera=_initiera)
