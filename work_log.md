# Work log - Shorts: pick by merit across the range

- `tools/framer/shorts.py`: the find prompt (`_count_rule`) now asks for the best moments of the WHOLE range
  by score (skip weak chapters, several from one chapter allowed, none if nothing scores >= 6). `select()`
  ranks by score first; a chapter's best-before-second only breaks ties. Before, it forced one pick per
  chapter, which is why early chapters always won.
- `tools/framer/static/js/app.js`: while Claude chooses, the progress row reads "RANGE / Studying N chapters"
  instead of "CH <lowest>", and no chapter group is shown for it. After the find, only chapters it picked
  get rows, so a skipped first chapter no longer shows as if it got a short.
- `tests/test_shorts.py`: spread test replaced by merit tests (59 pass).

Cost note: the find is one `claude` call per ~150k chars of scripts (thinking already off, effort low),
then one small call per picked short. Fewer shorts requested = fewer calls.

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`

# Work log - Fix: white bars beside webtoon strips (Return of the Mount Hua Sect)

- `stages/s1_pdf_to_pages.py`: new `_normalize_widths()`; the strip width is now the width most rows have, and slices of another width (cover 518px, outro 548px among 331px pages) are scaled to it instead of padding everything to the widest with white.
- Existing chapters keep their old strip until re-stitched; re-stitching changes strip coordinates, so frames/boxes of that chapter must be redone.

# Work log - YouTube publishing + Telegram remote (new `tools/framer/publishing/` package)

- Channels: `oauth.py` (browser sign-in, PKCE, scopes youtube / youtube.upload / yt-analytics.readonly, channel choice), `channels.py`, `youtube_api.py` (plain `requests`: resumable upload, thumbnails, playlists, visibility, analytics, token refresh). State (tokens, queue, bot token, quota) lives in `<repo>/publishing/`, git-ignored, never in a project/Drive folder.
- Queue: `uploads.py`, `enqueue.py` (merged video -> items waiting approval, split into parts above 11h55m via `videos.py`; rendered shorts -> scheduled items), `slots.py` (N shorts/day spread between two hours), `metadata.py` (Claude writes short title/description/tags), `worker.py` (uploader thread: full videos PRIVATE after approval, shorts PUBLIC at their slot, quota carry-over via `quota.py`/`pacific.py`, retries with backoff, audit-lock detection and automatic switch to public in `sync.py`, evening summary).
- Telegram: `telegram.py` (polling bot, pairs with the first `/start`, approval cards with Approve / Skip / Edit title, /queue /schedule /private /uploaded /stats /run /status, notifications); `services/remote.py` gives it /run and /status.
- App wiring: `routes/publishing.py`, page `static/publishing.html` (sidebar "Publishing"), project settings card "YouTube upload", "Queue for YouTube" button on merged videos, `services/batching.py` (`start_batch` shared with the route, Telegram notifications of pipeline events, shorts queued after `shorts_render`), `projects.py` (`settings.youtube`, `thumbnail` asset), `branding.chapters_of`, `app.py` starts the worker and the bot.
- Tests: `tests/test_publishing.py` (fake YouTube/Telegram). Not tested live: connecting the channel, the bot and a real upload.

# Work log - Telegram /run publishes by itself; work_log is append-only; default client path

- `work_log.md`: restored the earlier entries from git (the shorts entry from HEAD, plus the uncommitted white-bars entry, re-added from what was in the file) and appended the publishing entry. `CLAUDE.md` now says to APPEND to the log, never overwrite it.
- `/run` finishes the job: `services/remote.run` starts the batch with `publish=<chapters>`; `batching.start_batch` keeps that in `batch.meta["publish"]` (saved with the batch, so it survives a restart); `batch.BatchEnv.finished` is called when the batch runs out of work and `services/autopublish.py` merges the chapters (`services/merging.merge_videos`), renders the intro/outro when the project has them on, and calls `enqueue.full_video`, which sends the approval card to Telegram. Nothing is merged if a chapter did not finish or the project has no YouTube channel; Telegram says why.
- `services/merging.py`: the merge checks and the ffmpeg command moved out of `routes/merge.py` (`prepare_merge`, `merge_videos`, `merge_slot`) so the web merge and the Telegram flow share them.
- `static/publishing.html`: the client JSON path defaults to `D:\Work\ReCapper-secrets\client_secret.json` (the last path used is remembered).
- Tests: `tests/test_publishing.py` (merging, autopublish, /run) and `tests/test_batch.py` (finished hook). Whole suite: 332 OK.
- Not tested live (needs your sign-in, bot and phone). Run: `.venv\Scripts\python.exe tools/framer/app.py`; in Telegram send `/run <project> 22-45`.

# Work log - Fix: Telegram /run failed with "'NoneType' object has no attribute 'run'"; /projects added; sign-in redirect fix

- Cause: the bot's controls (`/run`, `/status`) were only handed over by `app.main()`. The tray launcher never calls it, so under the tray the bot was started by the Publishing page's token save without controls, and the YouTube uploader worker was never started either.
- `app.start_background()` (resume batches, uploader, bot) is now the one startup call, used by `app.main()` and `tray_launcher.py`. `telegram.start()` and `telegram.configure()` now require the controls, and `routes/publishing.py` passes `services.remote.CONTROLS`.
- New `/projects` command lists the project names exactly as `/run` takes them (`remote.project_names`); `/run` takes everything before the last word as the name, so spaces and hyphens work.
- Earlier in this session: `publishing/oauth.py` redirect server is now threaded with an idle-connection timeout (a browser's spare connection blocked the single-threaded one, so the redirect failed), 10 minute sign-in window, a new Connect closes the previous attempt, errors show on the Publishing page.
- Tested: 335 unit tests OK, and `/run` end to end through the real polling loop and handlers started like the tray launcher does, against a fake Telegram server and a throwaway project (the pipeline started and reported its failure on a fake URL; unknown projects were refused). Not tested with the real bot.
- Restart ReCapper (Quit from the tray, start it again) so the fix is loaded, then send `/projects` and `/run <name> 11-12`.

# Work log - Telegram planner: failure recovery, auto shorts, upload plan, suggestions, merge approval

- Failure recovery: `batch.py` gets `BatchEnv.failed` (called when a chapter fails), `Batch.retry(n, from_start=False)` (back to the failed step, or all the plan's steps; revives a finished batch) and `Batch.skip(n)` (chapter becomes `cancelled`). `services/cards.announce_failure` sends chapter, stage, error summary with Retry from failed stage / Redo all stages / Skip chapter / Show full error. Website: `POST /api/projects/<slug>/batch/chapters/<n>/retry` (`{from_start}`) and `/skip`, the same under `/shorts/batch/`; the failed chapter row has Retry from <stage> / Redo all stages / Skip chapter buttons (the error and log were already shown).
- Shorts: setting `shorts.auto_make` (default on, toggle in the Shorts settings). When a main batch finishes, `batching._pipeline_finished` starts the Shorts pipeline over the chapters it rendered (one range, so one Claude study rather than one per chapter; not while the next chapter is still rendering). `batching.start_shorts` is the old route body, shared by the route, the automatic start and the new `/shorts <project> <chapters>`. `/schedule` now lists Videos and Shorts in separate sections with their local upload times.
- Upload plan: `settings.plan` = `{days: [0-6], min_minutes}` (all seven = daily; default daily, 30 min), in the YouTube upload settings card. `services/planning.py`: average minutes per rendered chapter (8 until one exists), chapters per upload, buffer = queued (waiting/queued/uploading full videos) + merged-not-queued + finished-unmerged video; buffer days = whole uploads it covers x 7 / upload days per week; next upload day; next suggested chapters. `/plan` prints it per project, neediest first.
- Suggestions: `services/advisor.py` (thread started by `app.start_background`, checks every 5 min). Sends the project with the smallest buffer (only below 14 days, with a channel and chapters left) as a Run / Later card when no pipeline runs, Telegram is paired, the Claude CLI is ready and Claude has room. Room = both windows in `claude_usage.snapshot()` (filled from the CLI's rate-limit events on every Claude call) are under that project's `batch.five_hour_pct` / `seven_day_pct`, the same limits its batches hold at; a window past its reset counts as empty. Per-run token totals are not needed for this: the windows already measure what is left. The same chapters are not suggested again for 4 hours (whether answered Later or ignored); a different project or range is sent at once.
- Merge approval: `cards.propose_merge` after each pipeline: earliest finished chapters that are in no merged video or upload, in a row, up to the minimum minutes; card shows chapters and total, buttons Merge / Change range / Later. Change range asks for chapters (`telegram.ask`, any command cancels), checks them and answers with a new card showing the total before you confirm. Merge re-checks the range, then `autopublish.publish` (merge, intro/outro, upload approval card) on a thread. `/run` no longer sets `meta["publish"]`; `start_batch` lost its `publish` argument.
- Telegram plumbing: card buttons go through `Controls.act` and `telegram.Reaction`; `telegram.ask` replaces the title-only `_awaiting_title`. Card state is in `publishing/cards.json` (git-ignored with the rest of `publishing/`).
- Tests: new `tests/test_planner.py` (every flow through `telegram._handle` against a fake Telegram, including a forced failure at the stitch step and Retry from failed stage, which does not repeat download), retry/skip in `tests/test_batch.py`; `test_publishing.py` and `test_shorts.py` updated for the removed auto-merge and the moved route code. Whole suite: 380 OK. Not tested against real Telegram or a real Claude usage reading.
- Restart ReCapper, set each project's upload plan, then try `/plan`, `/shorts <project> 1-3` and `/run <project> 1-3`.

## 2026-10-27 - mgeko 403 diagnosis + log copy button
- Batch chapter log box now has a Copy button (static/js/app.js batchLogHtml + click handler on #batchList, css .blogcopy in static/css/app.css).
- sites/mgeko.py: a Cloudflare challenge 403 now raises a clear message. Cause: mgeko.cc serves a JS "Just a moment..." challenge (cf-mitigated: challenge); plain requests AND curl_cffi chrome impersonation both still get 403, so a real browser (or a cf_clearance cookie) is needed. Not fixed yet.

## 2026-10-27 - mgeko Cloudflare cookie
- app_settings.site_session(site) reads app_settings.json "site_sessions"; sites/mgeko.py sends that cookie + user agent on mgeko.cc page requests only (not the image host).

## 2026-10-27 - mgeko cookie paste fix
- sites/mgeko.py _clean_cookie: tolerates a cf_clearance value pasted as "name: value" / "name=name:value" (the saved one was "cf_clearance=cf_clearance:<value>", which Cloudflare ignored). Verified: chapter 191 page now loads (HTTP 200, 12 images found). Retry the download in the app.

## 2026-10-29 - Shorts uploads off really stops them; shorts music library

- **Bug:** with "Upload finished shorts automatically" (`youtube.auto_shorts`) off, scheduled shorts kept uploading. The
  switch only gated queueing new ones. Now `publishing/worker.py` never releases or sends a short of a project with it
  off (`_shorts_allowed`), and saving the setting off deletes that project's unsent shorts (`uploads.drop_unsent_shorts`,
  called from `routes/projects.py`). Removed 11 stale items of revenge-of-the-iron-blooded-sword-hound from `publishing/queue.json`.
- **Music library for shorts:** new `tools/framer/short_music.py` + routes `/api/projects/<slug>/shorts_music` (GET/POST,
  PATCH/DELETE `/<id>`, `/<id>/audio`). Tracks (`settings.shorts.library`, files in `assets/shorts_music/`) each have a theme
  (comedic, aura, emotional, serious, dramatic, op, action, mystery, sad, hype, or custom). `settings.shorts.music_share`
  (default 0.4; narration gets 0.6). Settings page: "Music library" section in Shorts.
- Claude picks a `theme` per short in `shorts.write_script` (only when the project has a library), stored in the plan.
  `stages/s8_shorts.py` `pick_track` chooses the track (theme, then fitting length, no repeats in a chapter) and the short
  is rendered exactly as long as the track: `s7_assemble.prepare_render(pick_music=, music_share=)` holds stills longer
  (`_pad_to`) and `_audio_filters(balanced=True)` mixes loudness-matched narration/music by share, no ducking. Index entries
  of rendered shorts get `music`/`music_theme`. Not in the project defaults (`NOT_SHARED`).
- Tests added (`tests/test_shorts.py`, `tests/test_publishing.py`). **Restart the app** to load the fixes. Existing plans have no theme (any track is used); re-run the shorts find for theme-based picks.

## 2026-10-29 - Music drop points for shorts

- Each library track can have `drop_s` (the second the build-up ends); set when adding or edited per track in the Shorts
  music library (`short_music.clean_drop`, PATCH `drop_s`).
- `shorts.write_script(tracks=...)` now shows Claude the library (`id | theme | seconds | drop`); it picks `track`, writes the
  narration to the track's length (about 2.5 words/s) and names `peak_line`, the line that must start at the drop. Stored in the
  plan as `track`, `theme`, `peak` (replaces the earlier theme-only choice). `shorts_jobs` sets the short's duration to the track's.
- `stages/s8_shorts.py` plays the chosen track (`pick_track(short_track=)`) and passes the peak line to `s7.prepare_render`, which
  holds the stills before it (`_hold_before`) so it starts exactly at the drop, then pads to the track length. Tests added.
- Restart the app; re-run the shorts find so new plans carry track/peak.

## 2026-10-29 - Project page: paging, range view, last-done/next-up; parallel download + stitch

- **Chapter list** (`static/js/app.js`, `index.html`, `static/css/app.css`): "Show N more" replaced by real pagination (50 per page, buttons
  labelled by chapter range, e.g. 51-100, prev/next, ellipsis for many pages). The project now loads with no per-chapter detail
  (`?limit=0`); detail is fetched only for the page on screen, so jumping to chapter 300 is instant. New bar above the list: "N of M
  have a finished video", **Last done #X**, **Next up #Y** (links jump to them), **Select next [N] from #Y**, and a **Go to #** box.
  Typing a range and pressing **Show** displays exactly that range; **Select range** shows and selects it; "Show all" clears it.
  Added **Select page**; Select all now applies to the chapters in view. The project opens on the page holding the next chapter to run.
  "Finished video" = chapter status `complete` (the status is derived from disk, so a deleted recap.mp4 drops it).
- **Parallel steps** (`batch.py`): `LANE_WORKERS = {"download": 3, "stitch": 2}` - those lanes run several chapters at once; all other
  steps stay at one (GPU / Claude / ffmpeg). The finish-time estimate accounts for the workers. Raise/lower the numbers in `LANE_WORKERS`.
- No manifest, stage or orchestrator change. Restart the app and hard-refresh the browser.

## 2026-10-29 - Wrong project shown / slow project load

- **Bug:** opening one project could show another's name and chapters: a slow `loadCurrentProject` of the previous project
  (big Drive-hosted projects take seconds) finished late and overwrote `CURRENT_PROJECT`. Now each load has a sequence number
  and a stale response is dropped; `openProjectScreen` stops if another project was opened meanwhile; paging state resets per project.
- **Speed:** `/api/projects/<slug>` takes `auto=1` (details for the page holding the next chapter to run) or `from`/`to`, so the first
  screen costs one request instead of two (each walks every chapter folder on Drive).
  Restart the app + hard-refresh.

## 2026-10-30 - Backup dialog: compare a range with Drive, then clear or back up

- **Why the PC kept filling:** of ~58 GB for one project, page images/panels/editor tiles (~37 GB, "Page images", unticked by default so never
  backed up or clearable) and the downloaded PDFs (3.4 GB, not part of any backup kind) were never touched by Clear.
- **Dialog** (`static/js/app.js`, `index.html`, `static/css/app.css`): "Back up to Drive" now opens on the chapters selected in the list (whole
  project if none) and scans only those against Drive (`GET /api/projects/<slug>/backup?chapters=...`, much faster on a big Drive project).
  A table shows per kind: on this PC / already in Drive / not in Drive. Two actions: **1. Clear from this PC** (everything already verified in
  Drive, all clearable kinds) and **2. Back up what is missing** (ticked kinds). Page images stay unticked for backing up (large, rebuildable).
- **`chapter_backup.py`:** new kind `download` (the chapter PDF; the Drive copy is the stored PDF in the chapter folder - `Kind.flat`). It is
  cleared only once the chapter's video is rendered. Counts as a done step when cleared (`services/batching._STAGE_OF_KIND`). Test added.
- Scripts and key mappings still always stay on the PC. Restart the app and hard-refresh.

## 2026-10-30 - Chapter-end advertisement art no longer narrated

- **Problem:** the last panel(s) of many chapters are an ad for another series (a lone girl/woman figure). Claude's pass 1 treated it as story
  art, it entered the story-so-far / character sheet, and pass 2 kept ending chapters on "a golden figure appears, nobody knows how she
  is connected".
- **Fix** (`tools/framer/claude_engine.py`): new `AD_RULE` (what such an ad looks like, and how a real late reveal differs: the chapter's own
  characters react to it) added to both passes. Pass 1 must mark ad panels `junk` and keep them out of scenes, characters, uncertain and the
  chapter summary; pass 2 must not end on an unconnected figure or say nobody knows who she is. Character-sheet entries whose name or
  description says teaser/ad/advert/promo are dropped on load and never saved (`_without_ads`), which also cleans the existing sheet.
- Applies to scripts generated from now on. Already-written chapters (and their saved one-line summaries in `story_memory.json`, which may still
  mention the figure) are not rewritten: regenerate the script of an affected chapter to fix it. No stage or manifest change.

## 2026-10-30 - Backup dialog: page images ticked by default, clear blockers explained

- Page images (`chapter_backup.KINDS["pages"]`) are now ticked by default (they are most of a chapter's size).
- Clear only ever deletes files already verified in Drive, so ticking a kind never enables it; the Clear text now lists what is blocked ("not in
  Drive yet: page images 16 GB. Back it up first"). New **Back up, then clear** button runs the backup and then the clear in one go.

## 2026-10-30 - Backup dialog: clear from this PC without backing up

- The Clear card now has its own per-kind ticks (frames, audio, page images, video, PDF), independent of what is ticked for backup, and a
  checkbox **Also delete files that are not in Drive**. Without it, only files verified in Drive are cleared (restored automatically later);
  with it, the rest is deleted for good (`chapter_backup.purge_kind`, `POST .../backup_clear` with `force`). Page images deleted this way are
  rebuilt by stitching the PDF again. The chapter PDF is never deleted unless Drive has it. Scripts/key mappings always stay. Tests added.
- Page images are unticked for *backup* by default again; the Clear card has them ticked, so they can be deleted without uploading.

## 2026-11-02 - Shorts paused
- Via the running app's API (`POST /api/projects/<slug>/settings`) set `shorts.auto_make=false` and `youtube.auto_shorts=false` for all 5 registered projects. Switching `auto_shorts` off runs `uploads.drop_unsent_shorts`, which removed all 23 scheduled shorts (and the 30 failed ones) from `publishing/queue.json`. Live shorts and the removed full video stay.
- No code changed. To resume: re-enable "auto make" and "auto shorts" in each project's settings.

## 2026-11-03 - Token-saving quality modes for the script step; automatic shorts off by default
- **Modes** (`tools/framer/quality.py`): Full (unchanged), Balanced (images at `claude.balanced_max_side`, default 512, plus OCR text and bubble positions), Lite (OCR text and positions only, no images; its prompts say Claude only has dialogue and captions). Same pass-2 prompt and style rules in all three. Batches: 40 / 60 / 200 panels per pass-1 call. `claude_engine.py` (`_batch_content`, `run_chapter(mode=, balanced_side=)`), `services/script_jobs.py`.
- **OCR** (`ocr.py`): EasyOCR on the existing CUDA torch, CPU fallback (also on GPU OOM), models in `models/easyocr` (git-ignored). `easyocr` added to `requirements.txt`, `torchvision==0.26.0+cu128` to `requirements-gpu.txt` and to the torch constraints in `deps.py`; `deps.py install` now downloads the models (covers setup.bat, the installer and the in-app updater). `tools/smoke_imports.py` imports easyocr.
- **Cache** (`panel_inputs.py`): `work/<ch>/claude/ocr.json` and `send_<side>/P###.jpg`, keyed to the panel file, reused across modes and re-runs. A saved Claude run in another mode is discarded; OCR text and copies are kept.
- **Scope**: `settings.claude.quality` per project; `quality` (override) and `script_mode` (last generated) per chapter in `chapter.json`; `POST /api/projects/<slug>/chapters/<n>/quality`. UI: project settings (Claude section), a Quality select and a "Script: <mode>" badge on each chapter row, and a Quality select next to "Generate script with Claude" in the editor. Changing it and generating again redoes only that chapter.
- **Usage log**: `projects/<slug>/script_usage.jsonl`, a line per finished script run (project, chapter, mode, panels, input/output/cache tokens, cost, seconds); the job log's chapter total names the mode.
- **Shorts**: `settings.shorts.auto_make` defaults to off; project schema 4 turns it off once in existing projects (`project.json.v3` keeps the old file). Shorts code is untouched; the long-video pipeline never waits on it. Tests updated.
- **Measured** on return-of-the-mount-hua-sect ch 5 (180 panels), pass-1 input estimate: Full ~106k tokens in 5 requests, Balanced ~45k in 3, Lite ~4k in 1.
- Tests: `tests/test_script_quality.py`. Run `.venv\Scripts\python.exe -m unittest discover -s tests -t .`; on other PCs `python deps.py install` fetches easyocr and the models.

## 2026-11-05 - Shorts: no gaps between lines, 70/30 mix, shorts paused (My Disciples Are All Big Villains)
- `stages/s7_assemble.py`: a short with music no longer stretches its stills to the track length (`_pad_to`) nor holds the lines before the peak (`_hold_before`); both left silent gaps between lines. The video now ends with the narration, the track is started later (`music_seek`, `-ss`) so its drop lands on the peak line, and the music fades out over the last 1.5s (`MUSIC_FADE_OUT`).
- Project settings (My Disciples...): `shorts.music_share` 0.3 (music 30% / narration 70%), `youtube.auto_shorts` off (shorts are not uploaded until reviewed). `shorts.auto_make` was already off.
- Deleted every short and plan of the project; re-rendered 2 from the old chapter 346 plan: chapters/346/output/shorts/short_01.mp4, short_02.mp4 (37.8s and 34.7s, were 51.3s).
- Run: `.venv\Scripts\python.exe -m unittest discover -s tests -t .` (tests of _pad_to/_hold_before still exist; helpers kept).

## 2026-11-05 (2) - Shorts: fast-paced frames, playlist link, only action / big-reveal shorts
- Black shorts: chapter 346's frames had been cleaned off the PC. Deleted them; found + rendered 2 shorts from chapters 401 and 402 (they still had frames): chapters/401|402/output/shorts/short_01.mp4.
- `stages/s8_shorts.py`: a line now shows every panel of its source lines (not the first of each), cut to ~1.6s per frame (`FRAME_SECONDS`, `fit_frames`, after the line is voiced); neighbouring panels fill in when a line names too few.
- `tools/framer/shorts.py`: the find prompt picks only ACTION (fights) and BIG REVEALS (mystery / emotional plot reveal); comedy and ordinary drama are excluded (applies to every project).
- Playlist: new `settings.youtube.shorts_playlist` (projects.py). The uploader appends "Full story playlist: <url>" to a short's description (`worker._snippet`) and posts it as a comment (`youtube_api.Client.comment`, +1 write of quota). The YouTube API cannot pin comments: pin it in Studio. Restart the app so it knows the new setting, then set it for the project (the link is already written in its project.json).

## 2026-11-06 - call-to-action (CTA) clip + "insert after chapter N" in merge
- New add-on stage `stages/s9_cta.py` (outside STAGE_ORDER, like s8): reads `work/<ch>/cta/plan.json`, voices it with the chapter's voice (s6) and renders with s7 at >=1080p -> `output/<ch>/cta/cta.mp4`. Constants in `config.py` (CTA_*).
- New `tools/framer/cta.py` + `routes/cta.py`: picks frames with no dialogue (EasyOCR finds no text AND no pure-white blanked-bubble boxes), writes the plan, runs s9, saves the clip to `projects/<slug>/cta/cta.mp4`. Words live in `projects/<slug>/cta/script.txt` (`pace: text` per row, created from DEFAULT_SCRIPT, edit freely). Routes: `GET /api/projects/<slug>/cta`, `/cta_stream` (SSE build), `/projects/<slug>/cta/cta.mp4`.
- Merge: `prepare_merge/merge_videos(..., cta_after=N)` and `merge_stream?chapters=..&cta_after=N` place the clip right after chapter N, normalized to the chapters' format so the stream-copy merge is kept. Output name gets `_cta<N>`. UI: "Call-to-action video after chapter [..]" + "Make CTA video" in the merge bar (index.html, app.js).
- Tests: `tests/test_cta.py` (full suite 426 OK). Built the clip for my-disciples-are-all-big-villains-2 (29s).
- Run next: `git add` the new files (check_requirements only sees tracked files), restart the app, hard-refresh, then merge with the new dropdown. Telegram/auto merges do not add the CTA yet.

- Wrote channel_analysis.docx (channel stats analysis + growth plan) in the project root.

## 2026-11-06 (2) - Shorts music off, 5 shorts/day auto upload, channel_analysis.docx rewritten
- `config.SHORTS_MUSIC = False`: gates the shorts music library in `projects._library_snapshot` (what stage 8 reads) and `services/shorts_jobs.py` (what the find/script step is given). Tracks/settings kept. "Music library" section hidden in `tools/framer/index.html`. Find prompt in `tools/framer/shorts.py` restored to the original (action / comedy / drama / mystery). Kept `fit_frames` (a new frame every ~1.6s, no repeats).
- My Disciples Are All Big Villains: `shorts.auto_make` and `youtube.auto_shorts` on; channel shorts schedule `per_day` 5 (was 10), existing queue rescheduled. Playlist link (already set, verified against the channel's real playlist) goes in each short's description and first comment; pinning must be done in Studio.
- Quota note: ~1,600 units per upload of 10,000/day; 5 shorts + 1 long video is at the limit.
- `channel_analysis.docx` now holds only the answer about shorts changes and multi-channel expansion. Restart the app.

## CTA script rewrite + reference frames
- Created `cta_script/`: `script.txt` (new CTA script, same `pace: text` format as `projects/<slug>/cta/script.txt`), `frame_guide.txt` (what image to make per line), and `reference_frames/` (49 full-res frames from my-disciples-are-all-big-villains-2, ch 0/2/401/450, sorted by character and mood).
- No code changed. The live script is still the old DEFAULT_SCRIPT in `tools/framer/cta.py`; when approved, copy `cta_script/script.txt` over `projects/my-disciples-are-all-big-villains-2/cta/script.txt`.

## New CTA video from custom scenes
- `tools/framer/cta.py`: new `own_frames(slug)`. If `<project>/cta/frames/` has pictures, `build()` skips the OCR frame search and the generated end card, and shows picture N on script line N (error if fewer pictures than lines). With no such folder behaviour is unchanged.
- My Disciples project: `cta/script.txt` replaced with `cta_script/new/script.txt` (old kept as `script_old.txt`), the 6 scenes copied to `cta/frames/`, clip rebuilt -> `G:\My Drive\RECAPPER\bebe\my-disciples-are-all-big-villains-2\cta\cta.mp4` (27.7s, 1080p, chapter 500's voice/background/watermark). Restart the app to pick up cta.py.

## Balanced script depth fix
- `tools/framer/claude_engine.py`: `WORDS_PER_PANEL = 17`. The narration prompt (pass 2, all modes) now gives a length guide of about 17 words per story panel and says to use every beat/feeling/stake/world detail in the notes (it used to say "no target length"). `BALANCED_RULES` now asks pass 1 for the same depth of scene notes as Full.
- `tests/test_script_quality.py`: two tests for the above.
- Test on ch 500 (Balanced, 47 panels, scratch copies only, nothing in the project touched): narration step only 671 -> 904 words; full re-run 671 -> 832 words. Existing scripts are not changed; regenerate a chapter's script to get the new density.

## 2026-11-07 - Cut Claude script token use (story context)
- Problem: every pass-1/pass-2 call sent the whole series memory (520 chapter summaries + 1,267-entry character sheet, ~140k tokens), so a ~50-panel chapter cost ~730k cache-write tokens.
- `tools/framer/claude_engine.py`: `_story_so_far` now sends only the last `RECENT_CHAPTERS` (15, a constant, not a setting) summaries; `_character_sheet` sends only characters mentioned in those summaries or in the current chapter's text (OCR / earlier batches in pass 1, the breakdown in pass 2); name words shared by >3 characters don't match. `_context` takes the new `relevant` text.
- Measured on ch 522: context ~140k -> ~3k tokens, 48 of 1,268 characters kept. Tests: 428 OK.
- Next: run one chapter and compare `script_usage.jsonl` cache_write (was ~730k); check narration still names characters correctly (Full mode has no OCR, so matching uses summaries only).

## 2026-11-08 - Downloaded missing chapter 80 (The World's Best Engineer)
- kingofshojo was missing ch80 of `the-world-s-best-engineer-2` (same series as "The Greatest Estate Developer"). Fetched it from roliascan.com (image list from `/auth/chapter-content?chapter_id=66851`, 99 webp pages) with a one-off script, retrying timeouts.
- Wrote `<projects>/the-world-s-best-engineer-2/chapters/80/the-worlds-best-engineer-chapter-80.pdf` + `chapter.json` (status `downloaded`), matching neighbouring chapters. No repo code changed.
- Next: open the project in the app and run Stitch -> script for chapter 80.

## 2026-11-08 - Added roliascan.com as a download source
- New adapter `tools/framer/sites/roliascan.py` (registered in `sites/__init__.py`): series metadata from `/manga/<slug>/` + `/auth/manga-chapters` (chapter list, whole numbers only), chapter images from `/auth/chapter-content`. Accepts a series URL or any `/read/...` chapter URL; the chapter's id is looked up by number.
- `sites/images.py`: `fetch_pages` now retries each image up to 4 times (a slow image host had timed out a whole chapter). Applies to all adapters.
- Site lists updated: `index.html` (new-project sources, download bar), `static/js/app.js`, `services/downloads.py`, `preflight.py` messages.
- Tested live: series (223 chapters), ch80 download from a series URL = 99-page PDF. Tests: 428 OK. No stage/manifest change.
- Next: restart the app; paste a roliascan.com series URL in New project. Old projects keep their own source.

## 2026-11-08 - slow pages + engineer shorts
- Slow pages: `projects._stored_states` re-read ~100 chapter.json per project load (~4ms/read on this disk, ~7s for the project list); now cached 30s per project (dropped on `save_chapter`). `branding.duration_of` ran one ffprobe per chapter per page load; durations now persist in `cache/durations.json`. Measured: project list 6s -> 0.1s warm, project page 3.4s -> 0.5s. Needs an app restart to take effect.
- Started a Shorts batch for the-world-s-best-engineer-2, chapters 21-50, max 10 shorts. Rendered shorts are auto-queued into the next free upload slots (`enqueue.shorts_of_chapter`).

## 2026-11-08 - script prefetch (no quality change)
- `services/script_jobs.py`: new `prepare_inputs` (panels + OCR text + downscaled copies, the same cache files `claude_engine.run_chapter` makes) and `prefetch` (background thread, failures only logged). `run_script_job` now calls `prepare_inputs`, then `job.after_prepare`.
- `services/batching.py`: once a chapter's inputs are ready, `_prefetch_next_script` prepares the next stitched chapter that needs a script while Claude works on the current one. `services/jobs.py`: `Job.after_prepare`.
- Claude receives identical inputs; only timing moves. Measured prep for ch134: 118s cold, 2.4s cached. Takes effect after an app restart. Tests (script, batch) pass.

## 2026-11-09 batch: reuse saved script after strip cleared
- services/batching.py: if a chapter's strip is missing but its script is saved, the batch rebuilds the strip, keeps the script (no Claude call) and cuts frames from mapping.json.

## 2026-11-09 shorts: music share, other title
- shorts.py, services/shorts_jobs.py, publishing/enqueue.py, publishing/metadata.py: optional project field `other_title`. With it, a short's closing says a full recap is coming soon (no playlist/pinned-comment line) and its description names the other title.
- The World's Best Engineer project.json (backup: project.json.pre-edit): other_title = The Greatest Estate Developer, shorts music_share 0.3 (narration 70
## 2026-11-09 shorts: music share, other title
- shorts.py, services/shorts_jobs.py, publishing/enqueue.py, publishing/metadata.py: optional project field `other_title`. With it, a short's closing says a full recap is coming soon (no playlist/pinned-comment line) and its description names the other title.
- The World's Best Engineer project.json (backup: project.json.pre-edit): other_title = The Greatest Estate Developer, shorts music_share 0.3 (narration 70%), full-video description mentions the other name.
- Restart the app. Only shorts made from now on change; already-written short scripts and queued uploads keep the old text.

## 2026-11-11 my-disciples-are-all-big-villains-2: added chapter 530
- mgeko lacked ch 530. Added it to the project (project.json + chapters/530/chapter.json), downloaded it from roliascan.com (ch530-295208, 8 pages; stored PDF in the project folder), and ran the full pipeline (stitch, script, export, audio, video) as a batch of just 530. Output: projects/.../chapters/530/output/recap.mp4.
- The earlier paused batch (562-574 queued) was cancelled at the user's choice; re-queue those chapters when wanted.
- No code changed.

## Closure video (end-of-series thank-you) + "Add closure video" merge toggle
- `cta.py` now handles two clip kinds (`KINDS`: `cta`, `closure`). Closure = same add-on under `projects/<slug>/closure/` (`script.txt`, `frames/`, `closure.mp4`); it needs its own pictures (one per script line, number order), no OCR search. `DEFAULT_CLOSURE_SCRIPT` (9 lines, `{series}` = project name).
- `stages/s9_cta.py`: `run(m, folder, filename)` + `--folder/--file` args (defaults unchanged, so the CTA clip is untouched). `config.py`: `CLOSURE_FOLDER/FILE`.
- Routes: `/api/projects/<slug>/closure`, `/closure_stream`, `/projects/<slug>/closure/closure.mp4`; `merge_stream` takes `closure=1`.
- `services/merging.py`: `prepare_merge/merge_videos(..., closure=True)` puts the closure after the LAST chapter (after the CTA when that is also last); output name gets `_closure`.
- UI (index.html, app.js): in the merge card, a checkbox "Add closure video", plus Make/Remake closure video and play. Checkbox is enabled once the clip exists and 2+ chapters are selected.
- Tests: `tests/test_cta.py` (+4, 13 pass). `test_planner...waits_until_the_situation_changes` fails with or without these changes.
- My Disciples project: `closure/script.txt` written, frames 1-9 copied to `closure/frames/` (G:\My Drive\RECAPPER\bebe\my-disciples-are-all-big-villains-2\closure). NOT built yet: frame 6 (weekly calendar) shows WED twice and no THU, and has art bleeding over the FRI/SAT tabs; replace `closure/frames/6.png`, then restart the app, hard-refresh and press "Make closure video".

## Closure video built (My Disciples)
- Frame 6 fixed (days MON-SUN, "TRANSMIGRATED" caption) and copied to `closure/frames/6.png`; built via `cta.build(slug, "closure")` -> `G:\My Drive\RECAPPER\bebe\my-disciples-are-all-big-villains-2\closure\closure.mp4` (56.5s, 1920x1080, 9 lines / 9 frames, chapter 577's voice, background and watermark).
- Run next: restart the app, hard-refresh, tick "Add closure video" in the merge card when merging the last range (it goes after the last selected chapter).

## Closure video rebuilt with updated frames
- All 9 updated frames (no Gemini sparkle; new frame 5 = series grid in the ornate style of frame 6) copied from `cta_script/end video/frames/` to `closure/frames/1..9.png` and the clip remade -> `G:\My Drive\RECAPPER\bebe\my-disciples-are-all-big-villains-2\closure\closure.mp4` (56.5s, 1080p). No code changes.

## 2026-11-14 - Missing chapters 142, 159, 462, 530 (My Disciples Are All Big Villains)
- Cause: all four were absent from the project's chapter list (574 of 578). 530 still had a finished work/ + output/recap.mp4 on disk; 142/159/462 had no folders. The project source (mgeko.cc) doesn't have them.
- Re-added the four to `projects/my-disciples-are-all-big-villains-2/project.json` (chapter list now 578, 0..577). 530 reads as complete again (no re-run).
- Downloaded 142 (20 pages), 159 (24), 462 (14) from roliascan.com via `services.downloads._download_chapter` (project URL left on mgeko).
- Started a batch via the app (`POST /api/projects/<slug>/batch`): stitch, script, export, audio, video for 142, 159, 462. Watch it on the project's Batch panel; outputs land in `chapters/<n>/output/recap.mp4`.
- No code changed.

## 2026-11-14 - Merge of ch 1-577 failed: file name too long
- Cause: merged file was named `merged_<stamp>_ch1-2-3-...-577.mp4` (~2,200 chars); Windows caps a file name at 255, so ffmpeg could not create it (all 577 videos were restored and the checks passed).
- Fix: `branding.chapters_label()` writes long lists as ranges (`ch1to141+143to577`; lists up to 100 chars keep the old `1-2-3` form); `branding.chapters_of()` reads both; `services/merging.py` uses it. Old merged files still parse.
- Next: restart the app (tray icon) so it loads the change, then run the merge again - all videos are already on disk, so it goes straight to the stream-copy join.
- Correction: the `services/merging.py` edit above had silently not applied (merge failed again with the long name). Applied it for real; verified a 577-chapter merge now names `merged_<stamp>_ch1to577.mp4` and ffmpeg writes it (~50 MB/s to the Drive folder). Restart the app, then merge again.
- Naming changed to always `ch<lowest>to<highest>` (branding.chapters_label); old names still readable.

## 2026-11-14 - render final "not a readable video file"
- Cause: branding.probe() had a fixed 30s ffprobe timeout; the 15.8GB merge on Google Drive was not locally cached, so the first read timed out and was reported as unreadable. The file itself is fine (h264 1080p + aac, 45.9h).
- Fix: tools/framer/branding.py - probe(path, timeout=30); render_final probes the merge with timeout=600.
- Next: restart the app and run Render final again.

## 2026-11-15 - Drive read retry + lane survives bookkeeping errors
- atomic_io.read_json_with_backup_fallback retries OSError reads (Google Drive Errno 22) 4x.
- tools/framer/batch.py _lane: an exception in _leave no longer kills the lane thread; the chapter is marked failed instead.
- Why: nano-machine script lane died when project.json read failed. Restart the app to load this, then Retry stuck chapters.

## 2026-11-15 - Mount Hua script failures (CUDA OOM)
- tools/framer/ocr.py: GPU model build and read fall back to CPU on any exception (not just RuntimeError, e.g. AcceleratorError) and stay on CPU for the process after a CUDA error.
- Restarted the app (poisoned CUDA context) and retried script for ch 48-58 via the batch retry endpoint.
- ch 89/90 failed at download (kingofshojo.com timeout); not retried.

## AMV Shorts prototype (new `amv/` package)
- Added `amv/music.py` (librosa beat/downbeat/low-band-drop analysis, cached in `cache/amv_music/`, window picker),
  `amv/render.py` (beat-synced 1080x1920 renderer: hook burst, build-up acceleration, drop flash/shake/RGB split,
  zoom-blur cuts, pans on tall/wide panels, grade, RECAPPER top+bottom middle + logo top-left), `amv/__main__.py` (CLI),
  `tests/test_amv.py`. No pipeline stage, manifest or old shorts code touched yet.
- Try: `.venv\Scripts\python.exe -m amv --track "shorts/bg music/xcool.mp3" --frames a.png b.png ... --hero 3 -o out.mp4`
  (`--preview` for a fast half-size check, `--analyze` for beats/drops only). Sample output: `shorts/amv_prototype/xcool_test.mp4`.
- Next: frame-pool tagging in the script pass, planner, plan window, upscaling only when needed, scheduling; then retire the narration shorts.

## AMV prototype: feedback round 1
- `amv/render.py`: one flow transition (zoom-blur + push) on every cut; flash/shake/RGB-split now only on the drop hero (and a light accent on drop downbeats) so the flow is only interrupted at the drop. No more push/pull cycling.
- Small pools: `thin_cuts` merges the relaxed sections (post, then intro) instead of repeating frames; the planner must still supply more frames than cuts (a 30s short has ~34 cuts before thinning).
- Watermark now matches `shorts/watermark placement`: bold sans (Arial Bold), 50% white, no shadow, centred at y=160 and y=1794; logo 172px at (39,78), opaque.
- Test render: `shorts/amv_prototype/xcool_test2.mp4` (35 frames). Tests: `.venv\Scripts\python.exe -m unittest tests.test_amv`.

## AMV prototype: feedback round 2 (camera continuity, music end, Mount Hua test)
- `amv/render.py`: every cut is now one continuous camera move. The outgoing shot accelerates into the cut, the incoming shot starts at the same speed and eases out, and motion blur follows that speed (fast cuts = high speed, slow shots = gentle). Drop hero still the only interruption. `thin_cuts` now merges the shortest neighbouring relaxed cuts (max hold 2.2s) instead of creating multi-second holds.
- `amv/music.py`: analysis also finds `breaks` (kick/808 gaps, cache files now `*_v2.json`); `pick_window(length=None)` ends at the first break after the drop section (+0.9s) so the track is never cut mid-phrase. CLI `--length` is optional now (default: auto). For xcool the clip is 25.4s (16.9s-42.3s of the track).
- New test render with Return of the Mount Hua Sect (chapters 48-69; earlier chapters' panels are no longer on disk): `shorts/amv_prototype/mounthua_xcool.mp4`. Frames were picked by eye, so a few may not be the protagonist; the real tagger will use character names from the script.

## AMV prototype: other tracks
- `amv/music.py` `pick_window`: never starts less than 2s before the drop; auto-length only uses a break inside the max window (else 30s).
- Rendered the Mount Hua frames with all 5 tracks into `shorts/amv_prototype/mounthua_<track>.mp4` (xcool = `mounthua_xcool.mp4`).

## AMV prototype: feedback round 3 (faces, pivots, beat transitions)
- New `amv/tagger.py`: tags frames with the app's `claude` CLI wrapper (sonnet, 16 images/call, cached in `cache/amv_tags/`): has_face, face_box, eye point, character, kind (face/action/scenery/aftermath/object), expression, intensity, text. Production path: this runs when a chapter's script is made (or on demand), the plan then reads the tags.
- New `amv/planner.py` (`plan_aura`): >=90% faces, intensity follows the music (calm intro -> rising build -> strongest on the drop -> cooldown), neighbours avoid the same chapter/expression, non-face frames spaced out, lettering a last resort.
- `amv/render.py`: face shots zoom about the eyes, which stay pinned at a shared screen anchor (50% x, 40% y) and are size-matched (face ~30% of screen height), so the gaze rests while frames change. Frames without a face never zoom (static, or a slide along the long side). `--style push|alt`: alt alternates zoom-in/zoom-out on the beat. Motion blur uses 11 samples and a shorter span.
- Test renders: `shorts/amv_prototype/v3_<track>_<style>.mp4` (Chung Myung aura, chapters 48-68).
- Final v3 renders verified (all 8 decode with 0 errors): `shorts/amv_prototype/v3_{xcool,sub_clair-phonk-hard,sub_clair-drift-phonk}_{push,alt}.mp4`, `v3_mondamusic-brazilian-phonk-phonk_push.mp4`, `v3_sub_clair-brazilian-phonk-instrumental_alt.mp4`. Note: never start two renders writing to the same output file (an earlier overlapping run corrupted them).

## 2026-11-18 - shorts: style flag fix + frame bank (steps 1-2 of the AMV plan)
- `amv/__main__.py`: `--style` was never passed to `render()`, so every `_alt` render equalled its `_push` twin. Now passed.
- `amv/tagger.py`: tags now also carry `visibility` (full/partial/hidden), `solo`, and `mouth`/`hand`/`weapon` points; `TAG_VERSION = 2` makes older cached tags be read again.
- `amv/bank.py` (new): per-chapter frame bank. Tags every exported frame, keeps the best <= 60 (no readable text, strong or with a face) in `work/shorts_bank/frames/`, writes `work/shorts_bank/bank.json` (tags of ALL frames).
- `tools/framer/chapter_backup.py`: new ticked-by-default clearable kind `shorts_frames`; `bank.json` joins the always-local `script` kind. So the big `frames` folder can be cleared and shorts still work.
- `tools/framer/services/shorts_bank.py` (new): `build_chapter` / `build_project` (restores cleared frames from Drive for tagging, skips chapters that already have a bank, backs the bank up). `services/frames.py` `export_chapter` drops the stale bank when frames are re-cut.
- `tests/test_bank.py` (new). Not yet wired: automatic tagging after export / job queue / UI button.
- Run next: `.venv\Scripts\python.exe tools/framer/services/shorts_bank.py <project-slug> [chapters]` to tag existing chapters (uses Claude).

## 2026-11-18 - shorts: auto-tagging job, old-chapter recut, style recipes (aura / story / twist)
- Job queue: new `shorts_tag` job (`services/jobs.py` `new_bank_job`, `queue_bank_tagging`; runner `run_shorts_tag_job` in `services/shorts_jobs.py`; holds the `claude` lock, own `~bank` key so it never blocks a chapter's audio/video). Queued after an editor export (`routes/chapters.py`) and after a batch's export step (`services/batching.py`).
- Routes: `GET/POST /api/projects/<slug>/shorts_bank` (status per chapter / queue tagging for chapters or all, `rebuild`). No UI button yet.
- `services/shorts_bank.py`: chapters with no frames on this PC (usual case: only script, mapping, video are on Drive) are re-cut from `mapping.json` via the Drive PDF (strip stitched, then removed again), tagged, and only the bank is kept. Tested on Mount Hua ch 1: 128 frames tagged, 47 kept, bank backed up to Drive.
- `amv/styles.py` (new): recipes `aura`, `story` (2s+ holds in reading order, then burst on the drop), `twist` (story, twist frame on the last story cut, aura from the drop). `amv/render.py`: `Cut.mode/trans`, story view with drift + dissolve, whip transition, `recipe=`/`items=`. CLI: `python -m amv --track X --recipe story --bank <chapter work folder> -o out.mp4`.
- Samples: `shorts/amv_prototype/v4_{aura,story,twist}_drift.mp4`. Tests: tests/test_amv.py, tests/test_bank.py.
- Next: pivot style (4), quote (6) and lyrics (7) layers, adaptive (5); a UI button for the bank.

## 2026-11-18 - shorts: cuts at the start now follow the audible hits
- Problem: at the start of a clip the cuts sat on the beat tracker's even grid (and its half-beats), which does not match syncopated or kick-less intros; there was also a ~1s hole after the hook, and a 0.25s audio/video fade-in swallowed a hit on the first frame.
- `amv/music.py`: `analyze` also stores `onsets` ([time, strength], 256-sample hop; cache `_v3`, old caches are re-analysed); `hit_near` / `pick_window` start the clip on a strong hit within 0.2s of the downbeat.
- `amv/render.py`: `build_cuts` takes hook / intro / post cuts from the strongest hits (min gaps, holes filled from the beat grid), snaps build/drop cuts to a hit within 50ms, enforces >= 0.14s between cuts; fade-in 0.06s (`FADE_IN`).
- `amv/styles.py`: story changes land on beats, holds stay >= 2s.
- Samples: `shorts/amv_prototype/v5_{aura,story,twist}_drift.mp4`. Tests in tests/test_amv.py.

## 2026-11-18 - shorts: style 4, pivot (one fixed point of focus)
- `amv/styles.py` `pivot`: aura timing over only the frames that show the chosen point (`eye`, `mouth`, `hand`, `weapon`; frames too small to zoom are left out; < 8 usable frames is refused with a message). No whips: the point is pinned to one screen spot (`render.ANCHORS`) and all the motion happens around it.
- `amv/render.py`: `pivot_of(tag, point)`, `load_shot(..., point)`: small panels the renderer letterboxes are pinned too, but only when a point is asked for, so the normal aura look is unchanged. `render(..., point=)`.
- CLI: `python -m amv --track X --recipe pivot --point eye --bank <chapter work folder> -o out.mp4`. Samples: `shorts/amv_prototype/v5_pivot_{eye,weapon}_drift.mp4`. Tests: `AmvPivot` in tests/test_amv.py.

## 2026-11-19 - shorts: plan for styles 5 (adaptive + backdrop), 6 (quote), 7 (lyrics)
- Planning only, no code. Decisions: style 5 = a per-section sequencer over the existing recipes plus a backdrop layer stack; 6/7 share one "title card" system (voice/text on the pre-drop part, protagonist frames after the drop). Backdrops are procedural numpy/cv2 (no new deps); lyrics alignment uses torchaudio's MMS forced aligner (no new torch-pulling package).
- Order: backdrop layer -> quote (6) -> lyrics (7) -> adaptive (5).

## 2026-11-19 - shorts: backdrop layer system (step 1 of styles 5-7)
- Decisions from the user: lyrics stop at the drop (style 7); narration voice is Kokoro `am_michael` (style 6); quotes are the protagonist's own philosophical lines, no author credit shown; post-drop frames are the protagonist matching the quote's persona/mood.
- New `amv/backdrop.py`: `Layer(kind, strength, t0, t1, fade)`, `Backdrop.apply(img, t)`, `parse("mono,rain:0.8@0-12,film@12-")`, `for_emotion()`. Kinds: mono, sepia, film (grain/flicker/scratches/dust/weave/vignette), rain (fogged glass + lens drops), ripple (water displacement), vignette, leak, dust. Pure numpy/OpenCV, no new dependency.
- `amv/render.py`: `render(..., backdrop=)` applies the layers after the flash/build darkening, before the branding. `amv/__main__.py`: `--backdrop "<spec>"`.
- Tests: `AmvBackdrop` in tests/test_amv.py (19 pass). Cost is ~70-170 ms/frame per layer at 1080x1920 (numpy); GPU (moderngl) only if that becomes a problem.
- Sample: `shorts/amv_prototype/v6_backdrop_preview.mp4` (film -> mono+rain -> vignette; Magic Emperor ch 49-53, half-size preview).
- Next: style 6 (quote bank, am_michael narration, text layer ending before the drop, protagonist frames after it).

## 2026-11-19 - shorts: music starts at the track's beginning, 60 fps
- `amv/music.py` `pick_window`: a drop within 35s of the start now means the clip starts at 0.0 (the artist's own intro), not `drop - lead`. A later drop starts on an 8-bar phrase boundary before it. The "main drop" must be >= 6s in (earlier ones are intro hits). New windows: monda 0-30, drift 0-30, phonk-hard 0-30, xcool 0-42.3 (drop 27.0), sub_clair-brazilian 29.6-76.1 (drop 44.3).
- `amv/render.py`: `FPS = 60` (was 30); preview now 30 fps (draws every second frame). All timings are in seconds so looks are unchanged, just smoother.
- Cost: a 30s 60 fps render with film + vignette took ~6 min on this PC (~200 ms/frame). If that is too slow: fewer motion-blur steps, or move the per-frame work to the GPU.
- Sample: `shorts/amv_prototype/v6_aura_60fps.mp4` (1800 frames, 60 fps, drift-phonk from 0:00).
- Follow-up: from-start rule only when the drop is <= 70% into a fixed-length clip (else a phrase boundary), tests all pass.

## 2026-11-19 - shorts: style 6, quote (voice + words + protagonist aura)
- New: `amv/voice.py` (Kokoro `am_michael`, slowed, pitched down, bass/echo/compression in ffmpeg; per-word times; cached in `cache/amv_voice/`), `amv/textlayer.py` (Cormorant Garamond from `assets/fonts/`, words rise out of a blur as spoken, accent colour on key words, line lifts away, picture dimmed under it), `amv/quotes.py` (per-project `quotes.json`: load/upsert/delete, mood -> accent colour, auto emphasis words, `suggest()` via Claude with a `confidence` label), `amv/cast.py` (one text-only Claude call that groups the tagger's inconsistent character labels and returns the protagonist's; cached in `cache/amv_cast/`), `amv/quote_short.py` (voice -> window -> render).
- `amv/styles.py`: recipe `quote` (slow mood-matched protagonist shots under the words, aura montage of his strongest frames from the drop; `Plan` gained `overlay`, `voice`, `backdrop`). `amv/music.py`: `pick_window(fixed_start=)`. `amv/render.py`: `extra=`, draws the text layer after the backdrop, mixes the narration over the unchanged music (stereo, no ducking), recipe backdrop defaults to the quote's mood.
- Timing rule: the last word ends 0.55s before the drop; the clip starts at 0:00 when the voice fits (voice slows/speeds 0.86-1.0 to make that work), else on a 2-bar boundary; an error says when no drop leaves room.
- CLI: `python -m amv --track X --recipe quote --quote "<text>" --mood tense --series "Magic Emperor" --bank projects/<slug>/chapters/*/work -o out.mp4` (or `--quotes projects/<slug>/quotes.json --quote <id>`).
- Suggestions for Magic Emperor saved in `projects/magic-emperor/quotes.json`: ALL are "in-spirit" (Claude cannot recall real dialogue); replace with real lines.
- Sample: `shorts/amv_prototype/v6_quote_preview2.mp4` (half-size preview). Tests: `AmvQuote` in tests/test_amv.py (23 pass).
- Not done yet: UI for the quote bank, frame bank of other projects uses same flow; style 7 reuses textlayer/voice-less path.

## 2026-11-19 - shorts: narration was inaudible under the music (quote style)
- Cause: the voice wav was fine (am_michael is in D:\hf_cache) but averaged ~-24 dB against music at ~-11 dB (peaks 0 dB), and the mix could clip.
- `amv/voice.py`: narration loudness-normalised (`loudnorm=I=-10:TP=-1`), `VERSION = 3` (old cached voices are regenerated).
- `amv/render.py`: `voice_mix_filter()` - the music dips `DUCK_DB` (9 dB, 0 = off) smoothly under the voice, stereo mix, `alimiter` on the sum. `Plan.voice` now carries how long the voice is heard.
- Measured: voice ~-14 dB vs ducked music ~-19 dB (voice ~5 dB above on average); after the drop the music is back at full level (~-5.6 dB mean). Sample: `shorts/amv_prototype/v6_quote_preview3.mp4`.

## 2026-11-19 - shorts: narration really inaudible = loudnorm sample-rate bug (fixed)
- Root cause of "no narration": `loudnorm` (added in `amv/voice.py` `POST`) outputs 192 kHz; the result was read back as 24 kHz, so each voice was ~8x too long and ~3 octaves too low (a 9s quote became 72-79s of rumble). Earlier "louder" fix only measured volume and missed it.
- Fix: `aresample={rate}` after the post chain, `VERSION = 4`, and `narrate()` raises if post-processing changes the length by more than the echo tail.
- Verified on the finished video (not just levels): cross-correlation finds the narration wav in `shorts/amv_prototype/v6_quote_preview4.mp4` at 0.91s, corr 0.66 (music alone 0.03); voice ~5 dB above the dipped music. v6_quote_preview, 2 and 3 have the broken voice: ignore them.

## 2026-11-19 - shorts: quote style - hero hold, music dip, new font, left text
- `amv/render.py`: `DUCK_DB = 18` (music dips 18 dB under the voice, back before the drop); new cut mode `hold` + `_hold_view()` (slow visible push + sway; a letterboxed wide panel gets an animated ambient plate instead of static blurred bars); `Shot.fit_geo`/`plate`.
- `amv/styles.py` `quote`: ONE powerful frame of the speaker (mood match, intensity, face size) is held through the narration + word exit; the beat-cut main edit starts after it (drop = hero); the hold frame is not reused. Adds aurora/embers/mist layers over the hold in the quote's accent colour.
- `amv/backdrop.py`: `Layer.color`; new kinds `embers`, `mist`, `aurora`. (`mist` wraps by tiling, OpenCV blur has no wrap border.)
- `amv/textlayer.py`: sans fonts in `assets/fonts` (Montserrat, Oswald, Anton, Poppins ExtraBold, Archivo Black, Barlow Condensed, Bebas Neue; Cormorant kept), default `anton`, per-quote `font` field (`quotes.normalise`), left-aligned block (MARGIN_X 72, MAX_WIDTH 800).
- Verified on `shorts/amv_prototype/v8_quote_preview.mp4`: narration waveform found at 0.91s (corr 0.86); voice window mean -15.8 dB, after the drop -5.2 dB. 23 tests pass.

## 2026-11-19 - shorts: narration louder + dip before the first word; style 7 (lyrics)
- `amv/render.py` `voice_mix_filter`: the music dip now finishes 0.55s BEFORE the first word (it used to start with the voice, so the first words fought the full-level music); voice gain x1.6 (limiter on the mix keeps it clean). Re-render a quote to hear it (old previews are unchanged).
- Style 7 `lyrics`: new `amv/lyrics_short.py` (`parse_lrc`, `clip_words`, `make`), recipe `lyrics` in `amv/styles.py` (shares `quote()`: hero hold + living background + bold left text, but words come from timed lyrics, no narration, music untouched; lyrics stop 0.55s before the drop and nothing is shown after it), `textlayer.pages_from_words(by_sentence=True)` = one page per lyric line.
- Input is LRC (`[mm:ss.xx] line`, times in the track). NOT done: automatic timing of plain lyrics (torchaudio MMS forced aligner) - plain text without time stamps is refused.
- CLI: `python -m amv --track X --recipe lyrics --lyrics song.lrc --mood tense --series "<name>" --bank projects/<slug>/chapters/*/work -o out.mp4` (`--font anton|montserrat|...`). Demo: `shorts/amv_prototype/demo_lyrics.lrc` (placeholder lines, the tracks here are instrumentals) -> `v9_lyrics_preview.mp4`.
- Tests: `AmvLyrics` (26 in tests/test_amv.py pass).

## 2026-11-19 - handoff file for the next session
- Wrote `shorts/amv_prototype/HANDOFF.md` (requirements per style, decisions, what is built/not, commands, state, traps) and a pointer memory `amv-shorts-handoff` in the Claude memory folder (indexed in MEMORY.md).

## Style 5 `adaptive` (2026-11-19)
- `amv/styles.py`: added `sections()` (music -> story/aura/pivot scenes: quiet pre-drop = story, a break after the drop = story,
  a long drop gets a pivot scene), `adaptive()` (joins per-section cuts/frames into one Plan, mood backdrop per scene from the
  frames' expressions via `backdrop.for_emotion`, `extra["mood"]` forces one), registered in `STYLES`/`build`.
  (styles.py was accidentally emptied by a bad patch and rewritten from the earlier read; section comments now use `--`.)
- `amv/render.py`: a pool frame may carry `"point"`; `load_shot` and the pin anchor use it per shot (pivot inside a mixed short).
- Run: `.venv\Scripts\python.exe -m amv --track "shorts/bg music/sub_clair-phonk-hard.mp3" --recipe adaptive --bank <chapter work dirs> --preview -o shorts/amv_prototype/v10_adaptive_preview.mp4`
- Not done: optional Claude review of the section plan; magic-emperor frame images are no longer on disk (bank.json only), test used return-of-the-mount-hua-sect ch 1.

## Handoff updated for the daily-shorts goal (2026-11-19)
- `shorts/amv_prototype/HANDOFF.md`: added a quick map (what to edit where), style 5 marked done, and a "THE DAILY GOAL" section:
  10+ shorts/day over projects, styles 1-5 + quote (lyrics parked), no approval step (straight to scheduled), playlist link in
  description + first comment; lists the existing publishing pieces to reuse and the 4 missing build steps in order.
- No code changed. Next: step 1 (AMV as the shorts renderer job).

## Step 1 of the daily shorts: AMV renderer stage (2026-11-19)
- New `stages/s8_amv.py` (AMV shorts from a chapter's shorts bank; appends to `output/<ch>/shorts/shorts.json`), `services/jobs.py`
  (`Job.amv`, `_amv_command`: a `shorts_render` job with `amv` set runs it), 3 tests in `tests/test_amv.py` (29 pass).
- Verified by running it on mount-hua ch 1 (2 shorts, previews). HANDOFF.md updated (step 1 done, what step 2/3 need).
- Run: `.venv\Scripts\python.exe -m stages.s8_amv --chapter <id> --count 2 --styles aura,adaptive,quote --series "<name>" --quotes projects/<slug>/quotes.json`
  (set MANHWA_WORK_ROOT / MANHWA_OUTPUT_ROOT to the project chapter folders, as the framer's job does).

## AMV shorts wired into the Shorts button + cleanup + thumbnails (2026-11-20)
- The framer's Shorts pipeline now makes AMV shorts: `services/shorts_jobs.py` `run_amv_find_job` (replaces the Claude moment search as the
  `shorts_find` step: builds/restores each chapter's frame bank, cutting frames again from mapping + stored PDF when they are gone, and deals
  `max_shorts` shorts over the chapters, 1 each by default), `services/batching.py` (`shorts_env.render` sets `job.amv`; `shorts_blocker` now
  needs a bank or mapping+PDF instead of a finished video; `amv_offset` rotates style/track from the number of queued shorts), overview labels.
  Old narration code (`run_shorts_find_job`, `s8_shorts.py`) is left in place, unused.
- Thumbnails: new `amv/thumb.py` (one Claude vision call per chapter ranks clean female faces, cut 9:16 1080x1920, margins trimmed, cached in
  `shorts_bank/thumbs.json`); `stages/s8_amv.py` writes `short_NN.jpg` and `thumbnail` in shorts.json; `publishing/enqueue.py` queues it;
  `publishing/thumbnails.py` keeps portrait covers 720x1280.
- Old shorts removed: 27 unsent/failed shorts out of the upload queue (16 live ones kept as history), `output/<ch>/shorts` + plans in 120 local
  chapters and 118 Drive copies, every `shorts_batch.json` and `shorts_find/`.
- Frame banks being built for chapters 1-6 of the-world-s-best-engineer-2, nano-machine, return-of-the-mount-hua-sect (logs/bank_*.log).
- RESTART the app to load the new code. Songs: drop .mp3 files into `shorts/bg music/`.
- Still open: set each project's `settings.youtube.shorts_playlist` link (description + first comment use it), ~6 uploads/day quota.

## Shorts page: style + music panel (2026-11-20)
- `short_music.py`: `study()` (amv/music analysis -> bpm, main drop >= 6s) runs on upload, stored on the track (`bpm`, `studied`, `warning` when no drop); `files_of()`; theme defaults to "phonk".
- AMV shorts take a project's own tracks (`settings.shorts.library`) when it has any, else `shorts/bg music`: `services/batching.py` (`amv_tracks`, run meta `find.styles`/`find.music`), `services/jobs.py` + `stages/s8_amv.py` (`--track-files a|b`), `routes/shorts.py` (body `styles`, `music`).
- Shorts page (`index.html`, `static/js/app.js`, `app.css`): "Style and music" panel: style picker (Auto mix = rotate, or one of aura/story/twist/pivot/adaptive/quote), music picker (project music / shared / one track), upload (studied on upload), play, delete.
- Style choice before: rotation `(offset + i) % styles` over aura, adaptive, story, twist, quote, pivot, offset = shorts already queued. Restart the app to load.

## Shorts page: multi-select music + ratings (2026-11-20)
- Music: `GET /api/projects/<slug>/shorts/music` lists the project's own tracks (`p:<id>`) and the shared folder's (`s:<file>`); the Shorts page shows them all as a tick list (Select all / Clear, play, delete own). Ticked tracks are the only ones used (`start_shorts(music=[ids])` -> `batching.amv_tracks` -> `--track-files`); nothing ticked = project's own tracks if any, else the shared folder.
- Ratings: new `tools/framer/short_feedback.py` (`projects/<slug>/short_feedback.json`: rating 1-5, tags, note, style, track), `POST /api/projects/<slug>/shorts/<n>/<id>/rating`, stars + tag chips + note on every short card (form opens for <= 3 stars), deleting a short drops its rating. The Auto mix leaves out a style or (default) track rated at least twice with an average under 2.5 stars (`batching.amv_styles/amv_tracks`); hand-picked style/music is never filtered. Notes/tags are not applied automatically: read `short_feedback.json` and change the renderer from them.
- AMV cards now show style + track (not "lines undefined"). Restart the app + Ctrl+F5.
- NOTE: the engineer project already has its own uploaded track (assets/shorts_music/*.aac), so without ticking anything its shorts use only that one.

## 2026-11-21 - Shorts thumbnails now uploaded
- tools/framer/publishing/worker.py: `_finish` set a thumbnail only for FULL videos; shorts got none. Now shorts call `_thumbnail` too (vertical JPEG from shorts.json `thumbnail`, queued by enqueue.py).
- Shorts already live (before chapter 1 of the-world-s-best-engineer-2) have no thumbnail in the queue; ch1-3 have a jpg but were uploaded before the fix and need a backfill.

## 2026-11-21 - Queue reorder, project names, music preview
- Publishing page: upload-queue cards can be dragged or moved with ▲/▼ (POST /api/publishing/queue/order; uploads.reorder sets `rank`, redeals scheduled shorts' slots per channel; worker._order uploads by rank first). Cards show the project name (api_state adds project_name).
- Shorts page: every music track, incl. shared-folder ones, now has a player (GET /api/shorts/shared_music/<file>); one plays at a time.
- Files: tools/framer/{static/publishing.html,static/js/app.js,routes/publishing.py,routes/shorts.py,publishing/uploads.py,publishing/worker.py}. Restart the app to load them.

## 2026-11-21 - Shorts scheduled on YouTube (laptop can be off at post time)
- worker.py: a short whose slot is within 7 days (AHEAD) is uploaded now as private with `publishAt` = its slot; YouTube publishes it. Item ends 'private' with `scheduled_at`. sync.refresh_visibility marks it live when public; if its slot passed (+15 min) and it is still private it is flagged locked (API restriction) so lift_lock handles it. uploads.reorder also deals slots of queued-ahead shorts. UI shows 'scheduled on YouTube'.
- Restart the app. Uploads still need the app running (quota ~6 uploads/day), publishing does not.


## 2026-11-23 - CTA script + GPT image prompts for Return of the Mount Hua Sect
- No files changed. Modelled on the chapter-500 CTA of my-disciples-are-all-big-villains-2 (6 lines, calm pace, one frame per line). Wrote a 6-part warm/humble CTA script, a GPT master prompt and 6 per-part image prompts (delivered in chat only).
- Next: generate 6 frames in GPT, save as cta/frames/scene 1..6.png in the new project, build the CTA from the framer.


## 2026-11-23 - Kokoro says "red" for "read"
- stages/tts_kokoro.py: new fix_pronunciations() called in synthesize(); forces present-tense "read" to /ɹˈid/ via misaki inline markup, leaving it alone after have/had/was/already etc. Covers narration, CTA and AMV voice (all use synthesize).
- tests/test_kokoro_pronunciation.py added. Re-render the CTA voice to hear it.


## 2026-11-23 - CTA video for return-of-the-mount-hua-sect
- Copied cta_script/project 2/frames/ (6 pngs) to G:/My Drive/RECAPPER/bebe/return-of-the-mount-hua-sect/cta/frames/ as "scene 1..6.png"; backed up old cta/script.txt as script.old.txt and wrote the new 6-line script.
- Built with cta.build(): Kokoro am_adam voice, project look (watermark + background), 1920x1080, 28.6s, 3.3 MB -> cta/cta.mp4 in the project folder.
- Re-run from the app (CTA button) or edit cta/script.txt then rebuild.


## 2026-11-23 - All shorts are style 6 (quote), cinematic quote look, louder voice / quieter music
- **Policy**: `AMV_STYLES = ("quote",)` in tools/framer/services/batching.py (`amv_styles()` ignores ratings); `s8_amv.DEFAULT_STYLES = ("quote",)`; the Shorts page style list (static/js/app.js `SHORT_STYLES`) offers only Quote. A project with no quotes.json now gets one automatically (`s8_amv.make_quotes`: Claude writes 10 lines for the protagonist; `cast.protagonist_name` gives the name; lines marked `[in-spirit]`/`[close]` in `note` are NOT verbatim: review them on the Shorts page).
- **Settings**: `settings.shorts.auto_make` is now True by default (projects.py) and was switched on in all 12 existing projects (project.json, via projects.save). Old shorts deleted from projects/: every `output/<ch>/shorts` and `work/<ch>/shorts` folder, plus the 2 `shorts_batch.json`. Frame banks (`work/shorts_bank`), music and quotes were kept. Drive copies only held desktop.ini. publishing/queue.json history (32 shorts, all `live`) untouched. Ratings in short_feedback.json kept.
- **Look** (amv/cinema.py new, backdrop.py, render.py, styles.py): text is now `CinemaText` - lower-third, local soft shade, light sweep through each word, key words near-white with an accent glow that breathes with the voice + a self-drawing underline, block drifts/grows with the camera push, one page per sentence. New backdrop layers `bokeh`, `rays`, `frame` (hairline border drawing itself + running light), `grain`; `Plan.post` layers run after the text so grain binds everything; `Plan.env` = voice loudness drives aurora/bokeh/rays. Quote frames are `cover_only` (never letterboxed; `render.load_shot(cover_only=True)`, Lanczos + `_ink` sharpening on frames magnified >2.2x; hero frame prefers frames that fill the screen sharply).
- **Audio**: `MUSIC_DB = -9` on all AMV music, `DUCK_DB` 18 -> 26, `VOICE_GAIN` 1.6 -> 2.0, limiter 0.9 (render.py).
- Tests: tests/test_amv.py `CinemaQuote` (4 new). Previews: shorts/amv_prototype/v11-v13_cinema_preview.mp4 (Mount Hua ch1).
- Note: running the tests earlier reverted cache/durations.json with `git checkout` (it was a modified cache file; regenerates).
- Run next: restart the framer app, then Shorts page -> make shorts; or `python -m stages.s8_amv --chapter <id> --count 1 --quotes projects/<slug>/quotes.json`.


## 2026-11-23 - Quote shorts: softer voice, no frozen frame, shorter, six project looks, closing message
- **Voice**: amv/voice.py echo is now one faint 26 ms tap (was two taps at 70/140 ms = "bathroom"); VERSION 5 invalidates the cache. `narrate(plain=True)` = Kokoro as it speaks (no pitch/bass/echo), `PLAIN_VOICE = am_adam`.
- **Repeated frame**: styles.quote widens the pool (other sharp frames) when the speaker has too few, thins the cuts to the pool (`thin_cuts`) and cycles it - no more freezing on the last frame. Short ends `TAIL_S` (8 s) after the drop, min 18 s (`quote_short.py`), not the whole track.
- **Looks** (amv/looks.py new): ember, scroll, royal, neon, blood, frost = font, text position, entrance (rise/slam/track), rule line, accent, overlay layers, border. New backdrop layers: snow, petals, scan, brackets, bars. `looks.resolve(setting, slug)`: project setting `settings.shorts.look` (auto default; Project settings -> Shorts -> Quote design), else `looks.BY_PROJECT`, else a stable pick from the slug. Passed to s8_amv with `--look` (services/jobs.py `_amv_command`). CLI: `python -m amv ... --look scroll`.
- **Closing message** (amv/quote_short.py `outro_text`, styles.quote, cinema.Stack): after the edit, a scene with its own frame, text and an am_adam voice: "Thank you for watching. The playlist link for <series> is in our description and our pinned comment, and we upload daily." `--no-outro` to skip. Mixed with `voice_mix_filter(..., more=)` (music dips under both voices). Limiter 0.85.
- **Playlist links** (`settings.youtube.shorts_playlist`): only my-disciples-are-all-big-villains-2 and the-world-s-best-engineer-2 have one, and both look truncated (11-12 chars after list=). The worker (publishing/worker.py) adds the link to the description and posts it as a comment, but the API cannot PIN a comment.
- Tests: tests/test_amv.py (QuoteLooks, QuoteOutro; 39 pass). Previews: shorts/amv_prototype/looks/look_<look>.mp4. Lesson: never start 6 renders at once (they starved each other for 33 min); max 3 at a time.


## 2026-11-23 - Random terminal windows while shorts render
- Cause: amv/render.py (ffmpeg render pipe) and amv/voice.py (ffmpeg voice post-processing) started ffmpeg without `creationflags=CREATE_NO_WINDOW`; the app runs under pythonw, so Windows opened a console for each. Fix: `amv.NO_CONSOLE` (amv/__init__.py) used in both. Other subprocess calls were already fine (config.NO_CONSOLE) or GUI launches (restart, splash, startfile).
- Takes effect for the next shorts render (each job is a new Python process); the render running when this was found keeps the old code.

## 2026-11-25 - app stuck on loading
- Cause: cold /api/projects took ~10s (1200 chapter.json reads at ~5ms each); cache expiry made each refresh block.
- tools/framer/projects.py: _stored_states now serves stale cache and refreshes in a background thread. tools/framer/app.py: warms the cache at startup.
- Restart the app (tray icon > quit, then start.bat) to pick it up.

## 2026-11-25 - shared CTA clip
- New tools/common_cta.py builds assets/cta/cta.mp4 (32.9s, 1080p, manga-style animation: logo, read/script/voice/edit panels, daily calendar stamps, like + subscribe + bell, comment bubbles; no narration text; synthesized calm SFX). Voice = the 6 project-name-free lines from my-disciples-are-all-big-villains-2, copied to assets/cta/audio/.
- tools/framer/cta.py: clip_path() falls back to assets/cta/cta.mp4 when a project has no cta/cta.mp4, so merge "CTA after chapter N" works for every project.
- Rebuild: .venv\Scripts\python.exe tools/common_cta.py (--preview, --still <sec>). Takes ~10 min on this RAM-tight PC.

## 2026-11-25 - shared CTA v2
- tools/common_cta.py: logo intro cut to 2.7s + new frame (pause->play button, panels with characters); line 3 is now a real 30-day wall calendar with a random chibi character stamped on each day (accelerating); characters also peek in during like/subscribe, comments and the closing logo.
- Characters = PNGs in assets/cta/characters/ (any size; transparent PNGs used as drawn, others become rounded badges). Currently 4 crops of the chibi disciples as placeholders: add chibis of other projects there and re-run tools/common_cta.py.
- Voice: line 2 re-voiced with "reed" (Kokoro am_adam, the villains project voice) -> assets/cta/audio/cta_02.wav (old kept as cta_02_old_read.wav.bak).

## 2026-11-25 - shared CTA v3
- Opening is a Marvel-style wall of fast-scrolling black-and-white manga panels (assets/cta/panels/*.jpg, 54 panels sampled from 6 series; columns scroll in opposite directions, page-flip riffle SFX, logo slams in at 0.7s, wall dims behind the second frame).
- Characters now come from assets/cta/new characters/ch*.png (white sticker outline added): Luffy/Gojo/Goku burst from the intro panels; ch1 + ch12 in the makers scene; Luffy + Goku frame the calendar (random stickers per day; ch6 excluded); ch4 thumbs-up + ch11 pointing at SUBSCRIBE; ch12 thinking + ch5 OK on comments; ch2 presents the logo, ch8 waves, ch13 duo, ch6 applauds at the end. Old placeholder crops deleted.
- Rebuild: .venv\Scripts\python.exe tools/common_cta.py

## 2026-11-25 - CTA line 5 voice
- Line 5 re-voiced with "reed" (assets/cta/audio/cta_05.wav, old kept as cta_05_old_read.wav.bak) and assets/cta/cta.mp4 re-rendered.

## 2026-11-25 - shared CTA final
- assets/cta/cta.mp4 (+cta.json, audio/, panels/, new characters/) is the final shared CTA. tools/framer/cta.py clip_path() falls back to it for any project without its own cta/cta.mp4, so the merge dropdown "Call-to-action video after chapter" works in every project (and on a friend's PC after update).
- To ship: git add assets/cta tools/common_cta.py tools/framer/cta.py (assets/** is not git-ignored). A project's own cta/cta.mp4 still wins; delete it to use the shared one.

## 2026-11-25 - faster intro/outro final render
- tools/framer/branding.py render_final: no more full-size ".faded" copy of the merge. Only the first keyframe-aligned seconds (>= 2s fade) are re-encoded into a small local head clip (-frames:v exact); the join then stream-copies head + merge (concat inpoint) + intro/outro in ONE read/write of the big file. Falls back to the old way when no keyframe is found. Tested on a local 375s chapter: identical frame count/duration, no decode errors, ~5x faster (bigger gain on Google Drive). Applies after restarting the app.

## 2026-11-25 - invisible waiting job
- A final (brand) render popped by the single job worker waits on the shared "video" hardware lock while a shorts render holds it; it was neither running nor in QUEUE_ORDER, so /api/jobs hid it and the button said "already queued or running". services/jobs.py jobs_with_eta now also lists such waiting jobs (needs an app restart to take effect).

## 2026-11-25 - nano-machine shorts: unique quotes/music + queued
- Cause of repeated quote/track: services/batching.py amv_offset counted only shorts already in the upload queue, so shorts rendered but not queued all started from the same rotation spot (offset 33 -> same quote #3 and track). Now it also counts this project's rendered-but-unqueued shorts (reads chapters/*/output/shorts/shorts.json). Needs app restart.
- Re-rendered ch138 short with offset 34 (quote "The higher you climb...", track xcool); old render kept in chapters/138/output/shorts_same_quote_backup/. ch136 keeps "Those who stand beside me..." + sub_clair-phonk-hard.
- Queued both for upload via publishing.enqueue.shorts_of_chapter (slots 12:00 and 13:24 UTC). ch139 has a partial short_01.mp4 from the cancelled render (no shorts.json): safe to delete.
