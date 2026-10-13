# pywebview / WebView2 feasibility trial (#10)

Evidence for the desktop shell of the planned meeting workspace. The legacy
tkinter app is unchanged. Re-run with:

```powershell
python tools\webview_trial.py path\to\synthetic-16k-mono.wav report.json [--harden]
```

The trial opens the real WebView2 window for about 20 seconds, using only
synthetic data, with audio muted. It exits non-zero if a security expectation fails.

## Tested configuration (13 October 2026)

| Item | Version |
| --- | --- |
| Windows | 11 Pro 10.0.26200, standard user (medium integrity, not in Administrators) |
| WebView2 runtime | 154.0.4258.62 (Evergreen, already present on Windows 11) |
| Python | 3.12.10 |
| pywebview | 6.2.1 (EdgeChromium backend via pythonnet 3.2.0, clr_loader 0.3.1) |

## Results

| Acceptance point | Result |
| --- | --- |
| Bundled HTML/CSS/JS, no remote executable UI | UI loads from `file://` and the CSP allows only `'self'` scripts. Injecting a remote `<script>` was blocked and reported as a CSP violation. |
| Does asset loading create a local listener? | Not with `file://`. With `file://` plus `http_server=False`: 0 TCP listeners and 0 UDP sockets across the Python process and all WebView2 child processes, both idle and after the tests. pywebview does start its bottle HTTP server on localhost when given a plain local path, so the app must always pass a `file://` URL (`workspace/shell.py`). |
| Long-document rendering | 20,000 paragraphs (about 480,000 words) rendered in about 1.3 s with the final bridge and hardening (0.6 s in the first version). Rendering uses `content-visibility: auto` and DocumentFragment batches of 2,000. |
| Hostile markup | Transcript text containing `<img onerror>`, `<script>`, `javascript:` and `</li><li onclick>` rendered as visible text: 0 elements created, nothing executed. All text uses `textContent`. |
| Bounded playback | A 3 s clip played in full (3.0 s media duration, 3.3 s wall time). Clips over 30 s, inverted or negative ranges, non-numeric arguments (`'0; import os'`), booleans and NaN were refused with plain-language errors. Audio reaches the page only as bounded `data:` URLs; no file paths are exposed to the page. |
| Narrow bridge | The page can call exactly `clip`, `ping` and `transcript`; everything else is refused at dispatch (see "Bridge dispatch guard" below). Errors come back as `{ok: false, error}` values with no stack traces or paths. |
| Page network and file access | `fetch('https://…')` and `fetch('file:///C:/Windows/win.ini')` were both blocked (CSP `connect-src 'none'`). |
| Navigation and new windows | See "Navigation lock" below: blocked. |
| Invalid paths and junction escapes | `workspace/safe_paths.py` accepts only `X:\...` local paths. It refuses relative and drive-relative paths, rooted paths without a drive, UNC and device paths in any separator mix (`\\`, `//`, `/\`, `\/`), NUL characters, NTFS alternate data streams, `..` escapes, and links or junctions whose real target is outside the allowed root or on a network path. It also refuses non-regular files, wrong types, empty or oversized files, and cloud placeholders that are not on disk. Covered by unit tests, including a real junction. |
| Standard-user startup | Passed as a standard (non-admin) user. A clean-profile test is still to do (see below). |

### Navigation lock (security finding fixed during the trial)

pywebview injects the bridge into any page the window shows, and by default:

- page script can navigate the window (`location.href = 'https://…'`) to a remote page, which then gets the bridge; and
- `window.open` / `target=_blank` URLs are handed to the system browser (`OPEN_EXTERNAL_LINKS_IN_BROWSER = True`). Such a URL can carry data out.

Both were reproduced. The shell now sets `OPEN_EXTERNAL_LINKS_IN_BROWSER = False` and `ALLOW_DOWNLOADS = False`, and cancels every WebView2 `NavigationStarting` outside the bundled UI folder. The trial verified that remote navigation, `window.open` and a `file://` escape to another folder were all refused. The page stayed on the bundled UI, the system browser was never invoked, and the bridge kept working.

### Bridge dispatch guard (security finding fixed after independent review)

pywebview advertises only non-underscore methods, but the function that handles calls from the page (`webview.util.js_bridge_call`) resolves the requested name with `getattr` over every dotted segment, with no filter. Page script that bypasses the advertised stubs (`pywebview._jsApiCallback(name, …)`) could therefore call `__init__`, private attributes or their methods. With the first version's numpy array in a private attribute, `_audio.tofile(path)` gave an arbitrary file write.

Fixes:

- `workspace.shell.install_dispatch_guard()` wraps `js_bridge_call` and refuses any name that is not exactly `clip`, `ping`, `transcript`, or pywebview's own promise callback. Dotted and private names are refused, as are pywebview's built-in window-move, state and DOM-event handlers, which the app does not use.
- The bridge is a facade with no state (`__slots__ = ()`); its data lives in a closure.
- Bridge methods never raise. pywebview would otherwise send a Python stack trace, with file paths, to the page.

The trial now posts these raw calls through `_jsApiCallback`: `__init__`, `__class__.__init__`, `clip.__globals__.clear`, `clip.__globals__.__setitem__`, `_paragraphs`, `transcript.__code__`, `pywebviewStateUpdate` and `pywebviewMoveWindow`. All 8 were refused, the data was unchanged, and the bridge kept working.

Also from the review:

- `ALLOW_FILE_URLS` is set to False. Otherwise pywebview adds `--allow-file-access-from-files`.
- `about:blank` is no longer an allowed navigation target.
- The navigation allowlist compares percent-decoded URLs, because Python and Chromium encode characters such as `( ) ' !` differently.
- The lock attaches at `before_load`, after the first navigation (the bundled page), and stays attached for every later navigation and reload.

### WebView2 hardening (on by default)

`create_window()` calls `harden_webview2()` unless told not to. It passes Chromium switches through `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`: resolve no host names, and disable SmartScreen, component updates, background networking, domain reliability, pings and crash upload. The trial passed with and without them; they are defence in depth on top of the CSP and navigation lock.

Not verified: that `--host-resolver-rules` takes effect. The page cannot attempt a lookup to prove it, because the CSP blocks every request first.

## Dependency finding: wheels-only installs

pywebview 6.2.1 depends on `proxy_tools`, which is published only as a 2014 source distribution (0.1.0, pure Python, no wheel). It also depends on `bottle` (only used for the HTTP server, which this design does not start). Under this repository's `--only-binary=:all:` policy, pywebview cannot be installed. Options:

1. Allow source for that one package (`--no-binary=proxy_tools`). This works and was used for the trial.
2. Build a wheel of `proxy_tools` once, then ship it hash-pinned in the private runtime (#11). This keeps installs binary-only.

Recommendation: option 2 for the packaged runtime. The workspace's dependencies stay out of `requirements.txt` until #11 decides how the runtime is assembled, so the legacy installer is unaffected.

## Known limitation

Validation returns a path, not an open file handle, so a junction swapped in between validation and opening would be followed. That is acceptable for a single-user local app. Opening by handle can be added when imports are implemented (#20/#23).

## Still to verify

- [ ] Startup on a clean Windows profile or machine. That needs a second account, Windows Sandbox or a VM, which this trial machine's standard user cannot create. It belongs to #11's clean standard-user install test.
- [ ] Packaged build (#11). This trial ran from a source checkout.
- [ ] Keyboard and screen-reader behaviour of the real workspace UI (#27).
