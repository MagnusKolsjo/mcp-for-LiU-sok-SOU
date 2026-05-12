#!/usr/bin/env python3
"""
02_explore_fields.py — Utforskar LiU:s SOU-API: fält, URL-typer och PDF-åtkomst.

Kör med: python3 02_explore_fields.py
Kräver: LIU_API_KEY i .env

Fynd från utforskningen (2026-05-12):
  Tillgängliga fält: id, namn, ar, nummer, titel, isbn, url, fritext
  - id: internt Solr-id (int som sträng för gamla, alfanumerisk för nya)
  - namn: SOU-beteckning, t.ex. "2023:14"
  - ar: utgivningsår (int)
  - nummer: löpnummer (int)
  - titel: utredningens titel (lowercase, kan ha avslutande /)
  - isbn: ISBN-13 (finns på de flesta moderna SOU:er, saknas ofta i äldre)
  - url: direktlänk till PDF eller KB URN-resolver (se nedan)
  - fritext: fulltexten som lista med ett element (kan vara >100 000 tecken)
  - _version_: Solr-intern, ej användbar

  URL-typer:
  - data.riksdagen.se/fil/<UUID> — direktlänk till PDF (1997+, digitala)
  - urn.kb.se/resolve?urn=urn:nbn:se:kb:sou-<id> — KB URN-resolver (1922–1996, skannade)

  KB URN-resolver kräver två extra steg:
  1. GET urn.kb.se → redirect till weburn.kb.se/metadata/<id>/SOU_<id>.htm
  2. Skrapa PDF-länken ur HTML:en (format: weburn.kb.se/sou/<n>/urn-nbn-se-kb-digark-<id>.pdf)

  Testnyckel: låser rows till max 5 oavsett rows-parameter.
  Paginering: fungerar via start-parametern.
  Sortering: fungerar via sort-parametern (t.ex. sort=ar+desc).
  Årsfilter: fq=ar:[2020 TO 2024] fungerar (Solr range syntax).
"""

import os
import json
import re
import urllib.request
import urllib.parse
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("LIU_API_KEY", "test")
API_BASE = os.getenv("LIU_API_BASE", "https://www2.bibl.liu.se/api/sou_api/getdata.aspx")
HEADERS = {"User-Agent": "liu-sou-mcp/0.1 (akademiskt projekt; kontakt via GitHub)"}


def sok(params: dict) -> dict:
    params["api_key"] = API_KEY
    params["wt"] = "json"
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def hamta_pdf_url_fran_kb_urn(urn_url: str) -> str | None:
    """Löser en KB URN-URL till en direktlänk för PDF-nedladdning.
    
    Steg 1: Följ redirect från urn.kb.se till weburn.kb.se/metadata/...
    Steg 2: Skrapa PDF-länken ur HTML-sidan.
    
    Returnerar PDF-URL eller None om länken inte hittades.
    """
    req = urllib.request.Request(urn_url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    
    pdf_lankar = re.findall(r'href=["'](https://weburn\.kb\.se/[^"\']+\.pdf)["']', html)
    return pdf_lankar[0] if pdf_lankar else None


def main():
    print("=== Fältutforskning ===")
    svar = sok({"q": "namn:2023\\:83", "fl": "id,namn,ar,nummer,titel,isbn,url"})
    if svar["response"]["docs"]:
        doc = svar["response"]["docs"][0]
        print("Fält i ett modernt dokument:")
        for k, v in doc.items():
            print(f"  {k}: {repr(v)}")
    
    print()
    print("=== URL-typer ===")
    
    # Modern SOU — direktlänk
    svar_ny = sok({"q": "namn:2023\\:14", "fl": "namn,url"})
    if svar_ny["response"]["docs"]:
        doc = svar_ny["response"]["docs"][0]
        print(f"Modern ({doc['namn']}): {doc['url']}")
    
    # Äldre SOU — KB URN-resolver
    svar_gammal = sok({"q": "ar:1922", "fl": "namn,url", "sort": "ar asc"})
    if svar_gammal["response"]["docs"]:
        doc = svar_gammal["response"]["docs"][0]
        print(f"Äldre ({doc['namn']}): {doc['url']}")
        
        if "urn.kb.se" in doc["url"]:
            print("  → Löser KB URN...")
            pdf_url = hamta_pdf_url_fran_kb_urn(doc["url"])
            print(f"  → PDF-URL: {pdf_url}")
    
    print()
    print("Fynd: Se docstring i denna fil för komplett sammanfattning.")


if __name__ == "__main__":
    main()
