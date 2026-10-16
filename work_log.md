# Work log

Removed Gemini, Kaggle, Groq and the terminal pipeline, plus the unused voice paths and packages, on `perf-refactor`
(not pushed). One commit per group:

| Commit | What |
|---|---|
| `c0081be` | Gemini script engine, its API key storage and UI (G1, G4, G5, G6) |
| `a00c1db` | Shared `gemini` settings become `script`; `model` and `script_engine` dropped; migration (schema 3, original kept once as `project.json.v2`) |
| `26707d5` | Split step (G3); a saved batch that names it still loads |
| `cdd010f` | `kaggle/` (K1, K2) |
| `d066e99` | Stages 2-5, `llm.py` (Groq/Ollama), `make_fixture.py`, `tools/new_series.py`, `tools/speed_audio.py`, `docs/CHAPTER_WORKFLOW.md` (A1-A4, Q1, Q2); orchestrator now runs audio then video; manifest drops the old fields and still loads old files (`clean` -> `frames`) |
| `e062362` | Full Chatterbox, emotion mode, draft voice (B2, B3); fallback is Turbo GPU then Turbo CPU |
| `fb97488` | MoviePy group and 72 unneeded packages (C1-C3) |
| `95ca93d` | Model cache location is a per-PC setting (`hf_home`, applied as `HF_HOME` by `config.py`) |
| `ad23c2d` | Two bugs found by the end-to-end run, both in deleting a project |

**Not removed from the survey list:** `en-core-web-sm` (Kokoro's text processing loads it) and `decorator` (librosa needs it).

**Data deleted (untracked):** `gemini_parts/` folders (105 MB), legacy `work/` and `output/` (2.2 GB), `yolov8n.pt`,
`~/.EasyOCR`, Gemini entries in Windows Credential Manager, `GROQ_API_KEY`/`MANHWA_LLM`/`MANHWA_OLLAMA_MODEL` in `.env`
(it is empty now), empty chapter folders in every project.

**Model cache:** moved from `C:\Users\priya\.cache\huggingface` to `D:\hf_cache` (setting in `app_settings.json`); the Turbo model
re-downloaded there (4.2 GB with Kokoro). `setup.bat` picks a second fixed drive with 20 GB free per PC, else keeps the default;
the Projects screen shows and changes it; the installer's uninstall reads it (compiled OK with Inno Setup).

**Verified:** 153 tests; pyflakes; requirements hook; smoke imports; `cv2` and CUDA torch after the OpenCV reinstall; all four real
projects migrated (originals kept); a full chapter on a local throwaway copy (stored PDF -> stitch -> Claude script -> export ->
Turbo audio -> video: 1920x1080, 87.6 s, then deleted); Turbo on the CPU.

**Run next:** the app is running the new code (restart it from the tray if you restart Windows). `deps.py check` is clean.
