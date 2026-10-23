Security hardening (#111, phase C):

- **Removing a meeting's audio makes it unreadable first.** When you confirm the transcript review (or discard a meeting), Unminute now deletes the audio's key and clears it from the database's log before it deletes any audio file. If the app stops part way, or another program is holding a file, whatever is left can no longer be read, and the meeting shows its audio as removed while the leftover files are deleted on the next try. An import that fails also deletes its keys before its files.
