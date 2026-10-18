Security hardening (#111, follow-up to A6):

- **The page can no longer show its own text in a message box.** The browser's built-in alert, confirm and prompt boxes are turned off, and the page's alert no longer opens a Windows message box, so a compromised page cannot pose as the app or as Windows with instructions such as "tick the box, then Allow". The app's own dialogs are unaffected. Page script can still open a file picker or print preview (#175).
- **The page can no longer ask for the microphone or the screen.** Browser permission requests (microphone, camera, notifications and the rest) and screen-capture requests are refused at once, with no browser prompt or picker, so recording only ever starts through the app's own confirmation.
