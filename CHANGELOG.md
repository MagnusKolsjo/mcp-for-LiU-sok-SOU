# Ändringslogg

Alla betydande ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshantering följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [3.0.0] — 2026-09-26

### Tillagt

- Skyddad PDF-extraktion (`pdftext_skydd.py`): extraktionen körs i en egen
  process, i block om `LIU_PDF_SIDBLOCK` sidor, under en minnesvakt
  (`LIU_PDF_MAX_MINNE_MB`) och tidsgräns (`LIU_PDF_TIDSGRANS_S`) per block. Ett
  block som passerar gränserna läses med ren textutvinning i stället, så att
  ett enskilt bildtungt dokument inte kan fälla servern.
- OCR-kö (`ocr_ko/ko.jsonl` + `ocr_ko/filer/`, mappen styrs av
  `LIU_OCR_KO_MAPP`): dokument med sidor utan textlager, eller där ett block
  fick läsas med ren textutvinning, noteras här tillsammans med en kopia av
  PDF:en, så att de kan köras genom en bättre OCR senare utan att laddas ned
  igen.

### Ändrat

- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen: `mcp-for-LiU-sok-SOU/3.0`.
- **Brytande:** servern kräver MCP Python SDK 2.x (`mcp>=2.0,<3`) och är
  omskriven till `MCPServer`. Verktygens namn, parametrar, standardvärden och
  beskrivningar är oförändrade.
- **Brytande:** http-läget kräver `MCP_API_KEY`. Utan nyckel avbryts uppstarten
  med exitkod 2 i stället för att servern startar utan autentisering. Fel nyckel
  ger 403, saknad header 401.
- **Brytande:** `search_sou`, `get_sou` och `find_document_relations` returnerar
  strukturerade svar med utdataschema i stället för markdown-text:
  `search_sou` → `{totalt, visade, traffar}`, `get_sou` → `{namn, delar}`,
  `find_document_relations` → `{riktning, beteckning, riksdagsdokument}` från en
  SOU eller `{riktning, beteckning, dokument, sou_beteckningar}` från ett
  riksdagsdokument. Riksdagsdokumenten har med `dok_id`. `fetch_sou_content`
  returnerar som förut markdown-text.
- **Brytande:** förväntade fel ger `isError` med ett förklarande meddelande i
  stället för en text som börjar med `FEL:`. Det gäller även en SOU- eller
  riksdagsbeteckning som inte finns.
- Verktygen har titlar och annotationer som markerar dem som läsande, och
  verktygslistan har cachningshintar.
- Verktygen körs på arbetstrådar; PDF-hämtningen och textextraktionen är
  skyddade av ett lås så att samtidiga anrop inte krockar i filcachen.
- Serverns instruktioner och README beskriver att LiU:s databas uppdateras med
  eftersläpning och att nyare SOU:er finns i Riksdagens öppna data.
- Kontaktadressen för API-nyckel är `ep@ep.liu.se`, som API:et självt anger.

### Rättat

- OCR-språket för sidor utan textlager var engelska, eftersom inget uttryckligt
  språk angavs till `pymupdf4llm`. Svenska tecken (å, ä, ö) i OCR-lästa SOU:er
  blev därför fel. OCR-språket sätts nu uttryckligen med `LIU_OCR_SPRAK`
  (standard `swe+eng`).
- http-läget startade inte (`streamable_http_app()` anropades på ett objekt som
  inte fanns). Det går nu via `mcp_transport.py` på standardport 8004.
- En avvisad `LIU_API_KEY` gav ett obegripligt JSON-tolkningsfel. Svaret känns
  nu igen och felet säger att nyckeln avvisas och hur en ny begärs.
- API-nyckeln skrivs inte längre i klartext i loggfilen.
- Läs vidare-raden i ett kapat svar från `fetch_sou_content` pekade på
  `fran_tecken + max_tecken`. Kapningen sker på ordgräns, så nästa utdrag
  hoppade över det avkapade ordet. Raden anger nu utdragets faktiska slut, så att
  på varandra följande utdrag tillsammans blir exakt den sammanhängande texten.
  Raden är dessutom ett komplett anrop med `url` (obligatorisk), `max_tecken`
  och, när de angetts, `sidor`. Det sista utdraget har ingen läs vidare-rad.
- En fil som inte går att läsa som PDF ger ett begripligt fel och tas bort ur
  filcachen, så att nästa försök inte fastnar på samma fil.

### Borttaget

- Den egna filbeskrivaromdirigeringen kring `pymupdf4llm`-anropet
  (`_tysta_subprocess_stdout`). Extraktionen körs nu i en egen process i
  `pdftext_skydd.py`, så den behövs inte längre.
- SSE-transporten och den egna Starlette-appen för autentisering. Streamable
  HTTP är den enda http-transporten.

## [2.0.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `fetch_sou_content`**, med standardtaket
  `SOU_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). En utredning i cachen är
  **1 131 996 tecken** som mest, vilket överskrider MCP-protokollets storleksgräns per
  svar — anropet misslyckades då helt, utan att anroparen hade någon väg runt.
  Ett kapat svar avslutas med en rad i klartext:
  `[Visar tecken 1–59 994 av 1 131 996. Läs vidare: fetch_sou_content(namn="…", fran_tecken=59994)]`.
  Kapningen sker på ordgräns, aldrig mitt i ett ord. `max_tecken=0` ger hela texten
  som ett uttryckligt val.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Cachen och databasen lagrar fortfarande hela
texten — trunkeringen gäller bara svaret till anroparen, så sökning och indexering
påverkas inte.

---


### Ur tidigare opublicerat arbete

### Brytande ändringar
- **K6 — Beteckningsformat:** SOU-beteckningar i returobjekt ändrade från `"YYYY/N"` till `"YYYY:N"` för att matcha det format API:et tar emot och som riksdagsdata använder. Berör fältet `sou_beteckning` i svaren från `find_document_relations`.
- **K7 — HTTP-transport:** Standardtransport för `MCP_TRANSPORT=http` ändrad från SSE (`sse_app()`) till Streamable HTTP (`streamable_http_app()`). SSE används som fallback om det nyare biblioteket saknas. Klienter som förlitar sig på SSE-endpunkten `/sse` behöver uppdateras.
- **K9 — Modulstruktur:** Databasfunktionerna extraherade till separat modul `db.py`. Installations­skript som importerar interna funktioner direkt från `mcp_server` behöver uppdateras.

## [1.2.0] — 2026-05-20

### Tillagt
- **Verifikationsloop i `find_document_relations`:** Kandidatdokument från riksdagens API verifieras nu mot riksdagens fulltextindex innan de returneras. Minskar falskt positiva träffar vid sökning av SOU → riksdagsdokument. Batch-storlek 2 parallellt (upp till 8 kandidater), sekventiellt därefter — balanserar svarstid mot belastning på riksdagens servrar.

### Ändrat
- Felmeddelande i `find_document_relations` (riktning riksdagsdok → SOU) förtydligat: anger nu att parentesreferenser utan `SOU`-prefix inte fångas och hänvisar till dokumentets referenslista på riksdagen.se.

### Rättat
- `SyntaxError` i `02_explore_fields.py` (rad 68): trasigt regex-mönster ersatt med korrekt escape-sekvens.

## [1.1.0] — 2026-05-15

### Tillagt
- **PostgreSQL/SQLite-stöd:** Dubbelt backend-mönster med schema `liu_sou` (PostgreSQL) eller separat SQLite-fil. Tabeller: `pdf_cache` (fulltext-cache) och `sync_status` (checkpoints). Schema skapas automatiskt vid serveruppstart.
- **DB-cache för fulltext:** `fetch_sou_content` kontrollerar `liu_sou.pdf_cache` innan nedladdning — återbesök returnerar direkt från databasen utan ny PDF-hämtning.
- **Radering direkt efter extraktion:** PDF-filen tas bort omedelbart efter att fulltexten sparats i databasen. `pdf_sokvag` nollas i DB. Förhindrar att `pdf_cache/`-mappen växer okontrollerat.
- **`stada_pdf_cache()`:** Städfunktion som raderar kvarliggande PDF-filer (t.ex. om serverprocessen kraschade mellan extraktion och radering). Jämför filålder mot `PDF_CACHE_TTL_DAGAR`.
- **`PDF_CACHE_TTL_DAGAR`** i `.env` (standard: 1 dag): säkerhetsventil för `stada_pdf_cache()`.
- **`SOU_SOKNING_AKTIV`** i `.env` (standard: true): styr om `search_sou` och `get_sou` exponeras. Sätt till false om en annan server i installationen hanterar SOU-sökning.
- **`SOU_HAMTNING_AKTIV`** i `.env` (standard: true): styr om `fetch_sou_content` exponeras och fulltext lagras i `liu_sou.pdf_cache`. Sätt till false för att undvika dubbellagring i installationer med flera aktiva servrar.
- **Dynamisk verktygslista:** `lista_verktyg()` filtrerar exponerade verktyg baserat på `SOU_SOKNING_AKTIV` och `SOU_HAMTNING_AKTIV`. `find_document_relations` exponeras alltid (söker riksdagens API, inte SOU-PDF:er).
- **`stada_pdf_cache()` anropas vid uppstart** i `main()` — städar eventuella rester från körningar där serverprocessen kraschade mellan extraktion och filradering.

### Ändrat
- `DATABASE_URL` standarddatabas ändrad till `riksdagstryck` (konsekvent med övriga MCP-servrar i projektet).
- Verktygsdefinitioner refaktorerade till namngivna konstanter (`_TOOL_SEARCH_SOU` m.fl.) för att möjliggöra dynamisk verktygslista.
- User-Agent bumpad till `liu-sou-mcp/1.1`.

## [1.0.0] — 2026-05-12

### Tillagt
- `search_sou`: fritextsökning i SOU 1922–idag med årsfilter och sortering
- `get_sou`: metadata för specifik SOU-beteckning (beteckning, titel, år, ISBN, PDF-URL)
- `fetch_sou_content`: PDF-nedladdning och textextrahering med OCR-fallback för KB-digitaliserade SOU:er 1922–1996
- `find_document_relations`: tvåriktad kedjesökning — SOU → propositioner/betänkanden/regeringsskrivelser samt riksdagsdokument → SOU-beteckningar i dokumenttext
- Brus-filter för årsöversikter (Kommittéberättelse m.fl.) som nämner alla SOU:er
- KB URN-upplösning för äldre SOU:er (1922–1996) via weburn.kb.se
- Relativa cache-sökvägar ankras mot skriptets mapp (skyddar mot skrivskyddat cwd i MCP-klienter)
- stdio- och HTTP-transportstöd med Bearer-token-autentisering i HTTP-läget

[3.0.0]: https://github.com/MagnusKolsjo/mcp-for-LiU-sok-SOU/releases/tag/v3.0.0
[1.0.0]: https://github.com/MagnusKolsjo/mcp-for-LiU-sok-SOU/releases/tag/v1.0.0
