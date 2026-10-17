# Work log - covers, descriptions, wider cards

- New `sites/meta.py`: reads og:image / og:description from the series page; all three adapters' `fetch_series` now also return `cover_url` and `description` (kingofshojo overrides with the real synopsis, since its og text is SEO boilerplate).
- New `tools/framer/series_meta.py`: stores `description` in project.json and downloads the cover to `<project>/cover.jpg`. Called on project creation and Refetch (`routes/projects.py`); new route `GET /api/projects/<slug>/cover`.
- `projects.list_projects()` returns `description` and `cover` (file mtime, for cache busting).
- Projects screen: cover as the card image, 2-line description, cards min 420px, "last active" moved to the host line so it no longer overlaps the name.
- Backfilled the two existing projects with a URL (100-Year-Old Top Chef, The Stellar Swordmaster). Other projects: press Refetch on the project page.
- Restart the app to load the new route. mgeko.cc not tested live (no mgeko project here).
