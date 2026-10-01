# Work log - project export / import

## What
- Export: Project settings -> System -> "Export project" downloads `<slug>.recapper.zip` containing the whole
  `projects/<slug>/` folder (chapter downloads, frames, scripts, audio, renders, uploaded media, settings).
  Checkbox "Include rendered videos" (off = skips recap.mp4 and merged/, much smaller).
- Import: Projects screen -> "Import project" restores a zip as a NEW project (never overwrites; renamed
  `-2` etc. if the slug exists), then opens it.
- Import rewrites the exporting machine's absolute project path inside chapter JSON (manifest, framer
  mapping; `export_info.json` in the zip records it) and regenerates each chapter's media_settings.json.

## Files
- `tools/framer/projects.py`: `export_archive`, `import_archive`, `_rebase_paths` (zip-slip checked, staged in `.import_*`).
- `tools/framer/app.py`: `GET /api/projects/<slug>/export`, `POST /api/projects/import`.
- `tools/framer/index.html`: Import button, Export card, upload icon. `.gitignore`: `.import_*/`. `CLAUDE.md` note.

## Verified
Exported the real stellar-swordmaster project (613 MB without videos), imported into a scratch projects
folder: paths rebased to the new location, videos excluded, bad/duplicate imports handled. UI not opened in a browser.
Reinstalling the app: export first (the uninstaller deletes `projects/` only if you say yes), then Import.
