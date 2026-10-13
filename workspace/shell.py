# workspace/shell.py
# Desktop shell: a pywebview (WebView2) window showing the bundled UI.
#
# The UI is loaded from a file:// URL. pywebview starts its built-in HTTP
# server (a localhost listener) for plain local paths, but not for file://
# URLs or inline HTML, so this keeps the app free of local listeners. The
# trial (tools/webview_trial.py) verifies that by listing listening sockets.
# private_mode keeps WebView2 from persisting cookies/storage between runs.

from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"


# Chromium switches for WebView2: resolve no host names (the page has nothing
# to fetch), and turn off background services that would otherwise contact
# Microsoft (SmartScreen, component updates, spell-check downloads, metrics).
WEBVIEW2_ARGS = " ".join([
    '--host-resolver-rules="MAP * ~NOTFOUND"',
    "--disable-features=msSmartScreenProtection,msEdgeEnhancedSecurityMode,"
    "SpareRendererForSitePerProcess,AutofillServerCommunication",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-pings",
    "--disable-sync",
    "--metrics-recording-only",
    "--disable-breakpad",
])


def harden_webview2():
    """Must run before the first window is created (read at WebView2 start)."""
    import os
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = WEBVIEW2_ARGS


def ui_url():
    return (UI_DIR / "index.html").as_uri()


blocked_navigations = []   # URLs the lock refused (for tests/diagnostics; no content)


def _allowed(uri):
    """Only the bundled UI folder may be shown; everything else is refused.
    pywebview injects the bridge into whatever page is loaded, so a remote
    page must never load."""
    base = UI_DIR.as_uri().lower().rstrip("/") + "/"
    return uri.lower().startswith(base) or uri.lower() == "about:blank"


def lock_navigation(window):
    """Cancel any navigation outside the bundled UI (WebView2 NavigationStarting)."""
    control = window.native.browser.webview

    def on_navigation_starting(sender, args):
        uri = str(args.Uri)
        if not _allowed(uri):
            args.Cancel = True
            blocked_navigations.append(uri.split("?")[0][:200])

    control.NavigationStarting += on_navigation_starting


def create_window(bridge, title="Meeting workspace", hidden=False):
    import webview
    # New-window requests (window.open, target=_blank) must not reach the
    # system browser: load them in place, where the navigation lock refuses them.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    webview.settings["ALLOW_DOWNLOADS"] = False
    window = webview.create_window(title, url=ui_url(), js_api=bridge,
                                   width=1100, height=760, min_size=(640, 480),
                                   hidden=hidden, text_select=True)
    window.events.before_load += lambda: _lock_once(window)
    return window


def _lock_once(window):
    if not getattr(window, "_nav_locked", False):
        lock_navigation(window)
        window._nav_locked = True


def run(func=None, args=None, debug=False):
    import webview
    webview.start(func, args, gui="edgechromium", http_server=False,
                  private_mode=True, debug=debug)
