# Watched folders, cloud files and deleting originals

This guide is for people who set up automatic intake in Unminute (#23, #24).

## Watching a folder

Settings > Watched folders > **Watch a folder…** adds a folder. Tick **Also import recordings already in it** to import its current contents. Otherwise only recordings that arrive later are imported.

How watching works:

- **When folders are checked.** The app checks each folder 30 seconds after it starts and every few minutes while it is open. **Check now** checks at once. If the app closes during the first check of a folder added without its existing recordings, that check starts again next time, and a recording that arrives before it finishes counts as already there. If the app was closed, it catches up when it is next opened. No background service runs while the app is closed.
- **What counts as new.** A recording is new because it was not in the folder before, whatever its date. An old recording copied in today is imported.
- **Renames and moves are not new.** Renaming or moving a recording within the folder does not make it new.
- **Edited files count as new.** A file whose content changes, for example one saved over with a new recording or one with edited tags, is compared by content and imported if the content is new.
- **Unfinished copies wait.** A file is imported only once it has stopped changing between two checks and has not been written for at least a minute, so a recording that is still copying or syncing is left until it is complete.
- **Failed imports.** A damaged or unsupported file is shown as "could not be imported". It is tried again if the file changes, for example when a complete copy is saved over it.
- **Which recordings decode.** Watched or imported by hand, a recording is decoded with audio decoders only (`decode_child.AUDIO_DECODERS`): PCM (including A-law and mu-law), IMA and Microsoft ADPCM, MP3, MP2, AAC, ALAC, FLAC, Opus, Vorbis, WMA, AC-3 and E-AC-3, AMR, and the phone and VoIP speech codecs G.729, G.723.1, GSM, iLBC and Speex. Video tracks, cover art and subtitles are never decoded. Audio in any other codec (for example TrueSpeech or QDM2), a sample rate below 4 kHz or above 384 kHz, or more than one second of audio per 16 bytes of file is refused and shown as "could not be imported".
- **Microphone recordings.** A recording made in Unminute goes to the same decoder, with the same limits, passed from memory as one WAV over the decoder's input, so no unencrypted copy of it is written (#111 C3b).
- **Interrupted imports.** Recordings waiting to be imported when the app closes are found again at the next launch.
- **Pausing a folder.** **Pause** stops imports from that folder at once. Recordings the app had already found but not yet imported wait, and are imported at the check when you select **Resume**. Recordings that arrived while it was paused are new: they follow once they have been seen unchanged at two checks, as usual (for a folder paused before or during its first check, with existing recordings not included, they count as already there). The one recording being copied in at that moment still finishes. If your organisation turns watched folders off, the same applies to every folder, from when the app next reads the policy.
- **The same recording in two folders.** It is imported once. If its import from one folder is held by a pause, or fails, the copy in the other folder is tried instead.
- **Partial reads.** If part of a folder cannot be read (for example while a sync client is re-initialising), nothing is treated as removed, and a new folder's existing recordings are not mistaken for new ones.
- **No double imports.** A recording already imported, from any folder or by hand, is not imported again for 120 days. A file saved over with different content is imported. **Import recordings…** always works, so you can deliberately transcribe something again.
- **Narrow, local folders only.**
  - Watch a specific recordings folder on this computer. Synced folders such as OneDrive are fine.
  - These are refused: whole drives; your user folder and any folder that holds it (such as `C:\Users`); the Windows and program folders; `AppData` and every folder inside it (such as `Temp`, other apps' data and the old `%LOCALAPPDATA%\whisper` data folder), and everything inside a redirected `%APPDATA%` or `%LOCALAPPDATA%`; Unminute's own data folder (`%LOCALAPPDATA%\Unminute`), anything inside it and any folder that holds it; and network folders (including mapped drives). A folder watched before it was refused (by an earlier version, which refused less) is checked again at each scan: while it is refused it is not read, and its note gives the same message.
  - The app looks at most two subfolder levels deep, and does not follow links or junctions.
  - **Very long paths.** Unless long paths are turned on in Windows, Windows cannot open a file whose full path is 260 characters or longer. Such a recording is not imported, and the folder's note says how many there are; the rest of the folder is checked and imported as usual. Moving the recording to a folder with a shorter path imports it, unless it was already there when the folder was added without its existing recordings. Importing such a file by hand says its path is too long.
  - Folders are checked one after another. A large cloud file being downloaded can delay checks of other folders.

The app keeps a small record of what it has seen in each folder so it does not import things twice:

- keyed hashes of file locations, rather than names;
- sizes and dates;
- for 120 days, a fingerprint (SHA-256) of each imported recording.

None of this is meeting content. Stopping watching a folder removes that folder's records.

The location hashes use a key stored in the same workspace database. They keep names out of plain sight, but they are not a secret from someone who has the database and can guess file names.

Where a recording came from is stored only while the app may delete the original (see below). Otherwise only its fingerprint is kept, for duplicate detection and the identity check.

## Files in cloud storage (OneDrive, SharePoint and similar)

A synced folder can hold files that exist only in the cloud ("online-only"). By default the app does not download them. It shows them as "in cloud storage, not on this computer yet" and imports them once they are on this computer. You can get them onto the computer with **Always keep on this device**, or by opening them once.

Tick **Download recordings that are only in cloud storage** for a folder to let the app read such files. Reading makes the sync client download them. The app only ever reads: it never needs permission to change the folder.

What happens in awkward cases:

| Situation | What the app does |
| --- | --- |
| Sync paused, or offline | The file is checked again on the next pass. The app shows no progress percentage, because the sync client does not provide one. |
| A file is still syncing | It waits until the file stops changing. |
| No permission to read a file | Shown as "could not be read". Nothing is imported. |
| The folder is renamed, moved or disconnected | Reported on the folder. It is checked again later. |
| A file changes while it is being imported | The import is refused, and the file is tried again once it is stable. |

## Deleting originals after review

Watching a folder lets the app import from it. Deleting from it is a separate permission, and it is off by default.

**Delete originals from this folder after review** needs an explicit confirmation. When it is on, the app may delete the original recording, but only when all of these are true:

- you have confirmed the meeting's transcript is reviewed (the first cleanup step);
- the folder the recording was imported from still has this permission at that moment, so turning the permission off later keeps originals. Permission on a parent folder does not extend to recordings imported from a separately watched subfolder. Pausing a folder does not remove its permission; Stop watching does;
- the file is still exactly the one that was imported (same size, date and SHA-256). A replaced or edited file is never deleted;
- it is a plain local file, not a link, a network path or a shortcut.

Recordings imported by hand are never deleted.

Do not turn this on for:

- **Shared or team folders,** Teams channel files, or SharePoint libraries that other people rely on.
- **Recordings someone else owns.** For example, a Teams recording saved to the meeting organiser's OneDrive is theirs. Deleting it from a synced copy may delete it for them too.

## What deletion does not reach

Deleting an original removes that file from this computer's copy of the folder. It does not remove:

- **Other copies** on a phone or recorder, in email, or in chat.
- **Cloud recycle bins** (OneDrive and SharePoint keep deleted files for a period) and backups.
- **Copies made by Teams or Stream.** Their storage and retention depend on how the meeting was recorded and on your organisation's policies:
  - channel meetings are usually stored in the channel's SharePoint site;
  - other meetings are usually stored in a OneDrive Recordings folder.

  Confirm the details with IT.

Ordinary deletion is not a forensic erasure of the disk.
