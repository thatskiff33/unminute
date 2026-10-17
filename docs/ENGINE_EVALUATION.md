# Speech engine evaluation

Findings from comparing speech-to-text engines for Unminute on public audio, 15 to 16 October 2026 (issue #12). Only aggregate figures from public datasets are published here. No transcripts and no private recordings were used. Business recordings are still to be tested (#25).

All tests ran on one laptop: Intel Core Ultra 7 255H (16 logical processors, 32 GB of memory, Intel Arc 140T graphics, Intel AI Boost NPU, NVIDIA RTX 500 Ada 4 GB), Windows 11, on mains power.

## Summary

- **Most accurate: Qwen3-ASR-1.7B.** On eight whole meetings it had a word error rate of 19.4%, against 24.0% for Parakeet v2 (the default) and 23.9% for Whisper large-v3 turbo. It was the most accurate on every one of the eight meetings.
- **Fastest: Parakeet v2.** 20 times faster than real time (about 3 minutes per hour of audio), against about 3 times for Qwen3-ASR and turbo (about 20 minutes per hour).
- **Qwen3-ASR needs much more memory.** Its peak was 11.2 GB per meeting, against 2.2 GB for turbo. That rules it out on 8 GB laptops and makes it tight on 16 GB.
- **The NPU is not usable** on this laptop for any engine tested (details below).
- **Decision so far:** Parakeet v2 stays the default. Qwen3-ASR is a candidate for an optional "most accurate" mode on well-equipped laptops, subject to real-recording tests (#25) and a lower-memory word-timing aligner.

## Whole meetings (main result)

**Data.** Eight meetings from the AMI Meeting Corpus test set (EN2002a, EN2002c, ES2004a, ES2004c, IS1009a, IS1009c, TS3003a, TS3003c): headset mix audio, 254 minutes in all, 14 to 50 minutes each. The reference is the official manual transcript (AMI manual annotations 1.6.2): every speaker's words merged in time order, 39,100 words after normalisation. The AMI corpus is released under CC BY 4.0 by the University of Edinburgh and partners.

**Method.**
- Every engine ran through Unminute's own processing path: 45-second chunks cut at quiet points, 2 seconds of context each side, and each word kept by the chunk that owns its start time (`workspace/chunking.py`).
- All engines used the Faster setting (14 threads) on the processor, with the app code pinned at commit 5b7132d.
- Parakeet v2 and turbo are the app's own engines (INT8). Qwen3-ASR-1.7B ran as INT8 on OpenVINO 2026.4, with Qwen3-ForcedAligner-0.6B (PyTorch) providing word timings.
- Text was normalised identically on both sides by `tools/compare_engines.py`.
- Each engine started only after total processor use stayed below 10% for 60 seconds. Processor use, free memory and any other busy process were logged every 15 seconds.

**Results.**

| Engine | Word error rate | Substitutions | Deletions | Insertions | Speed (x real time) | Peak memory | Word start within 0.25 s / 0.5 s | Words placed in silence, per hour |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Qwen3-ASR-1.7B + aligner | **19.4%** | 5.2% | 12.8% | 1.4% | 3.0 | 11.2 GB | 94% / 97% | 29 |
| Whisper large-v3 turbo | 23.9% | 4.7% | 17.9% | 1.2% | 3.1 | 2.2 GB | 84% / 92% | 14 |
| Parakeet v2 (default) | 24.0% | 6.2% | 16.2% | 1.7% | **20.4** | not recorded in this run | **98% / 99%** | 13 |

Word error rate per meeting:

| Engine | EN2002a | EN2002c | ES2004a | ES2004c | IS1009a | IS1009c | TS3003a | TS3003c |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Qwen3-ASR-1.7B | 28.7% | 24.0% | 17.4% | 14.1% | 18.1% | 10.8% | 14.9% | 12.9% |
| Whisper turbo | 36.6% | 29.6% | 20.6% | 18.2% | 20.6% | 12.3% | 17.4% | 15.0% |
| Parakeet v2 | 34.6% | 29.1% | 21.7% | 18.6% | 23.1% | 13.7% | 19.8% | 15.6% |

**Reading the results.**
- The absolute error rates are high because AMI meetings have frequent crosstalk. The reference contains every speaker's words, and a single transcript cannot hold overlapping speech, so much of every engine's deletion rate is overlap. Compare the engines with each other.
- Word timing is measured on correctly recognised words against the AMI word times, which AMI produced by aligning its human transcript to the audio. Median timing error: Qwen3-ASR 0.03 s, turbo 0.05 s, Parakeet 0.06 s. None drifted over the course of a meeting (the median was the same in each quarter).
- "Words placed in silence" counts recognised words more than 1 second from any reference word, a rough proxy for invented text. Qwen3-ASR's rate is about twice the others' and should be checked by ear on real recordings.
- Qwen3-ASR's memory peak was 10.8 to 11.2 GB on every meeting regardless of length, so it is a fixed cost, not growth. A likely large share is the word-timing aligner running unquantised on PyTorch; this was not measured separately. An OpenVINO version of the aligner was merged upstream on 7 October 2026 but is not yet in a release (the latest at the time was 2026.4.1, 1 October).
- Parakeet's peak memory was not recorded in this run. An earlier short-clip run (`tools/compare_engines.py`, 13 October 2026, #12) measured 1.2 GB. That run, like the app's Parakeet worker at the time, loaded faster-whisper's decoder and with it CTranslate2, which the worker has not loaded since R12a (#182), so the figure is likely higher than the worker's today. Since #187 the tool decodes clips as the app does, in its contained decoder process: stereo clips reach the engine about 3 dB louder than in earlier runs, other clips differ only by 16-bit rounding, and a damaged clip stops the run instead of being cut short. Re-measure memory and word error rates before comparing them with the figures here.
- Background Windows services (Defender, the Intel Driver & Support Assistant) briefly used up to about a quarter of the processor's total capacity. Another process used 5% or more in 12% of the 15-second samples during the Qwen3-ASR run and 3% during turbo. That may have slowed Qwen3-ASR slightly. It cannot change any transcript.

## Short clips (first screen)

148 public clips (21.5 minutes, 3,457 reference words, from AMI, Earnings-22 and EdAcc), scored with `tools/compare_engines.py`:

| Engine | Device | Word error rate | Speed (x real time) |
| --- | --- | --- | --- |
| Qwen3-ASR-1.7B | Processor or Intel Arc graphics | 12.6% | 3.2 (processor), 4.5 (Arc) |
| Qwen3-ASR-0.6B | Processor | 14.5% | 7.9 |
| Whisper large-v3 (OpenVINO) | Intel Arc graphics | 14.7% | 5.4 |
| Whisper large-v3 turbo (app engine) | Processor | 14.8% | not comparable (noisy earlier run) |
| Whisper large-v3 turbo (OpenVINO) | Intel Arc graphics | 14.9% | 13.0 |
| Parakeet v2 (app engine) | Processor | 15.9% | not comparable (noisy earlier run) |

Whisper large-v3 was no more accurate than turbo and needed about twice the memory, so it was dropped. The ranking on short clips matched the whole-meeting ranking.

## NPU (Intel AI Boost)

This laptop's NPU is architecture 3720 (driver 32.0.100.4778), tested with OpenVINO 2026.4:

- **Whisper large-v3:** compiles in about 7 minutes, then fails on the first transcription. Intel documents this failure for this NPU generation.
- **Whisper large-v3 turbo:** runs, with the same accuracy as on the Arc graphics, but the NPU hung and reset when word timings were requested, and hung again partway through a run without them. Unminute needs word timings to assign speakers.
- **Qwen3-ASR:** does not compile for the NPU, which needs fixed input sizes.
- **Parakeet v2:** the realistic route would be Windows ML's OpenVINO execution provider (`onnxruntime-windowsml` 1.30.0 matches the app's ONNX Runtime); not trialled. The standalone `onnxruntime-openvino` package (1.24.1) is a version the speech library excludes.

Conclusion: no usable NPU path on this hardware. The Arc graphics ran OpenVINO models reliably and could be an option for laptops without an NVIDIA card.

## Limits

- Public datasets only. Some models may have been trained on AMI; this was not checked. Treat these results as a filter, not proof of performance on New Zealand business meetings.
- One laptop, one run per engine.
- The table-microphone (far-field) AMI audio was not tested, because its download source was unavailable during the run.
- Qwen3-ASR's licence and packaging (OpenVINO GenAI plus a PyTorch aligner) have not been reviewed for shipping.

## Next steps

1. Run the real-recording comparison (#25) with Parakeet v2, Qwen3-ASR-1.7B and turbo.
2. Try a lower-memory word-timing aligner (the upcoming OpenVINO aligner, or a quantised PyTorch one) and re-measure memory.
3. Review Qwen3-ASR's licence and model files before any packaging work.
