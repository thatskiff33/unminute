Security hardening (#111, slice A7):

- **The earlier app is retired.** The original source-installed app, Whisper Transcriber, has been removed from the repository and gets no further fixes. If it is still on a computer, the user guide explains how to remove it without touching Unminute's workspace or models.
- **The option to use the earlier app's Hugging Face token is gone.** To download the speaker model, paste your token in Setup. A token the earlier app left in your Windows environment is still kept away from the app's other processes, and is no longer used.
- **Less left over from the earlier app.** The speech-model code no longer contains the earlier installer's own downloader, the network guard no longer has a way to let downloads through in the app's own processes, and error messages point to Repair in Settings > Manage downloads instead of the earlier app's Update / repair button.
- **Smaller package.** Three packages only the earlier app used (darkdetect, sv-ttk and tkinterdnd2) are no longer installed.
