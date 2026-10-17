# Work log - per-project folder

Existing projects can now be moved/pointed to a different folder (e.g. fix `RECAPPER/angrynymph` -> `RECAPPER/bebe`).

- `app_settings.py`: `project_folders()` / `set_project_folder()` - per-PC slug -> folder override.
- `projects.py`: `project_dir`, `list_projects`, `delete` honour the override; new `relocate()` (moves files + rebases stored paths, or just points at a folder that already holds the project).
- `routes/projects.py`: `GET/POST /api/projects/<slug>/folder` (refused while the project is busy).
- `index.html`, `static/js/app.js`: "Project folder" card in project settings (path field, "Move the files" checkbox, Change folder button). Empty path = default location.
- `tests/test_projects.py`: `RelocateTest`.

Run: `.venv\Scripts\python.exe -m unittest tests.test_projects`, then restart the app.
