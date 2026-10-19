# tools/import_dialog_check.ps1
# #20/#28: import a recording through the native Import dialog of an INSTALLED
# package, driven by UI Automation and window messages (no mouse or keyboard
# input). Uses a throwaway LOCALAPPDATA; no models are needed to import.
#
#   powershell -File tools\import_dialog_check.ps1 -Version <install>\versions\<v> -Wav <recording>

param([string]$Version, [string]$Wav)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$T = [System.Windows.Automation.TreeScope]

# taskkill by its full path in the system folder, never by name (#253). Found
# before the app starts, so a missing one stops the check with no app running.
$taskkill = Join-Path ([Environment]::SystemDirectory) 'taskkill.exe'
if (-not (Test-Path -LiteralPath $taskkill -PathType Leaf)) {
    throw 'taskkill.exe is missing from the system folder'
}

# 1. Throwaway profile, onboarding already done (no models needed to import).
$profileDir = Join-Path $env:TEMP ("umdialog-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force (Join-Path $profileDir 'Unminute\workspace') | Out-Null
'{"onboarded": true, "speakers": false}' | Out-File -Encoding ascii (Join-Path $profileDir 'Unminute\workspace\settings.json')
$env:LOCALAPPDATA = $profileDir
$app = Join-Path $Version 'app'
$proc = Start-Process -FilePath (Join-Path $Version 'runtime\pythonw.exe') -ArgumentList '-m', 'workspace' -WorkingDirectory $app -PassThru


# Processes of this app instance only (pythonw and its children).
function Get-AppPid {
    $ids = @($proc.Id)
    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
    do {
        $more = @($all | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId } |
            ForEach-Object ProcessId)
        $ids += $more
    } while ($more.Count)
    return $ids
}

# Top-level windows of this app instance; never another program's windows.
function Get-AppWindow($class) {
    $appIds = Get-AppPid
    $wins = $A::RootElement.FindAll($T::Children, [System.Windows.Automation.Condition]::TrueCondition)
    return @($wins | Where-Object { $appIds -contains $_.Current.ProcessId -and
        (-not $class -or $_.Current.ClassName -eq $class) })
}

function Find-InApp($cond, $seconds, $class) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        foreach ($w in Get-AppWindow $class) {
            if (-not $cond) { return $w }
            $e = $w.FindFirst($T::Descendants, $cond)
            if ($e) { return $e }
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

$result = [ordered]@{ pass = $false }
try {
    # 2. The app's Import button (inside WebView2). Without models the app
    #    opens on Setup; Import is on the Inbox page.
    $inboxCond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Inbox')),
        (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
    $inbox = Find-InApp $inboxCond 90
    if (-not $inbox) { throw 'Inbox button not found' }
    $inbox.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $byName = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, "Import recordings$([char]0x2026)")
    $button = Find-InApp $byName 90
    if (-not $button) { throw 'Import button not found' }
    $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()

    # 3. The app's own native Open dialog (#32770). Win32 messages to its
    #    controls (no user input): file name Edit in ComboBoxEx32 (id 1148)
    #    > ComboBox > Edit; Open is id 1.
    # Shown as a top-level window, or under its owner (the app window).
    $dlgCond = New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, '#32770')
    $dialog = Find-InApp $null 3 '#32770'
    if (-not $dialog) { $dialog = Find-InApp $dlgCond 30 }
    if (-not $dialog) { throw 'Open dialog not found' }
    $result.dialog_title = $dialog.Current.Name
    Add-Type @'
using System; using System.Runtime.InteropServices;
public static class Dlg {
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h, int id);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr a, string c, string w);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, string l);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
}
'@
    Start-Sleep -Seconds 2
    $hDlg = [IntPtr]$dialog.Current.NativeWindowHandle
    $combo = [Dlg]::GetDlgItem($hDlg, 1148)
    $editH = [Dlg]::FindWindowEx([Dlg]::FindWindowEx($combo, [IntPtr]::Zero, 'ComboBox', $null), [IntPtr]::Zero, 'Edit', $null)
    if ($editH -eq [IntPtr]::Zero) { $editH = [Dlg]::FindWindowEx($combo, [IntPtr]::Zero, 'Edit', $null) }
    if ($editH -eq [IntPtr]::Zero) { throw 'File name box not found' }
    [Dlg]::SendMessage($editH, 0x000C, [IntPtr]::Zero, $Wav) | Out-Null          # WM_SETTEXT
    [Dlg]::SendMessage([Dlg]::GetDlgItem($hDlg, 1), 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null   # BM_CLICK on Open

    # 4. The meeting appears: a read-only count, so the probe changes nothing.
    # as_uri() escapes the path: a '#' or '%41' in TEMP would break a raw URI (#164).
    $db = Join-Path $profileDir 'Unminute\workspace\workspace.db'
    $probe = "import pathlib, sqlite3, sys; d = sqlite3.connect(pathlib.Path(sys.argv[1]).absolute().as_uri() + '?mode=ro', uri=True); print(d.execute('SELECT count(*) FROM meetings').fetchone()[0])"
    $end = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        $count = & (Join-Path $Version 'runtime\python.exe') -c $probe $db 2>$null
    } while ((Get-Date) -lt $end -and "$count".Trim() -ne '1')
    $result.meetings_after_dialog = "$count".Trim()
    $result.pass = ("$count".Trim() -eq '1')
    if (-not $result.pass) { $result.error = 'No meeting was created within 120 s' }
} catch {
    $result.error = $_.Exception.Message
} finally {
    Start-Process $taskkill -ArgumentList '/F', '/T', '/PID', $proc.Id -NoNewWindow -Wait | Out-Null
    Start-Sleep -Seconds 2
    Remove-Item $profileDir -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue
    $result.profile_removed = -not (Test-Path $profileDir)
}
$result | ConvertTo-Json
if (-not $result.pass) { exit 1 }
