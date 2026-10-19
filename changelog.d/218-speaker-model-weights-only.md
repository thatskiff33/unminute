Security hardening (#111, slice B9):

- **The speaker model loads only what was checked.** Each speaker model file is now read once and checked, and speaker identification is built from those same bytes in memory. Before, pyannote opened the files again after the check, and in a way that could run code from a tampered file. The model's weights are now read in a mode that cannot run code. Only the pinned model's exact settings are accepted, and a file that fails is refused with one plain message. `--self-test` checks that a model file built to run code is refused.
