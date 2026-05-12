# Ändringslogg

Alla betydande ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versionshantering följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Tillagt
- `search_sou`: fritextsökning i SOU 1922–idag med årsfilter
- `get_sou`: metadata för specifik SOU-beteckning
- `fetch_sou_content`: PDF-nedladdning och textextrahering med OCR-fallback för KB-digitaliserade SOU:er
- `find_document_relations`: tvåriktad kedjesökning SOU↔riksdagsdokument
- Brus-filter för Kommittéberättelse och liknande årsöversikter
- KB URN-upplösning för SOU:er 1922–1996
- stdio- och HTTP-transportstöd (Bearer-token-autentisering i HTTP-läget)
