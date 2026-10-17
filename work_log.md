# Work log

- Volume settings card (narration 50-600%, music 0-30%) -> `projects.py`, `s7_assemble.py`, `index.html`, `app.js`.
- Removed the "Download more chapters" button; pipeline shows "N videos done" instead of cards; merge no longer embeds a player on the Chapters page.
- `defaults/settings.json` re-saved from `100-year-old-top-chef`; `audio` group label added in `project_defaults.py`.
- Splash: 16ms frames, port probed every 0.25s (`splash.py`).
- Merged `perf-refactor` into `main` and pushed `main`.
- Note: the in-app "Check for updates" runs `git reset --hard HEAD` first, which wipes uncommitted work - commit before pressing it.
