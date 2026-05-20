"""
db.py — Databashjälpfunktioner för liu-sou MCP-servern.

Hanterar anslutning, schema-init och cache-operationer mot PostgreSQL eller SQLite.
Välj backend via DATABASE_URL i .env:
  postgresql://<ANVÄNDARE>:<LÖSENORD>@localhost:5432/<DATABASNAMN>  — PostgreSQL
  sqlite:///liu_sou_cache.db                                        — SQLite
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

DATABASE_URL        = os.getenv("DATABASE_URL", "")
PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "1"))

logger = logging.getLogger(__name__)


# ── Databashjälpfunktioner ─────────────────────────────────────────────────────

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

        # ── Baseline-schema (låst vid v1.1.0) ─────────────────────────────────
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

        # ── Migrationer ──────────────────────────────────────────────────────────
        # Lägg till ALTER TABLE IF NOT EXISTS-satser här efter v1.1.0.
        # Ändra aldrig baseline-schemat ovan efter första GitHub-publicering.
        # (inga ändringar ännu)

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
