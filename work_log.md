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
