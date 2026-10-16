# Work log

Fixed chapter 5's download failure ("Using http2=True, but the 'h2' package is not installed").

- `webtoon-downloader` asks httpx for HTTP/2, which needs the `h2` package; nothing in the requirements pulled it in, so every webtoons.com download failed.
- `requirements.txt`: added `h2>=4.1`. Installed it in `.venv` (h2, hpack, hyperframe); CUDA torch still `True`, `deps.py check` clean.

**Run next:** click Retry download on chapter 5 in the pipeline card. Other PCs get the fix by re-running `setup.bat` (or the in-app update).
