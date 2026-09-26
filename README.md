# mcp-for-LiU-sok-SOU

MCP-server för sökning och läsning av svenska statliga offentliga utredningar (SOU) 1922–idag, samt tvåriktad traversering av dokumentkedjan proposition ↔ SOU.

Datakällan är Linköpings universitetsbiblioteks fulltextdatabas, som täcker:
- **1922–1996:** Inskannade och OCR-behandlade SOU:er (digitaliserade av Kungliga biblioteket)
- **1997–idag:** Digitala SOU:er från riksdagens öppna data

LiU:s databas uppdateras med viss eftersläpning, så de senast utgivna SOU:erna
kan saknas där. Nyare SOU:er finns i [Riksdagens öppna data](https://data.riksdagen.se/)
(dokumenttyp `sou`). Hittar `get_sou` inte en nyligen utgiven beteckning är det
oftast förklaringen.

## Verktyg

| Verktyg | Beskrivning | Svar |
|---|---|---|
| `search_sou` | Fritextsökning i SOU-fulltext med valfritt årsfilter | Strukturerat: `totalt`, `visade`, `traffar` |
| `get_sou` | Metadata för specifik SOU, t.ex. `2025:108` | Strukturerat: `namn`, `delar` |
| `fetch_sou_content` | Laddar ned och extraherar text ur SOU-PDF (OCR-fallback för äldre dokument) | Markdown-text |
| `find_document_relations` | Tvåriktad kedjesökning: SOU→propositioner/betänkanden/skrivelser eller prop→SOU:er | Strukturerat: `riktning`, `riksdagsdokument` eller `dokument` och `sou_beteckningar` |

Alla verktyg bär annotationer som markerar dem som läsande. De strukturerade
svaren har ett utdataschema, så att en klient kan kedja anropen utan att tolka
text: en träff från `search_sou` har den `url` och det `namn` som
`fetch_sou_content` tar, och `sou_beteckningar` från `find_document_relations`
går direkt in i `get_sou`. Förväntade fel, till exempel en okänd beteckning
eller en avvisad API-nyckel, returneras som fel (`isError`) med ett meddelande
som säger vad som kan göras.

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
- MCP Python SDK 2.x (`mcp>=2.0,<3`); övriga beroenden i `requirements.txt`
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

För fullständig åtkomst: mejla LiU Electronic Press (`ep@ep.liu.se`) med
ämnesraden "SOU API-nyckel" och beskriv kortfattat hur du avser använda API:et.
Det är den adress API:et självt hänvisar till när en nyckel avvisas.

En nyckel som inte längre godtas ger ett tydligt fel från `search_sou` och
`get_sou` med samma anvisning. Testnyckeln fungerar under tiden.

## Konfiguration i MCP-klient

Lägg till i din MCP-klientkonfiguration (`mcpServers`):

```json
"liu-sou": {
  "command": "/absolut/sökväg/till/mcp-for-LiU-sok-SOU/.venv/bin/python3",
  "args": ["/absolut/sökväg/till/mcp-for-LiU-sok-SOU/mcp_server.py"],
  "cwd": "/absolut/sökväg/till/mcp-for-LiU-sok-SOU"
}
```

## HTTP-transport

Servern kör stdio (lokal MCP-klient som startar processen) eller Streamable
HTTP (delad drift bakom en URL), valt med `MCP_TRANSPORT` i `.env`:

```bash
MCP_TRANSPORT=http
MCP_HOST=127.0.0.1
MCP_PORT=8004
MCP_API_KEY=<SLUMPMÄSSIG_NYCKEL>
```

Endpointen är `http://<MCP_HOST>:<MCP_PORT>/mcp`. Varje anrop kräver headern
`Authorization: Bearer <MCP_API_KEY>`; utan header svarar servern 401, med fel
nyckel 403. **http-läget kräver `MCP_API_KEY`**: saknas nyckeln avbryts
uppstarten med exitkod 2 i stället för att servern startar utan skydd. Generera
en nyckel med `python3 -c "import secrets; print(secrets.token_hex(32))"`.

## Köra testskript

```bash
python3 01_test_api.py        # Verifierar API-anrop och svarsformat
python3 02_explore_fields.py  # Visar tillgängliga fält och URL-typer
```

## PDF-extraktion och OCR

PDF:erna görs om till markdown med `pdftext_skydd.py`. Tre saker skyddar
servern:

- **OCR-språk.** Sidor utan textlager OCR:as med Tesseract på språken i
  `LIU_OCR_SPRAK` (standard `swe+eng`). Utan uttryckligt språk används
  engelska, och å, ä och ö blir fel.
- **Minnesvakt.** Extraktionen körs i en egen process i block om
  `LIU_PDF_SIDBLOCK` sidor. Passerar processen `LIU_PDF_MAX_MINNE_MB` eller
  `LIU_PDF_TIDSGRANS_S` avbryts den, och blocket läses med ren textutvinning
  i stället. Ett enskilt bildtungt dokument kan då inte fälla datorn eller
  servern.
- **OCR-kön.** Dokument med sidor utan textlager, eller med block som lästes
  med ren textutvinning, noteras i `ocr_ko/ko.jsonl`, och PDF:en sparas i
  `ocr_ko/filer/`. De kan senare köras genom en bättre OCR utan att laddas
  ned igen. Mappen styrs av `LIU_OCR_KO_MAPP`.

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
