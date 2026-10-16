# The speaker model is fully re-hashed before every load (#111 slice A4):
# pyannote unpickles its checkpoints, so the quick size-and-date check that
# other models use is not enough.
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SpeakerModelLoadTests(unittest.TestCase):
    def test_speaker_model_is_fully_verified_before_loading(self):
        import numpy as np

        import asr_engines
        from workspace import engines
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.f32"
            np.zeros(16000, dtype="<f4").tofile(audio)
            with mock.patch.object(asr_engines, "find_verified", return_value=None) as check:
                with self.assertRaises(asr_engines.ModelLoadError):
                    engines.diarize_file("pyannote", tmp, str(audio), None, 1)
        check.assert_called_once_with(asr_engines.SPEAKER_MODEL, tmp, full=True)


if __name__ == "__main__":
    unittest.main()
