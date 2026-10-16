# Work log

Made the settings from project `100-year-old-top-chef` the defaults for all projects.

- `defaults/settings.json` re-saved from that project (via `project_defaults.save_from`). Changes: voice engine
  `chatterbox`, tone `comedic`, zoom 1.06 (replaces ken_burns), `framing.blur_text` false, no `target_minutes`.
- Applied the defaults to the existing projects `test` and `the-stellar-swordmaster` (`project_defaults.apply`).
  Series briefs and custom files they already had were kept. New projects pick the defaults up automatically.
