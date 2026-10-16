# Work log

Performance and structure refactor, on branch `perf-refactor` (not pushed). One commit per step:

| Commit | What |
|---|---|
| `c08eba4` (main) | All earlier uncommitted work, as-is |
| `fe8b521` | Tests: project loading, chapter state, bulk download/resume, pipeline step data, bulk actions, delete |
| `8d03220` | Each chapter's state in its own `chapters/<n>/chapter.json`; `project.json` keeps the chapter index; automatic migration |
| `1f75885` | Cached per-project totals (speaking pace, Claude tokens), keyed by the files they come from |
| `24b6c75` | Job/batch polling pauses while the tab is hidden and resumes at once |
| `a4014be` | App runs on waitress (`make_server`), `waitress` in requirements.txt |
| `2a09376` | Select all / range select cover every chapter; details loaded on demand |
| `eb22f82` | `app.py` split into `routes/` (blueprints) and `services/` |
| `bf3023e` | `index.html` split into HTML + `static/css/app.css` + `static/js/icons.js` + `static/js/app.js` |
| `f218d94` | Fix: migration race that could replace the `project.json.v1` original |

**Storage:** chapter files exist only for chapters with progress, in the project folder (the Drive folder for shared
projects), so two people saving different chapters never write the same file. Status changes no longer touch
`project.json`. Schema 1 projects migrate on first load (original kept as `project.json.v1`). The real projects have
already migrated; `project.json.v1` of `my-disciples-are-all-big-villains` is not the original (see fix commit); its chapter
data is intact.

**Tests:** `.venv\Scripts\python.exe -m unittest discover -s tests -t .` (85 tests, no new dependency).

**Not done in code:** `deps.py check` reports `google-genai` off-pin; it did before these changes.

**Empty chapter folders** were removed from every project under `projects/` (local, git-ignored, no commit).
