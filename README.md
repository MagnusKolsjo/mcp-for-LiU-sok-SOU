# mcp-for-LiU-sok-SOU

MCP-server för sökning och läsning av svenska statliga offentliga utredningar (SOU) 1922–idag, samt tvåriktad traversering av dokumentkedjan proposition ↔ SOU.

Datakällan är Linköpings universitetsbiblioteks fulltextdatabas, som täcker:
- **1922–1996:** Inskannade och OCR-behandlade SOU:er (digitaliserade av Kungliga biblioteket)
- **1997–idag:** Digitala SOU:er från riksdagens öppna data

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `search_sou` | Fritextsökning i SOU-fulltext med valfritt årsfilter |
| `get_sou` | Metadata för specifik SOU, t.ex. `2025:108` |
| `fetch_sou_content` | Laddar ned och extraherar text ur SOU-PDF (OCR-fallback för äldre dokument) |
| `find_document_relations` | Tvåriktad kedjesökning: SOU→propositioner/betänkanden/skrivelser eller prop→SOU:er |

### Dokumentkedjans logik

```
Prejudikat (HD/HFD)
  → SFS-paragraf
    → Proposition  ←→  find_document_relations  ←→  SOU
                                                       → Remissvar
```

`find_document_relations` känner automatiskt av riktningen:
- Indata `2025:108` (YYYY:N) → SOU-beteckning → söker riksdagsdokument som behandlar SOU:n
- Indata `2025/26:136` (YYYY/YY:N) → riksdagsdokumentbeteckning → extraherar SOU-refs ur dokumenttexten

## Krav

- Python 3.10+
- Beroenden: se `requirements.txt`
- API-nyckel från Linköpings universitetsbibliotek (testnyckel `test` ger max 5 träffar per sökning)
- Tesseract OCR (valfritt, för äldre skannade SOU:er):
  - macOS: `brew install tesseract tesseract-lang`
  - Linux: `apt install tesseract-ocr tesseract-ocr-swe tesseract-ocr-eng`

## Installation

```bash
git clone https://github.com/<anvandare>/liu-sou-mcp.git
cd liu-sou-mcp
pip install -r requirements.txt
cp config.example.env .env
# Fyll i LIU_API_KEY i .env
```

## API-nyckel

Testnyckel `test` fungerar direkt men returnerar max 5 träffar per sökning.

För fullständig åtkomst: kontakta Anders Fåk, Linköpings universitetsbibliotek
(`anders.fak@liu.se`) och beskriv kortfattat hur du avser använda API:et.
Det är en akademisk öppen datatjänst — förfrågan är vanligtvis okomplicerad.

## Konfiguration i MCP-klient

Lägg till i din MCP-klientkonfiguration (t.ex. `claude_desktop_config.json`):

```json
"liu-sou": {
  "command": "/absolut/sökväg/till/liu-sou-mcp/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/liu-sou-mcp/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/liu-sou-mcp"
}
```

## Köra testskript

```bash
python3 01_test_api.py        # Verifierar API-anrop och svarsformat
python3 02_explore_fields.py  # Visar tillgängliga fält och URL-typer
```

## Kända begränsningar

- **Testnyckelns 5-träffarsgräns:** `rows`-parametern ignoreras med testnyckel `test`.
- **OCR-kvalitet för äldre SOU:er:** Inskannade dokument från 1922–1996 kan ha artefakter,
  framför allt degraderade svenska tecken (å/ä/ö).
- **Falska positiv i `find_document_relations`:** Riksdagens sökning matchar på
  fritextnivå, vilket kan ge enstaka irrelevanta träffar om SOU-beteckningen råkar
  likna en riksdagsbeteckning (t.ex. `2025:108` kan matcha dokument med beteckning `2025/108`).
- **KB URN-upplösning:** Äldre SOU:er (1922–1996) kräver ett extra steg för att lösa
  PDF-URL:en via KB:s URN-resolver. Kan vara något långsammare.

## Licens

Koden publiceras under [AGPLv3](LICENSE).

LiU:s API-tjänst har egna användningsvillkor — du måste inhämta en egen API-nyckel
och följa Linköpings universitetsbiblioteks villkor. SOU-texterna är offentliga
handlingar.
