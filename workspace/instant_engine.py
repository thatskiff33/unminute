# workspace/instant_engine.py
# Instant live words (#105): NVIDIA Nemotron Speech Streaming 0.6B
# (asr_engines.INSTANT_MODEL, 560 ms chunks, int8), run with the app's own ONNX
# Runtime and numpy. No sherpa-onnx (the user's decision DEC-092): its
# Windows binaries carry espeak-ng code and a second ONNX Runtime whose
# telemetry the app's switch does not reach. The method and the comparison
# with sherpa-onnx on public audio are in docs/ENGINE_EVALUATION.md.
#
#   - Features: Kaldi-style online filter-bank frames (25 ms, 10 ms shift,
#     edges reflected), pre-emphasis 0.97, a symmetric Hann window, a 512-point
#     FFT, the power spectrum, 128 mel bands on librosa's Slaney scale with
#     Slaney normalisation (0 to 8 kHz), and the natural log with NeMo's 2^-24
#     guard. No normalisation (the export's metadata says none).
#   - Encoder: cache-aware streaming, one call per window_size feature frames
#     (65), moving on by chunk_shift (56: 560 ms), carrying its three cache
#     tensors from call to call, all as the export's metadata gives them.
#   - Decoding: greedy RNN-T, at most MAX_SYMBOLS tokens per encoder frame; the
#     prediction network continues from the state returned with its output.
#   - Integrity (I1): the four pinned files are read once and checked by SHA-256
#     over exactly those bytes (asr_engines.read_verified), and the sessions are
#     built from the same bytes, so no file is opened again after its check.
#     ONNX Runtime's Python session keeps the bytes it was given for a retry
#     on another execution provider, which this CPU-only engine never makes;
#     they are let go (_release), saving about 0.6 GB.
#   - One intra-op thread, sequential, no spinning (with two threads the
#     16 October trial's runtime busy-waited for the same output).
# Standard library and numpy only at module level: onnxruntime is imported by
# load(), in the instant-words worker (tests/test_import_hygiene.py).

import math

import numpy as np

from workspace import asr_engines
from workspace.asr_engines import ModelLoadError

SAMPLE_RATE = 16000
FRAME_LEN = 400                  # 25 ms
FRAME_SHIFT = 160                # 10 ms
NFFT = 512
PREEMPH = 0.97
LOG_GUARD = 2.0 ** -24           # NeMo's log zero guard
MAX_SYMBOLS = 5                  # tokens one encoder frame may give (1 loses many words)
THREADS = 1
FEATURE_SECONDS = FRAME_SHIFT / SAMPLE_RATE


def _hz_to_mel(f):
    """librosa's Slaney mel scale (htk=False)."""
    f = np.asarray(f, dtype=np.float64)
    log_part = 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (math.log(6.4) / 27.0)
    return np.where(f >= 1000.0, log_part, f / (200.0 / 3))


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    return np.where(m >= 15.0, 1000.0 * np.exp((math.log(6.4) / 27.0) * (m - 15.0)),
                    m * (200.0 / 3))


def mel_banks(n_mels, fmin=0.0, fmax=SAMPLE_RATE / 2):
    """librosa.filters.mel(sr=16000, n_fft=512, n_mels, htk=False, norm="slaney"):
    [n_mels, 257] float32."""
    fft_f = np.linspace(0, SAMPLE_RATE / 2, NFFT // 2 + 1)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_f[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels]))[:, None]
    return weights.astype(np.float32)


class Fbank:
    """Online log-mel frames from 16 kHz float32 audio, emitted as soon as their
    samples have arrived. Frame f covers samples from f * 160 - 120 (Kaldi's
    snip_edges=False framing); before the start the audio is reflected."""

    def __init__(self, n_mels=128):
        i = np.arange(FRAME_LEN)
        self._window = (0.5 - 0.5 * np.cos(2 * math.pi * i / (FRAME_LEN - 1))).astype(np.float32)
        self._banks = mel_banks(n_mels).T.copy()           # [257, n_mels]
        self.n_mels = n_mels
        self._buf = np.zeros(0, dtype=np.float32)          # samples from self._base
        self._base = 0
        self._next = 0                                     # next frame

    @staticmethod
    def _start(frame):
        return frame * FRAME_SHIFT + FRAME_SHIFT // 2 - FRAME_LEN // 2

    def accept(self, samples):
        """New samples in, [frames, n_mels] float32 out (possibly none)."""
        x = np.asarray(samples, dtype=np.float32)
        self._buf = np.concatenate([self._buf, x])
        total = self._base + len(self._buf)
        frames = []
        while self._start(self._next) + FRAME_LEN <= total:
            start = self._start(self._next)
            if start < 0:                 # the first frame: nothing is dropped before it
                seg = self._buf[:start + FRAME_LEN]
                frames.append(np.concatenate([seg[:-start][::-1], seg]))
            else:
                frames.append(self._buf[start - self._base:start - self._base + FRAME_LEN])
            self._next += 1
        keep = self._start(self._next)
        if keep > self._base:
            self._buf = self._buf[keep - self._base:]
            self._base = keep
        if not frames:
            return np.zeros((0, self.n_mels), dtype=np.float32)
        fr = np.stack(frames)
        fr = np.concatenate([fr[:, :1] * (1 - PREEMPH), fr[:, 1:] - PREEMPH * fr[:, :-1]], 1)
        spec = np.fft.rfft(fr * self._window, n=NFFT)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        return np.log(power @ self._banks + LOG_GUARD).astype(np.float32)


def read_tokens(data):
    """tokens.txt ("<piece> <id>" per line) -> {id: text}, the word marker
    "▁" as a space and special tokens ("<unk>", "<blk>") left out."""
    tokens = {}
    for line in data.decode("utf-8").splitlines():
        piece, _, number = line.rpartition(" ")
        if not piece or not number.isdigit():
            continue
        if piece.startswith("<") and piece.endswith(">"):
            continue
        tokens[int(number)] = piece.replace("▁", " ")
    return tokens


class Stream:
    """One stream's streaming state: features, the encoder's caches and the
    greedy decoder. accept(samples) -> [(seconds from the stream's start, text
    piece)] for the tokens decoded from what has arrived."""

    def __init__(self, model):
        self.m = model
        self.fbank = Fbank(model.feat_dim)
        self._feats = np.zeros((0, model.feat_dim), dtype=np.float32)
        self._used = 0                    # feature frames already moved past (by shift)
        self._cache_ch = np.zeros(model.cache_shapes[0], dtype=np.float32)
        self._cache_t = np.zeros(model.cache_shapes[1], dtype=np.float32)
        self._cache_len = np.zeros((1,), dtype=np.int64)
        self._frames = 0                  # encoder frames decoded so far
        h = np.zeros((model.pred_layers, 1, model.pred_hidden), dtype=np.float32)
        # The prediction network's output for the last token (blank at the
        # start) and the state returned with it, which the next step continues.
        self._dec_out, self._dec_state = model.predict(model.blank, (h, h.copy()))

    def accept(self, samples):
        feats = self.fbank.accept(samples)
        if len(feats):
            self._feats = np.concatenate([self._feats, feats])
        out = []
        m = self.m
        while len(self._feats) - self._used >= m.window:
            out += self._chunk(self._feats[self._used:self._used + m.window])
            self._used += m.shift
        if self._used >= m.window:        # frames no later chunk needs
            self._feats = self._feats[self._used:]
            self._used = 0
        return out

    def _chunk(self, x):
        m = self.m
        enc, length, self._cache_ch, self._cache_t, self._cache_len = m.encode(
            x, self._cache_ch, self._cache_t, self._cache_len)
        out = []
        n = int(length[0])
        for t in range(n):
            e = enc[:, :, t:t + 1]
            for _ in range(MAX_SYMBOLS):
                k = m.join(e, self._dec_out)
                if k == m.blank:
                    break
                piece = m.tokens.get(k)
                if piece:
                    out.append((round((self._frames + t) * m.subsampling * FEATURE_SECONDS, 3),
                                piece))
                self._dec_out, self._dec_state = m.predict(k, self._dec_state)
        self._frames += n
        return out


class InstantEngine:
    """The streaming model, loaded offline from verified bytes (I1, N1)."""

    device = "cpu"

    def __init__(self, cache_dir, threads=THREADS):
        self.spec = asr_engines.INSTANT_MODEL
        self.cache_dir, self.threads = cache_dir, threads
        self._enc = self._dec = self._join = None
        self.tokens = {}

    @property
    def description(self):
        return (f"{self.spec.name} instant words (ONNX {self.spec.quantization}, {self.device}, "
                f"revision {self.spec.revision[:7]})")

    def load(self):
        """Verify then load, entirely from local files. Raises ModelLoadError.
        Only IT can repair IT's copy (#111 B7a), so its message names IT."""
        blobs = asr_engines.read_verified(self.spec, self.cache_dir)
        if blobs is None:
            fix = (f". {asr_engines.machine_damaged(self.spec)} "
                   if asr_engines.machine_copy(self.spec) is not None else " ")
            raise ModelLoadError(f"{asr_engines.label(self.spec.name)} cannot be used for "
                                 f"instant words{fix}(not installed, or a file does not match "
                                 "its checksum)")
        try:
            self._load(blobs)
        except Exception as exc:
            self._enc = self._dec = self._join = None
            raise ModelLoadError(f"{asr_engines.label(self.spec.name)} failed to load for "
                                 f"instant words ({type(exc).__name__}: {exc})") from exc
        return self

    def _load(self, blobs):
        import onnxruntime as rt
        rt.disable_telemetry_events()     # before any session, in every process
        so = rt.SessionOptions()
        so.intra_op_num_threads = int(self.threads)
        so.inter_op_num_threads = 1
        so.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        so.add_session_config_entry("session.inter_op.allow_spinning", "0")
        so.log_severity_level = 3
        sessions = []
        for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"):
            data = blobs.pop(name)
            sessions.append(rt.InferenceSession(data, so, providers=["CPUExecutionProvider"]))
            del data
            _release(sessions[-1])
        self._enc, self._dec, self._join = sessions
        self.tokens = read_tokens(blobs.pop("tokens.txt"))
        meta = self._enc.get_modelmeta().custom_metadata_map
        self.window = int(meta["window_size"])
        self.shift = int(meta["chunk_shift"])
        self.subsampling = int(meta["subsampling_factor"])
        self.blank = int(meta["vocab_size"])
        self.feat_dim = int(meta["feat_dim"])
        self.pred_hidden = int(meta["pred_hidden"])
        self.pred_layers = int(meta["pred_rnn_layers"])
        self.cache_shapes = (
            (1, int(meta["cache_last_channel_dim1"]), int(meta["cache_last_channel_dim2"]),
             int(meta["cache_last_channel_dim3"])),
            (1, int(meta["cache_last_time_dim1"]), int(meta["cache_last_time_dim2"]),
             int(meta["cache_last_time_dim3"])))
        self._dec_names = [i.name for i in self._dec.get_inputs()]

    # -- the three networks (Stream calls these) -----------------------------------------

    def encode(self, feats, cache_ch, cache_t, cache_len):
        return self._enc.run(None, {
            "audio_signal": np.ascontiguousarray(feats.T[None, :, :]),
            "length": np.array([feats.shape[0]], dtype=np.int64),
            "cache_last_channel": cache_ch, "cache_last_time": cache_t,
            "cache_last_channel_len": cache_len})

    def predict(self, token, state):
        out = self._dec.run(None, {
            self._dec_names[0]: np.array([[token]], dtype=np.int32),
            self._dec_names[1]: np.array([1], dtype=np.int32),
            self._dec_names[2]: state[0], self._dec_names[3]: state[1]})
        return out[0], (out[2], out[3])

    def join(self, enc_frame, dec_out):
        logits = self._join.run(None, {"encoder_outputs": enc_frame,
                                       "decoder_outputs": dec_out})[0]
        return int(np.argmax(logits.reshape(-1)))

    def stream(self):
        return Stream(self)


def _release(session):
    """ONNX Runtime's InferenceSession keeps the model bytes it was built from
    (onnxruntime 1.30, _model_bytes), only to build the session again on
    another execution provider after an error. This engine runs on the
    processor alone, so they are let go. A newer ONNX Runtime without the
    attribute changes nothing here (tests/test_instant.py says so)."""
    if getattr(session, "_model_bytes", None) is not None:
        session._model_bytes = None
