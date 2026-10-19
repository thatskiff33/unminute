Security hardening (#111, slice B3):

- **The release build no longer fails its signature check when started from PowerShell 7, as on GitHub's runners.** The check that the bundled Python is signed by the Python Software Foundation is as strict as before, and Windows PowerShell now runs only from the Windows system folder, with modules only from the folders an administrator controls.
