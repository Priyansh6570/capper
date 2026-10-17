# Work log - new Projects screen

- Projects screen restyled from the "Projects.dc.html" design (Claude Design project "Recapper chapters UI redesign"): totals row (complete / in pipeline / not started), cards with a cover slot (initial placeholder for now), host link, running pill, split progress bar.
- `projects.py`: `list_projects()` now returns `running` (chapters in a live batch, from batch.json).
- `index.html`, `static/js/app.js` (`renderProjectsList`), `static/css/app.css` (.pcard, .pstats).
- Not done: real cover images / descriptions (needs the scrapers in `sites/` to save og:image + og:description), the cards/rows toggle and light/dark prop from the design (the app has its own theme button).
- Commit before pressing "Check for updates" (it runs git reset --hard).
