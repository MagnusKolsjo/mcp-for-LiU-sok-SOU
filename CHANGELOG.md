# Ändringslogg

Alla betydande ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshantering följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
