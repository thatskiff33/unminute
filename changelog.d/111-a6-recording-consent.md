Security hardening (#111, slice A6):

- **Each recording starts with a confirmation that everyone has been told.** Before recording, Unminute asks you to tick that everyone in the meeting has been told it is being recorded, and why, then select Start recording. The question is drawn by the app outside its page, so nothing in the page can answer it: Start recording becomes available after a moment and answers only a mouse click, and Enter means Cancel. IT can turn it off with the new `RequireRecordingConsent` policy.
- **You can see when the app is recording.** The window title starts with a red-dot "Recording" mark and the app's taskbar button shows a red dot, also when the window is minimised.
- **Discarding a meeting and allowing the app to delete originals use the same protected confirmation**, in place of the page's own dialog. The deletion question names the folder.
