Security hardening (#111, follow-up to A6):

- **The page can no longer show its own text in a pop-up box.** The browser's built-in alert, confirm and prompt boxes are turned off, and the page's alert no longer opens a Windows message box, so a compromised page cannot pose as the app or as Windows with instructions such as "tick the box, then Allow". The app's own dialogs are unaffected.
