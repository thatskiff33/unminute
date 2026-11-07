# tools/process_listeners.py
# The app's process tree and any network sockets it listens on. The window
# checks use these to show the app opens no local listener: the page loads from
# a WebView2 virtual host (https://unminute.invalid, #488), which WebView2 serves
# itself from the UI folder, and pywebview starts no HTTP server for an https://
# address (workspace/shell.py). Before #488 it loaded from file://. Windows only.
# The importing tool puts tools/ on the import path (after the standard
# library); this module leaves the path alone.

import subprocess

import windows_powershell


# Each process as "pid parent-pid created name": created is its start time in
# Windows file-time ticks (UTC), or 0 where Windows gives none.
PROCESSES = ("Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) "
             "$($_.ParentProcessId) $(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } "
             "else { 0 }) $($_.Name)\" }")


def processes():
    """{pid: (parent pid, start time or 0, name)} for every process now."""
    # Windows PowerShell by full path, with a minimal environment (#237).
    out = windows_powershell.run(PROCESSES).stdout
    found = {}
    for line in out.splitlines():
        parts = line.strip().split(" ", 3)
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            found[int(parts[0])] = (int(parts[1]), int(parts[2]),
                                    parts[3] if len(parts) == 4 else "?")
    return found


def descendants(procs, root_pid):
    """{pid: name} of root and every descendant in `procs` (processes()).

    A process names its parent by PID only, and Windows reuses a PID once its
    process has ended: a process whose parent ended long ago can name a PID
    that now belongs to this check, or to the app. So a process counts as a
    child only if it started no earlier than the parent it names, where both
    start times are known. (In v0.5.0-preview.9's release evidence, 8 Nov
    2026, the UI check reported a listener of OneDrive's sync service, whose
    parent had long ended, as the app's; a reused PID is the likely cause.)"""
    kids = {}
    for pid, (ppid, _, _) in procs.items():
        if pid != ppid:
            kids.setdefault(ppid, []).append(pid)
    tree, todo = {}, [root_pid]
    while todo:
        p = todo.pop()
        if p in tree:
            continue
        tree[p] = procs.get(p, (0, 0, "?"))[2]
        born = procs.get(p, (0, 0, "?"))[1]
        for kid in kids.get(p, []):
            kid_born = procs[kid][1]
            if not (born and kid_born and kid_born < born):
                todo.append(kid)
    return tree


def process_tree(root_pid):
    """PIDs of root and all its descendants (WebView2 runs child processes),
    with their names."""
    return descendants(processes(), root_pid)


def listeners(pids):
    """Listening TCP sockets and bound UDP sockets owned by `pids`."""
    # netstat from the system folder by full path, with the same minimal
    # environment: a netstat.exe planted beside Python or in the current folder
    # could otherwise run instead and hide a listener.
    netstat = windows_powershell.system_program("NETSTAT.EXE")
    out = subprocess.run([netstat, "-ano"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, env=windows_powershell.environment()).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in ("TCP", "UDP"):
            pid = int(parts[-1]) if parts[-1].isdigit() else -1
            state = parts[3] if parts[0] == "TCP" and len(parts) == 5 else ""
            if pid in pids and (parts[0] == "UDP" or state == "LISTENING"):
                found.append({"proto": parts[0], "local": parts[1], "pid": pid,
                              "process": pids[pid]})
    return found
