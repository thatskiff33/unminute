Security hardening (#111, slice A7):

- **The original app is retired.** The source-installed app this one replaced has been removed from the repository and gets no further fixes.
- **A Hugging Face token in your Windows environment is no longer used.** To download the speaker model, paste your token in Setup. A token left in your Windows environment is still kept away from the app's other processes.
- **Less left over from the original app.** The speech-model code no longer contains the original installer's own downloader, the network guard no longer has a way to let downloads through in the app's own processes, and error messages point to Repair in Settings > Manage downloads instead of the original app's Update / repair button.
- **Smaller package.** Three packages only the original app used (darkdetect, sv-ttk and tkinterdnd2) are no longer installed.
