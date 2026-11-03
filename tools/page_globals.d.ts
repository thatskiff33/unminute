// What the page's scripts use but do not declare themselves, for the
// report-only type check (#489; tools/ts_check.py, tsconfig.json). Types only:
// nothing here runs, and the package never includes it (only workspace/ ships).
// The page's own globals need nothing here: its ordered classic scripts
// (index.html, #414) share one global scope, and the type check reads them all.

interface Window {
  /** pywebview's bridge, present once the window has injected it. Each method
   * is one of API_METHODS in workspace/api.py and answers {ok, value} or
   * {ok: false, error, kind}; core.js's call() is the only caller. */
  pywebview?: {
    api: Record<string, (...args: unknown[]) => Promise<any>>;
  };
}
