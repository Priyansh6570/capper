# Work log - Projects page redesign (round 2)

- Projects screen: "detailed" layout from the design (2 columns, 128px cover, 4-line description, host · time, running pill, split bar, dashed "New project" tile). `static/css/app.css`, `static/js/app.js` (`renderProjectsList`).
- New project is now a modal dialog (`index.html`, css `.np*`, js `openNewProject` etc.): URL field with live "recognised / not supported" check, supported-source tiles, optional name (taken from the series title if empty - `routes/projects.py` `api_create_project`), inline Change/Save rows for Projects folder and Model cache. Create is enabled only for a supported site.
- Check for updates: sidebar states (checking spinner, up to date, update ready card with Later / Restart now, error card with Dismiss / Try again, packages-to-install note) - js `checkForUpdate`, `setUpd`, `paintUpdate`; the old inline result bar is gone.
- Not done: the design's "Updating... %" progress (the app's update is one git pull with no progress), the cards/rows layout switch, and the design's own theme toggle.
- Checked in the browser on a throwaway server (port 5099, now stopped). Restart the real app and hard-refresh (Ctrl+F5).
