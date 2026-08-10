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
git clone https://github.com/MagnusKolsjo/mcp-for-LiU-sok-SOU.git
cd mcp-for-LiU-sok-SOU
pip install -r requirements.txt
cp config.example.env .env
# Fyll i LIU_API_KEY i .env
```

## API-nyckel

Testnyckel `test` fungerar direkt men returnerar max 5 träffar per sökning.

För fullständig åtkomst: mejla Tekniska utvecklingsgruppen, Linköpings
universitetsbibliotek (`sysaccount@bibl.liu.se`) och beskriv kortfattat hur
du avser använda API:et.

## Konfiguration i MCP-klient

Lägg till i din MCP-klientkonfiguration (t.ex. `claude_desktop_config.json`):

```json
"liu-sou": {
  "command": "/absolut/sökväg/till/mcp-for-LiU-sok-SOU/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/mcp-for-LiU-sok-SOU/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/mcp-for-LiU-sok-SOU"
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


## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Den största utredningen i cachen är **1 131 996 tecken** — långt över gränsen.
`fetch_sou_content` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken; `0` ger hela texten som ett uttryckligt val. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett kapat svar säger alltid ifrån med en rad som anger vilket teckenintervall som visas och det färdiga anropet för att fortsätta. Kapningen sker på ordgräns, aldrig mitt i
ett ord.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `SOU_MAX_TECKEN`.

## Licens

Koden publiceras under [AGPLv3](LICENSE).

LiU:s API-tjänst har egna användningsvillkor — du måste inhämta en egen API-nyckel
och följa Linköpings universitetsbiblioteks villkor. SOU-texterna är offentliga
handlingar.
