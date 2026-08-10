# Ändringslogg

Alla betydande ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshantering följer [Semantic Versioning](https://semver.org/).

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

[1.0.0]: https://github.com/MagnusKolsjo/mcp-for-LiU-sok-SOU/releases/tag/v1.0.0
