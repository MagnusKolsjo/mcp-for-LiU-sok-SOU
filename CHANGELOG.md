# Ändringslogg

Alla betydande ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshantering följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- `DATABASE_URL` standarddatabas ändrad till `riksdagstryck` (konsekvent med övriga arbetsströmmar i projektet).
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
