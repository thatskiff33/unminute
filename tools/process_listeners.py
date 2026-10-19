# tools/process_listeners.py
# The app's process tree and any network sockets it listens on. The window
# checks use these to show the app opens no local listener (the page loads from
# file://, so pywebview starts no HTTP server; workspace/shell.py). Windows only.
# The importing tool puts tools/ on the import path (after the standard
# library); this module leaves the path alone.

import subprocess

import windows_powershell


def process_tree(root_pid):
    """PIDs of root and all its descendants (WebView2 runs child processes)."""
    # Windows PowerShell by full path, with a minimal environment (#237).
    out = windows_powershell.run("Get-CimInstance Win32_Process | ForEach-Object "
                                 "{ \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"
                                 ).stdout
    rows = [line.split(" ", 2) for line in out.splitlines() if line.strip()]
    children, names = {}, {}
    for pid, ppid, name in rows:
        children.setdefault(int(ppid), []).append(int(pid))
        names[int(pid)] = name
    tree, todo = set(), [root_pid]
    while todo:
        p = todo.pop()
        if p not in tree:
            tree.add(p)
            todo += children.get(p, [])
    return {p: names.get(p, "?") for p in tree}


def listeners(pids):
    """Listening TCP sockets and bound UDP sockets owned by `pids`."""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
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
