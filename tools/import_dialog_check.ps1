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

# 1. Throwaway profile, onboarding already done (no models needed to import).
$profileDir = Join-Path $env:TEMP ("ww28dialog-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force (Join-Path $profileDir 'whisper\workspace') | Out-Null
'{"onboarded": true, "speakers": false}' | Out-File -Encoding ascii (Join-Path $profileDir 'whisper\workspace\settings.json')
$env:LOCALAPPDATA = $profileDir
$app = Join-Path $Version 'app'
$proc = Start-Process -FilePath (Join-Path $Version 'runtime\pythonw.exe') -ArgumentList '-m', 'workspace' -WorkingDirectory $app -PassThru

function Find-Until($scope, $cond, $seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        $e = $scope.FindFirst($T::Descendants, $cond)
        if ($e) { return $e }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

$result = [ordered]@{ profile = $profileDir }
try {
    # 2. The app window and its Import button (inside WebView2). Without models
    #    the app opens on Setup; Import is on the Inbox page.
    $inboxCond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Inbox')),
        (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
    $inbox = Find-Until $A::RootElement $inboxCond 90
    if (-not $inbox) { throw 'Inbox button not found' }
    $inbox.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $byName = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, "Import recordings$([char]0x2026)")
    $button = Find-Until $A::RootElement $byName 90
    if (-not $button) { throw 'Import button not found' }
    $result.button_found = $true
    $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()

    # 3. The native Open dialog (#32770): file name box (1148) and Open (1).
    $dlgCond = New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, '#32770')
    $dialog = Find-Until $A::RootElement $dlgCond 30
    if (-not $dialog) { throw 'Open dialog not found' }
    $result.dialog_title = $dialog.Current.Name
    # The file name box is an Edit inside a ComboBox (both AutomationId 1148 on
    # most builds); use whichever offers a value pattern.
    # Win32 messages to the dialog's own controls (no user input): the file
    # name Edit sits in ComboBoxEx32 (id 1148) > ComboBox > Edit; Open is id 1.
    Add-Type @'
using System; using System.Runtime.InteropServices; using System.Text;
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
    $result.name_box = 'set'
    [Dlg]::SendMessage([Dlg]::GetDlgItem($hDlg, 1), 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null   # BM_CLICK on Open

    # 4. The meeting appears in the workspace (content-free counts only).
    $end = (Get-Date).AddSeconds(120)
    Set-Location $app
    do {
        Start-Sleep -Seconds 2
        $status = & (Join-Path $Version 'runtime\python.exe') -c "import sys; sys.path.insert(0, '.'); from workspace.store import Store, default_root; s = Store(default_root() / 'workspace.db'); print(s._one('SELECT count(*) n FROM meetings')['n']); s.close()" 2>$null
    } while ((Get-Date) -lt $end -and "$status".Trim() -ne '1')
    $result.meetings_after_dialog = "$status".Trim()
} catch {
    $result.error = $_.Exception.Message
} finally {
    Start-Process taskkill -ArgumentList '/F', '/T', '/PID', $proc.Id -NoNewWindow -Wait | Out-Null
}
$result | ConvertTo-Json
