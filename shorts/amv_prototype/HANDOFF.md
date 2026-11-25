# AMV Shorts - handoff for the next session (written 2026-11-19, updated after style 5 + the daily-goal decisions)

**Quick map for "I want to change X in the shorts":** look/timing of a style -> `amv/styles.py` + `amv/render.py`; music analysis /
window -> `amv/music.py`; mood layers -> `amv/backdrop.py`; quote text/voice/font -> `amv/quotes.py`, `voice.py`, `textlayer.py`;
frames/tags -> `amv/bank.py`, `tagger.py`, `cast.py`; app + YouTube side -> see "THE DAILY GOAL" below. Tests: `tests/test_amv.py`.
Add a work_log.md entry after every task.

Read this first. It says what the user wants, what is built, what is not, and the traps. The code is in `amv/` (project root
`D:\Work\manhwa-recap`); the running history is in `work_log.md` (append-only: add a new entry after every task, never rewrite).
Project rules are in the root `CLAUDE.md` (stages connected only by files; Windows; torch must stay the +cu128 build).

## The goal
Vertical 1080x1920 Shorts for manhwa recap channels, **replacing the old narration shorts** (`stages/s8_shorts.py`, not touched yet).
They are beat-synced AMV-style edits over phonk tracks (`shorts/bg music/*.mp3`) made from the chapters' exported frames, for ANY
project, on demand. Branding: RECAPPER top + bottom middle, logo top-left (already in the renderer).
Prototype outputs go in THIS folder (`shorts/amv_prototype/`). Nothing is wired into the app yet (CLI only).

## The 7 styles the user defined
1. `aura` - character faces, intensity follows the music, push + zoom-blur, flash/shake only on the drop.  DONE
2. `story` - slow cuts (>=2s) in reading order, burst on the drop.  DONE
3. `twist` - story, twist frame before the drop, aura from the drop.  DONE
4. `pivot` - aura around ONE fixed point (eye|mouth|hand|weapon) pinned to one screen spot.  DONE
5. `adaptive` - DONE (2026-11-19, `styles.adaptive` / `styles.sections`): reads the music's sections (quiet pre-drop = story,
   loud = aura; a break after the drop = story; a long drop gets a 5s pivot scene; one break max) and joins per-section cuts/frames
   into one Plan; a mood backdrop per scene from the frames' expressions (`backdrop.for_emotion`, `extra["mood"]` forces one).
   A pool frame may carry `"point"` so the renderer pins per shot (`render.render`: `anchors`). Preview `v10_adaptive_preview.mp4`
   was accepted by the user ("alright for now"). NOT done: optional cheap Claude text review of the section plan.
6. `quote` - DONE (see below).
7. `lyrics` - DONE for timed LRC only (see below).
Also wanted for "on demand for different projects": a Shorts page in the framer (style dropdown, track picker, quote/lyrics editor,
backdrop picker with preview) and a job in the existing queue (`services/jobs.py` `new_bank_job`, `services/shorts_jobs.py`) that
runs `python -m amv ...` as a subprocess; output to `output/<ch>/shorts/`. NOT STARTED.

## THE DAILY GOAL (decided 2026-11-19) - read before touching the app side
The user wants **at least 10 shorts per day, of different types, from different projects, all scheduled to YouTube** with a proper
title, a description that contains the **project's playlist link**, and the **same playlist link in the first comment**.
Decisions (do not re-ask):
- **Types in the daily mix: styles 1-5 (aura, story, twist, pivot, adaptive) AND 6 `quote`.** Lyrics (7) are LEFT OUT for now (keep
  the code; do not wire it). Quote shorts need a quote per project: take it from `projects/<slug>/quotes.json` (`quotes.py`); if a
  project has none, `quotes.suggest()` (Claude) can make some, but they are only "in the spirit of" the character: tell the user
  which projects still need real lines; never block the daily run on that (skip quote for that project).
- **NO approval step for shorts**: they go straight to `status: "scheduled"` in the next free slot (this is what
  `publishing/enqueue.shorts_of_chapter` already does; full videos keep their Telegram approval, do not change that).
- Unattended run: rotate styles, tracks (`shorts/bg music/*.mp3`) and projects (round-robin, neediest first); render overnight.

### What already exists (reuse, do not rebuild)
- `tools/framer/publishing/` uploads to YouTube: `youtube_api.py` has `playlist_id(title)` (find/create a playlist by title),
  `add_to_playlist`, `comment(video_id, text)`, `upload`; `worker.py` runs the queue; `slots.py` picks the next free schedule slot;
  `uploads.py` is the queue store; `quota.py` tracks quota; `metadata.py` `write()` asks Claude for title/description/tags FROM THE
  SHORT'S NARRATION (`item["script"]`), `default_tags(series)`.
- `publishing/enqueue.py` `shorts_of_chapter(slug, n)`: reads `output/<ch>/shorts/shorts.json` via `shorts.rendered(output)` (entries
  need `file`, `title`, `description`, `duration_s`, optional `script`) and queues each as a SHORT in the next slot. Item has
  `"playlist"`/`series` fields; check `worker.py` for how playlist + first comment are written for FULL videos and make shorts
  do the same (playlist URL = `https://www.youtube.com/playlist?list=<id>` from `playlist_id(series)`).
- Shorts batch: `batch.SHORTS_STAGES`, `shorts_batch.json`, `routes/shorts.py`, `services/shorts_jobs.py`, `shorts.py`,
  `stages/s8_shorts.py` (OLD narration shorts: to be replaced by AMV; retire last). `services/planning.py` / `services/advisor.py`
  compute per-project plans and suggest what to run; the Telegram bot (`publishing/telegram.py`, `services/cards.py`) is the remote.
- Per-chapter frame bank: `tools/framer/services/shorts_bank.py <slug> [chapters]` (Claude tags frames; the bank frames folder is
  git-ignored/maybe missing: `projects/magic-emperor` currently has only `bank.json`, its images are gone, so rebuild before use;
  `projects/return-of-the-mount-hua-sect/chapters/1/work` has a full bank and was used for the adaptive test).

### Step 1 DONE (2026-11-19): AMV as the shorts renderer
- `stages/s8_amv.py` (`python -m stages.s8_amv --chapter <id> --count N --styles a,b --series NAME --quotes quotes.json --offset K`):
  renders N AMV shorts from the chapter's shorts bank, rotating style/track/quote by `--offset`, APPENDS to
  `output/<ch>/shorts/shorts.json` (same format the upload queue reads; extra keys `style`, `track`), prints `shorts i/n:` and
  `render NN%` lines for the job tracker. `quote` is skipped when the project has no quotes.json. Fallback title/description are
  plain (step 2 rewrites them). Tested on mount-hua ch 1 (aura + adaptive previews).
- `services/jobs.py`: `Job.amv` (None = old narration shorts) and `_amv_command`; a `shorts_render` job with `job.amv = {count,
  styles, offset}` runs s8_amv. Nothing sets `job.amv` yet: the planner (step 3) must create
  `new_shorts_job(ch_id, 'shorts_render', slug, n)` and set it. It needs the chapter's bank (the `shorts_tag` job, `new_bank_job`).

### What is STILL MISSING (build in this order; 1 is done)
1. **AMV as the shorts renderer**: a job (like `shorts_render` in `services/shorts_jobs.py`/batch) that runs
   `python -m amv --recipe <style> --track <t> --bank <chapter work dirs> -o output/<ch>/shorts/short_NN.mp4` (quote: `--recipe quote
   --quote <id> --quotes projects/<slug>/quotes.json`, which calls `quote_short.make`) as a subprocess, then writes
   `output/<ch>/shorts/shorts.json` in the existing format. No manifest/orchestrator change (CLAUDE.md architecture rule). amv is
   not in `STAGE_ORDER`.
2. **Metadata for AMV shorts**: they have no narration script. Feed `metadata.write` the series name + the chapter's script lines
   (or the quote text for quote shorts); the description must END with the project's playlist link, and the first comment must be
   the same link (extend the SHORT upload path in `publishing/worker.py`).
3. **Daily planner**: N shorts/day (setting, default 10) over projects x styles x tracks, queued into `slots.next_slot`; start from
   `services/planning.py`/`advisor.py` ideas; a Telegram `/shorts` already starts shorts by hand.
4. Framer Shorts page (style, track, quote editor, backdrop preview) for manual use.
Limits to remember: YouTube quota ~1600 units/upload vs 10,000/day default => ~6 uploads/day unless a quota increase is granted
(`quota.py`; unaudited API projects upload private/locked, see CLAUDE.md "publishing"); a full-size 30s 60fps render is ~6+ min, so 10
shorts is ~1-1.5 h: queue overnight (see open item 7, speed).

## User's requirements and decisions (do not re-ask)
- Music: start the clip **from the beginning of the track** whenever the main drop is within ~35s of it (the user disliked clips
  starting mid-track). Later drop -> start on a 2/8-bar phrase boundary. A "drop" before 6s is an intro hit. (`music.pick_window`)
- **60 fps** (`render.FPS = 60`; previews are 30 fps half size).
- Style 6 quote: the quote is a **famous/philosophical line of the protagonist** (understandable without the series), the user can
  edit/add their own; **no author credit on screen**. Voice = Kokoro **`am_michael`** (installed in `D:\hf_cache`), deep.
  Narration plays at the START and **ends just before the drop** (0.55s); music keeps running **but must be low under the voice**
  and the voice must be clearly louder (the user complained twice: now music dips 18 dB, dip complete 0.55s BEFORE the first word,
  voice x1.6, limiter). The words are shown on screen: **bold sans-serif (Anton default), left-aligned block (left margin 72px,
  max width 800), accent colour on key words, words rise out of a blur as spoken, line lifts away at the end**.
  While the quote plays: **ONE powerful frame of the speaker only**, held with a slow push, on a **living background** (moving
  coloured lights, rising embers, mist, animated ambient plate behind letterboxed panels) so it never looks still.
  **After the narration is done the main edit starts** (beat cuts of the protagonist's frames, drop = hero). Frames must match the
  protagonist's persona and the quote's mood.
- Style 7 lyrics: same as 6 but the lyrics of the track instead of a quote and NO narration; **no lyrics after the drop**.
- User preference: it is a one-person tool; they close the laptop abruptly - keep work saved and logged.

## What is built (all in `amv/`)
`music.py` analysis (beats/downbeats/onsets/drops/breaks, cache `cache/amv_music/`), `render.py` (renderer; cut modes aura/story/
burst/hold; `voice_mix_filter`; `DUCK_DB`), `planner.py` (frame order by intensity), `styles.py` (recipes aura/story/twist/pivot/
quote/lyrics; `Plan` has overlay/voice/backdrop), `tagger.py` + `bank.py` (Claude tags every exported frame; per-chapter
`work/shorts_bank/`), `cast.py` (finds the protagonist's tag labels with one cached text call), `backdrop.py` (mono, sepia, film,
rain, ripple, vignette, leak, dust, embers, mist, aurora; `parse("mono,rain:0.8@0-12")`, `for_emotion()`), `voice.py` (Kokoro
narration + word times, cached `cache/amv_voice/`), `textlayer.py` (fonts in `assets/fonts/`, selectable per quote via `font`),
`quotes.py` (per-project `quotes.json`, `suggest()` via Claude), `quote_short.py`, `lyrics_short.py`.
Tests: `tests/test_amv.py`, `tests/test_bank.py` (26 + bank tests pass).

## Commands
```
# quote (style 6); --quotes projects/<slug>/quotes.json --quote <id>  or  --quote "text" --mood tense
.venv\Scripts\python.exe -m amv --track "shorts/bg music/sub_clair-phonk-hard.mp3" --recipe quote --quote "A blade does not hesitate. Only the man who holds it does. And hesitation is the only thing that has ever killed a swordsman." --mood tense --series "Magic Emperor" --bank projects/magic-emperor/chapters/*/work --preview -o shorts/amv_prototype/out.mp4
# lyrics (style 7): LRC file "[mm:ss.xx] line"
.venv\Scripts\python.exe -m amv --track "shorts/bg music/sub_clair-phonk-hard.mp3" --recipe lyrics --lyrics shorts/amv_prototype/demo_lyrics.lrc --mood tense --series "Magic Emperor" --bank projects/magic-emperor/chapters/*/work --preview -o shorts/amv_prototype/v9_lyrics_preview.mp4
# styles 1-4: --recipe aura|story|twist|pivot [--point eye|mouth|hand|weapon] --bank <chapter work folders>
# options: --backdrop "film:0.8@0-12,vignette:0.5"  --font anton|montserrat|oswald|poppins|archivo|barlow|bebas|cormorant  --length N  (omit --preview for full size)
```
Test project with a built bank: `projects/magic-emperor` (protagonist Zhuo Fan; chapters 49-... have `work/shorts_bank`).
Build a bank for another project: `.venv\Scripts\python.exe tools/framer/services/shorts_bank.py <project-slug> [chapters]` (uses Claude).

## State at the end of the session
- Latest previews in this folder: `v8_quote_preview.mp4` (hero frame + living background + Anton text; narration present but made
  BEFORE the last audio fix: re-render to hear the louder voice / dip-before-first-word), `v9_lyrics_preview.mp4` (style 7 demo with placeholder
  lyrics; finished and 30s long, not yet looked at by the user).
  `v6_quote_preview`, `..2`, `..3` have a BROKEN (8x too long) voice: ignore/delete. `demo_lyrics.lrc` has placeholder lines.
- `projects/magic-emperor/quotes.json` holds 6 Claude-written lines, ALL "in-spirit" (Claude cannot recall real dialogue): the user
  must supply the real famous lines (the bank is editable; `quotes.upsert/delete`).
- Not verified by ear/eye: nothing has been WATCHED by the assistant; checks were frames + volume + waveform correlation.

## Open items / ideas, in suggested order
1. Ask the user how the latest quote/lyrics previews look (v8 / v9); re-render a quote to confirm the louder voice.
2. Text sits over the speaker's eyes (CENTER_Y 0.46, `textlayer.py`): offered to move it lower (~0.66); user has not answered.
3. (style 5 done) 4. Lyrics (PARKED by the user for now): automatic timing of PLAIN lyrics (torchaudio `pipelines.MMS_FA` forced aligner; no new
   torch-pulling package; `demucs` only if alignment fails; check `requirements*.txt` + CUDA torch rule in CLAUDE.md). The five
   tracks are instrumentals: lyrics need vocal tracks. 5. The framer UI + job queue; retire the old narration shorts last.
6. Tagger consistency: the tagger names one character many ways (252 labels / 525 frames). `cast.py` papers over it; better to pass
   the protagonist's name/description into the tagger context when a project's bank is built.
7. Speed: a 30s 60fps full-size render takes ~6+ min (backdrop layers 70-170 ms each per frame). Try fewer motion-blur steps / GPU.
8. Pre-drop shots when the hero panel is a wide letterboxed panel are handled by `_hold_view`'s plate; tall panels pan.

## Traps (cost time this session)
- **Shell escaping**: in this environment a backslash-r / backslash-n typed inside a Bash heredoc python snippet can arrive as a real
  CR/LF, which broke an f-string in `amv/__main__.py` three times. Prefer the Write/Edit tools for code, or build bytes with
  `bytes([92])`; always `ast.parse` the file after patching. Files are CRLF: when patching with Python read/write with
  `newline=''`.
- Run renders in the background (`run_in_background`) and wait on a `DONE` marker in the log; do not start two renders to the same
  output file.
- Do not trust loudness stats alone for audio: check the narration waveform is in the mix (cross-correlation script idea in
  `work_log.md`, "narration really inaudible" entry). `loudnorm` outputs 192 kHz: always `aresample` back.
- 5 unrelated tests already fail on `main` (`test_cta`, `test_planner`, `test_publishing`); running tests rewrites
  `cache/durations.json` (git shows it modified; restore with `git checkout -- cache/durations.json` if needed).
- `amv/styles.py` was once emptied by a Python patch that wrote with the default cp1252 encoding: always open with
  `encoding="utf-8", newline=""` (or use Edit/Write) and never write a file before the new text is fully built.
- Nothing in this work is committed (git status shows `amv/`, `assets/fonts/`, tests, previews as untracked/modified). Commit only
  when the user asks.
