# Watched folders, cloud files and deleting originals

This guide is for people who set up automatic intake in the meeting workspace (#23, #24).

## Watching a folder

Settings > Watched folders > **Watch a folder…** adds a folder. Tick **Also import recordings already in it** to import its current contents. Otherwise only recordings that arrive later are imported.

How watching works:

- **When folders are checked.** The app checks each folder when it starts and every few minutes while it is open. If the app was closed, it catches up when it is next opened. No background service runs while the app is closed.
- **What counts as new.** A recording is new because it was not in the folder before, whatever its date. An old recording copied in today is imported.
- **Unfinished copies wait.** A file is imported only once it has stopped changing between two checks, so a recording that is still copying or syncing is left until it is complete.
- **No double imports.** A recording already imported, from any folder or by hand, is not imported again for 120 days. A file saved over with different content is imported. **Import recordings…** always works, so you can deliberately transcribe something again.
- **Narrow folders only.** Watch a specific recordings folder. Whole drives and the whole user folder are refused, and the app looks at most two subfolder levels deep.

The app keeps a small record of what it has seen in each folder so it does not import things twice:

- keyed hashes of file locations, not names;
- sizes and dates;
- for 120 days, a fingerprint (SHA-256) of each imported recording.

None of this is meeting content. Stopping watching a folder removes that folder's records.

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
- the original is still in a folder with this permission at that moment, so turning the permission off later keeps originals;
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
