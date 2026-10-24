Security hardening (#111):

- **Whisper no longer looks for libraries in a folder named by `HIP_PATH`.** CTranslate2, which runs Whisper, adds `%HIP_PATH%\bin` to the places Windows looks for libraries when that variable is set. Unminute's worker processes now remove it before Whisper loads, so a library placed in that folder cannot load into them, on the graphics card or the processor. Nothing else changes for users (#357).
