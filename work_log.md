# Work log - merged videos in the project folder

- `projects.merged_dir()` now returns `<project folder>/merged` (the Drive/shared folder for projects that live there) instead of always the PC's local `projects/<slug>/merged`. Merges and branded finals, the Merged videos screen, the library and the video route all go through it.
- Old merges kept on the local disk are moved into the new folder the first time the folder is used (only matters for projects stored elsewhere than `projects/`).
- Chapter renders (`recap.mp4`), work files and the branding clip cache stay on this PC.
- Restart the app to pick it up.
