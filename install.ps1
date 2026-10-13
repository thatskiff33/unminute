# install.ps1 - One-click setup for the local Whisper transcriber.
#
# Safe to re-run: every step checks what already exists and skips it.
# Needs NO admin rights. Everything is per-user:
#   - Python 3.12 (per-user install, only if no suitable Python is found)
#   - venv + packages   -> %LOCALAPPDATA%\whisper\.venv   (~1.2 GB)
#   - model weights     -> %LOCALAPPDATA%\whisper-models  (0.7-4.6 GB)
#   - HF_TOKEN          -> user environment variable (optional, speaker ID only)
#   - one Start Menu shortcut "Whisper Transcriber"
#
# Usual entry point is install.cmd (double-click). Unattended use:
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Models turbo -SkipToken
param(
    # 'parakeet' = Parakeet English only, 'turbo' = Standard (large-v3-turbo
    # only), 'full' = turbo + large-v3. Omit to be asked interactively.
    # Any Parakeet model already on disk is verified (and repaired) every run.
    [ValidateSet('parakeet', 'turbo', 'full')]
    [string]$Models,
    [switch]$SkipModels,    # skip the model download step entirely
    [switch]$SkipToken,     # skip the Hugging Face token step (no speaker ID)
    [switch]$SkipShortcut   # skip the Start Menu shortcut
)

$ErrorActionPreference = 'Stop'
# python.org and huggingface.co require TLS 1.2; PS 5.1 may default lower.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- Paths (all per-user, all off OneDrive) ----------------------------------
$RepoDir     = $PSScriptRoot
$WhisperHome = Join-Path $env:LOCALAPPDATA 'whisper'
$VenvDir     = Join-Path $WhisperHome '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$ModelCache  = Join-Path $env:LOCALAPPDATA 'whisper-models'
$PyVersion   = '3.12.10'   # matches the environment the pinned wheels were tested on

function Write-Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Note([string]$msg) { Write-Host "  $msg" }
function Write-Warn2([string]$msg){ Write-Host "  WARNING: $msg" -ForegroundColor Yellow }
function Fail([string]$msg)       { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# Run a short Python snippet with the venv interpreter via a temp file
# (avoids quoting problems); returns the exit code, prints output as it goes.
function Invoke-VenvPython([string]$code) {
    $tmp = Join-Path $env:TEMP ("whisper-install-{0}.py" -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    Set-Content -Path $tmp -Value $code -Encoding ascii
    try {
        # Out-Host displays Python's output instead of letting it leak into
        # this function's return value (which must be just the exit code).
        & $VenvPython $tmp | Out-Host
        return $LASTEXITCODE
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

# Stamp an explicit AppUserModelID onto a .lnk so a pinned shortcut groups with
# the running window (the app sets the same ID at startup). The target is
# pythonw.exe, whose default identity is shared with every other Python app, so
# without this a taskbar pin shows as a second button while the app is running.
# Done via the shell property store (no admin needed).
function Set-ShortcutAppId([string]$LnkPath, [string]$AppId) {
    if (-not ([System.Management.Automation.PSTypeName]'ShellLnk.Helper').Type) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ShellLnk {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid;
        public PropertyKey(Guid id, uint p) { fmtid = id; pid = p; } }
    [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPersistFile {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string n, int m);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string n, [MarshalAs(UnmanagedType.Bool)] bool r);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string n);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string n); }
    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint c);
        void GetAt(uint i, out PropertyKey k);
        void GetValue(ref PropertyKey k, out PropVariant v);
        void SetValue(ref PropertyKey k, ref PropVariant v);
        void Commit(); }
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")] public class CShellLink { }
    public static class Helper {
        static PropertyKey K = new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);
        [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant pv);
        public static void SetAppId(string lnk, string appId) {
            IPersistFile pf = (IPersistFile)(new CShellLink());
            pf.Load(lnk, 2); // STGM_READWRITE
            IPropertyStore store = (IPropertyStore)pf;
            PropVariant pv = new PropVariant();
            pv.vt = 31; // VT_LPWSTR
            pv.p = Marshal.StringToCoTaskMemUni(appId);
            store.SetValue(ref K, ref pv);
            store.Commit();
            PropVariantClear(ref pv);
            pf.Save(lnk, true);
        }
    }
}
'@
    }
    [ShellLnk.Helper]::SetAppId($LnkPath, $AppId)
}

if (-not (Test-Path (Join-Path $RepoDir 'requirements.txt'))) {
    Fail "requirements.txt not found next to install.ps1. Run the installer from the whisper folder."
}

Write-Host 'Whisper transcriber - local, offline setup' -ForegroundColor White
Write-Host '(audio never leaves this machine)'

# --- 1. Preflight -------------------------------------------------------------
Write-Step 'Step 1/8: Preflight checks'

$drive  = (Get-Item $env:LOCALAPPDATA).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
$fresh  = -not (Test-Path $VenvPython)
if ($fresh -and $freeGB -lt 8) {
    Fail "Only $freeGB GB free on $($drive.Name): - a fresh install needs about 8 GB."
} elseif ($freeGB -lt 4) {
    Write-Warn2 "Only $freeGB GB free on $($drive.Name): - model downloads may not fit."
} else {
    Write-Ok "Disk space OK ($freeGB GB free)."
}

if ($RepoDir -like '*OneDrive*') {
    Write-Warn2 'This folder is inside OneDrive. Transcripts saved here will sync to the cloud.'
    Write-Warn2 'Consider moving the whisper folder to e.g. C:\Users\<you>\Local_Repos first.'
} else {
    Write-Ok 'Folder is not OneDrive-synced.'
}

$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Write-Ok "Installed RAM: $ramGB GB."

# --- 2. Find or install Python -------------------------------------------------
Write-Step 'Step 2/8: Python'

# Returns the exe path if it is CPython 3.12, else $null. Only 3.12 is accepted:
# several pinned packages (numpy, scipy, PyAV, onnxruntime) no longer publish
# Windows wheels for 3.10/3.11, and the pins are only tested on 3.12.
function Test-PythonCandidate([string]$exe) {
    if (-not $exe) { return $null }
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.12$') { return (& $exe -c "import sys; print(sys.executable)") }
    } catch {}
    return $null
}

$python = $null
# Preferred: the py launcher pointing at 3.12, then the default per-user path,
# then any 3.x the launcher knows, then python on PATH.
try { $python = Test-PythonCandidate (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $python) { $python = Test-PythonCandidate (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe') }
if (-not $python) { try { $python = Test-PythonCandidate (& py -3 -c "import sys; print(sys.executable)" 2>$null) } catch {} }
if (-not $python) { $python = Test-PythonCandidate ((Get-Command python -ErrorAction SilentlyContinue).Source) }

if ($python) {
    Write-Ok "Using Python at $python"
} else {
    Write-Note "No Python 3.12 found (other versions are not used). Installing Python $PyVersion per-user (no admin)..."
    $installer = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
    if (-not (Test-Path $installer)) {
        Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" `
            -OutFile $installer -UseBasicParsing
    }
    # Per-user silent install: no UAC prompt, nothing system-wide.
    Start-Process $installer -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_launcher=1', 'Include_test=0' -Wait
    $python = Test-PythonCandidate (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    if (-not $python) { Fail 'Python install did not complete. Re-run this installer, or ask IT to install Python 3.12 (per-user).' }
    Write-Ok "Installed Python $PyVersion to $python"
}

# --- 3. Virtual environment ----------------------------------------------------
Write-Step 'Step 3/8: Virtual environment'

if (Test-Path $VenvPython) {
    Write-Ok "venv already exists at $VenvDir"
} else {
    Write-Note "Creating venv at $VenvDir ..."
    New-Item -ItemType Directory -Force -Path $WhisperHome | Out-Null
    & $python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) { Fail 'Could not create the virtual environment.' }
    Write-Ok 'venv created.'
}

# --- 4. Python packages ----------------------------------------------------------
Write-Step 'Step 4/8: Python packages (pinned versions, wheels only)'

& $VenvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
# --only-binary=:all: -> never compile from source (no compiler available, no admin).
& $VenvPython -m pip install -r (Join-Path $RepoDir 'requirements.txt') `
    --only-binary=:all: --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail 'Package installation failed. Check your network connection and re-run.' }
Write-Ok 'All packages present at pinned versions.'

# --- 5. Hugging Face token (optional - needed for speaker identification only) ----
Write-Step 'Step 5/8: Hugging Face token (for speaker identification)'

# Look in this process, then the user registry (setx from another window
# does not reach an already-open console).
$token = $env:HF_TOKEN
if (-not $token) {
    $token = (Get-ItemProperty 'HKCU:\Environment' -ErrorAction SilentlyContinue).HF_TOKEN
    if ($token) { $env:HF_TOKEN = $token }
}

if ($SkipToken) {
    Write-Note 'Skipped (-SkipToken). Speaker identification will be unavailable until a token is set.'
} elseif (-not $token) {
    Write-Note 'Speaker identification (who said what) needs a free Hugging Face account:'
    Write-Note '  1. Create an account at https://huggingface.co/join'
    Write-Note '  2. Accept the terms on BOTH of these pages (one click each):'
    Write-Note '       https://huggingface.co/pyannote/speaker-diarization-community-1'
    Write-Note '       https://huggingface.co/pyannote/speaker-diarization-3.1'
    Write-Note '  3. Create a token (type: Read) at https://huggingface.co/settings/tokens'
    Write-Note 'The token only authorises the one-off model download. Audio never leaves this machine.'
    $entered = Read-Host 'Paste your token now, or press Enter to skip (you can re-run the installer later)'
    if ($entered) {
        setx HF_TOKEN $entered | Out-Null   # user-level env var, no admin needed
        $env:HF_TOKEN = $entered
        $token = $entered
    } else {
        Write-Note 'Skipped. Transcription works fine; the speakers option will be greyed out.'
    }
}

if ($token -and -not $SkipToken) {
    # Verify the token and the gated-repo acceptance before trying any download.
    $code = Invoke-VenvPython @'
import sys
from huggingface_hub import HfApi
api = HfApi()
try:
    name = api.whoami()["name"]
except Exception:
    print("  Token was not accepted by huggingface.co (wrong/expired token?).")
    sys.exit(2)
try:
    api.model_info("pyannote/speaker-diarization-community-1")
    print(f"  Token OK (account: {name}); gated model access confirmed.")
except Exception:
    print(f"  Token OK (account: {name}), but the gated model terms are NOT accepted yet.")
    print("  Visit https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the terms.")
    sys.exit(3)
'@
    if ($code -ne 0) { Write-Warn2 'Speaker identification will not work until this is fixed. Everything else continues.' }
}

# --- 6. Model downloads -----------------------------------------------------------
Write-Step 'Step 6/8: Speech models'

$PyaDir     = Join-Path $ModelCache 'models--pyannote--speaker-diarization-community-1'

if ($SkipModels) {
    Write-Note 'Skipped (-SkipModels). The app will download models on first use instead.'
} else {
    if (-not $Models) {
        # Parakeet is the recommended English engine. Whisper sets remain for
        # anyone who wants them; Full only makes sense with plenty of RAM
        # (large-v3 is also painfully slow on smaller CPUs).
        Write-Note 'Which models do you want?'
        Write-Note "  [1] Parakeet - English, fast (~0.7 GB). Recommended."
        Write-Note "  [2] Standard - Whisper large-v3-turbo only (~1.6 GB)."
        Write-Note "  [3] Full     - Whisper turbo + large-v3 (~4.6 GB). For 32 GB machines; large-v3 is slowest."
        $choice = Read-Host "Enter 1, 2 or 3 (Enter = 1)"
        $Models = switch ($choice) { '2' { 'turbo' } '3' { 'full' } default { 'parakeet' } }
    }
    Write-Note "Selected: $Models"
    New-Item -ItemType Directory -Force -Path $ModelCache | Out-Null

    # Make every download land in the shared, non-synced cache.
    $env:HF_HUB_CACHE = $ModelCache
    $env:HUGGINGFACE_HUB_CACHE = $ModelCache

    # Download the selected models and verify every speech model already on
    # disk against its pinned checksums and preprocessing settings. A folder
    # existing is not proof of a working model. A damaged copy is never
    # deleted: Parakeet's is set aside as .invalid-<date>, and an old Whisper
    # cache is left untouched while a verified copy is downloaded beside it.
    # 'parakeet' adds the recommended model only when no Parakeet model is on
    # disk yet, so Update / repair on a v3-only install does not pull v2.
    $selected = switch ($Models) {
        'parakeet' { @() }
        'turbo'    { @('large-v3-turbo') }
        'full'     { @('large-v3-turbo', 'large-v3') }
    }
    $ensureArgs = @('ensure', '--present', '--cache', $ModelCache)
    if ($Models -eq 'parakeet') { $ensureArgs += '--default-parakeet' }
    foreach ($m in $selected) { $ensureArgs += @('--model', $m) }
    Write-Note 'Checking speech models (verifying large files can take a minute)...'
    & $VenvPython (Join-Path $RepoDir 'asr_engines.py') @ensureArgs | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'A speech model could not be downloaded or repaired. Check your network connection and re-run.' }

    if ($env:HF_TOKEN) {
        if (Test-Path $PyaDir) {
            Write-Ok 'Speaker identification model already downloaded.'
        } else {
            Write-Note 'Downloading speaker identification model (~100 MB)...'
            $code = Invoke-VenvPython @'
from pyannote.audio import Pipeline
Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")
print("  Speaker identification model downloaded and loads OK.")
'@
            if ($code -ne 0) { Write-Warn2 'Speaker model download failed (token/terms issue above?). Transcription itself is unaffected.' }
        }
    } else {
        Write-Note 'No token set - skipping the speaker identification model.'
    }
}

# --- 7. Start Menu shortcut ---------------------------------------------------------
Write-Step 'Step 7/8: Start Menu shortcut'

if ($SkipShortcut) {
    Write-Note 'Skipped (-SkipShortcut).'
} else {
    $lnkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Whisper Transcriber.lnk'
    $iconFile = Join-Path $RepoDir 'whisper.ico'
    if (Test-Path $iconFile) { $iconLoc = "$iconFile,0" }   # custom mic icon, ships with the repo
    else { $iconLoc = "$env:SystemRoot\System32\SndVol.exe,0" }  # fallback: built-in speaker icon
    $ws  = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath       = Join-Path $VenvDir 'Scripts\pythonw.exe'   # no console window
    $lnk.Arguments        = '"{0}"' -f (Join-Path $RepoDir 'transcribe_ui.py')
    $lnk.WorkingDirectory = $RepoDir
    $lnk.IconLocation     = $iconLoc
    $lnk.Description      = 'Local offline audio transcription (Whisper)'
    $lnk.Save()
    # Stamp the matching AppUserModelID so taskbar pinning groups cleanly.
    try { Set-ShortcutAppId $lnkPath 'Unlimit.WhisperTranscriber' }
    catch { Write-Warn2 "Could not set the taskbar identity on the shortcut: $($_.Exception.Message)" }
    Write-Ok "Shortcut created: Start Menu > Whisper Transcriber"
}

# --- 8. Self-check ----------------------------------------------------------------
Write-Step 'Step 8/8: Self-check'

$code = Invoke-VenvPython @'
import faster_whisper, onnx_asr, onnxruntime, av, tkinter, sv_ttk, tkinterdnd2, darkdetect
print("  All imports OK (faster-whisper, onnx-asr, PyAV, tkinter, theme, drag-and-drop).")
'@
if ($code -ne 0) { Fail 'Self-check failed - one of the packages did not import. Re-run the installer.' }

Write-Host ''
Write-Host '=== Setup complete ===' -ForegroundColor Green
Write-Host "  Launch:       Start Menu > 'Whisper Transcriber'  (or double-click whisper-ui.cmd)"
Write-Host "  Transcripts:  saved to the 'extracted' folder next to the app"
Write-Host "  Re-run this installer any time to repair or add models - it skips whatever is already set up."
