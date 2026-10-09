# Work log - merged videos: intro/outro polish, chapter lengths

- Branded finals now have a 1s black/silent gap after the intro and before the outro, and the
  main video's audio fades in from 15% to 100% over its first 2s (picture still stream-copied;
  only the audio track is re-encoded). Constants at the top of `tools/framer/branding.py`.
- Chapters list shows each complete chapter's video length; the merge bar shows the combined
  length of the selected chapters (`/api/projects/<slug>` now returns `duration` per chapter).
- Earlier work (Merged videos view, Media library, branding job, status-from-disk) is unchanged:
  see CLAUDE.md. New files: `tools/framer/branding.py`, `tools/framer/library.py`, the two clips.

Next: restart the app, then `git add recapper_intro.mp4 recapper_outro.mp4 tools/framer` and commit.
