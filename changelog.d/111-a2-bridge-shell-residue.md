Security hardening (#111, slice A2):

- **Settings are checked when the app starts.** A value in `settings.json` that the app would not accept from Settings (for example an unknown speech model) is replaced by its default.
- **The window refuses to open unsafely.** The app now stops with a message if Windows would fall back to the old Internet Explorer engine instead of WebView2, or if its window protections cannot be put in place.
- **Tighter limits on what the page can ask for.** The page can show only the app's own screen, drag and drop accepts only files Windows reports as dropped, and calls with the wrong arguments are refused before they run.
- **Less left behind.** The window's browser data is kept inside the workspace folder and cleared at the next start if the app did not close cleanly, and SQLite keeps its temporary data in memory rather than in the Windows temp folder.
- **The handoff file name no longer contains the meeting title.** Save suggests `Meeting transcript YYYY-MM-DD HH-MM.md` (when the meeting was imported), so client names in titles do not end up in file names by default.
- **A Hugging Face token left in your Windows environment is no longer passed on** to the app's other processes.
- **The app refuses a missing, synced or network app data folder** instead of falling back to the current folder.
- **Only one copy runs at a time.** Opening Unminute again while it is open shows a message instead of starting a second copy on the same workspace.
