// What the page's scripts use but do not declare themselves, for the
// report-only type check (#489; tools/ts_check.py, tsconfig.json). Types only:
// nothing here runs, and the package never includes it (only workspace/ ships).
// The page's own names need nothing here: its modules import them from each
// other (#414, #488), and the type check reads every module.

interface Window {
  /** pywebview's bridge, present once the window has injected it. Each method
   * is one of API_METHODS in workspace/api.py and answers {ok, value} or
   * {ok: false, error, kind}; core.js's call() is the only caller. */
  pywebview?: {
    api: Record<string, (...args: unknown[]) => Promise<any>>;
  };
}
