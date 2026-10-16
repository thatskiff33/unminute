Security hardening (#111, slice A6):

- **Each recording starts with a Windows confirmation.** Before recording, Windows asks whether everyone in the meeting has been told it is being recorded, and why; the recording starts only after Yes. The question is outside the app's page, so nothing in the page can answer it: Yes becomes available after a moment and answers only a mouse click, and Enter means No. IT can turn it off with the new `RequireRecordingConsent` policy.
- **You can see when the app is recording.** The window title starts with a red-dot "Recording" mark and the app's taskbar button shows a red dot, also when the window is minimised.
- **Discarding a meeting and allowing the app to delete originals also ask Windows to confirm**, after the app's own dialog. The deletion question names the folder.
