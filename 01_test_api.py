#!/usr/bin/env python3
"""
01_test_api.py — Testar LiU:s SOU-API och verifierar anropsstruktur och svarsformat.

Kör med: python3 01_test_api.py
Kräver: LIU_API_KEY i .env (testnyckel "test" ger max 5 träffar)
"""

import os
import json
import urllib.request
import urllib.parse
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("LIU_API_KEY", "test")
API_BASE = os.getenv("LIU_API_BASE", "https://www2.bibl.liu.se/api/sou_api/getdata.aspx")
HEADERS = {"User-Agent": "liu-sou-mcp/0.1 (akademiskt projekt; kontakt via GitHub)"}


def sok(params: dict) -> dict:
    """Skickar en förfrågan till LiU:s SOU-API och returnerar svaret."""
    params["api_key"] = API_KEY
    params["wt"] = "json"
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    print("=== Test 1: Fritextsökning ===")
    svar = sok({"q": "fritext:digitalisering", "fl": "namn,titel,ar", "rows": "5"})
    print(f"numFound: {svar['response']['numFound']}")
    print(f"Returnerade: {len(svar['response']['docs'])} dokument")
    for doc in svar["response"]["docs"]:
        print(f"  {doc['namn']} ({doc['ar']}) — {doc['titel'][:60]}")

    print()
    print("=== Test 2: Namnbaserad sökning ===")
    svar2 = sok({"q": "namn:2023\:14", "fl": "id,namn,titel,ar,isbn,url"})
    print(json.dumps(svar2["response"]["docs"], ensure_ascii=False, indent=2))

    print()
    print("=== Test 3: Årsintervallfilter [2020 TO 2024] ===")
    svar3 = sok({"q": "fritext:skatt", "fq": "ar:[2020 TO 2024]", "fl": "namn,titel,ar", "sort": "ar desc"})
    print(f"numFound: {svar3['response']['numFound']}")
    for doc in svar3["response"]["docs"]:
        print(f"  {doc['namn']} ({doc['ar']}) — {doc['titel'][:60]}")

    print()
    print("=== Test 4: Nyaste SOU:erna ===")
    svar4 = sok({"q": "*:*", "fl": "namn,titel,ar", "sort": "ar desc"})
    for doc in svar4["response"]["docs"]:
        print(f"  {doc['namn']} ({doc['ar']}) — {doc['titel'][:60]}")

    print()
    print("=== Test 5: Tillgängliga fält ===")
    svar5 = sok({"q": "namn:2023\:83", "fl": "id,namn,ar,nummer,titel,isbn,url"})
    if svar5["response"]["docs"]:
        print("Fält i ett dokument:", list(svar5["response"]["docs"][0].keys()))
    print()
    print(f"Obs: testnyckel låser antal träffar till 5 oavsett rows-parameter.")


if __name__ == "__main__":
    main()
