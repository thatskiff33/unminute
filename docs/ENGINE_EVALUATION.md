# Speech engine evaluation

Findings from comparing speech-to-text engines for Unminute on public audio, 15 to 16 October 2026 (issue #12). Only aggregate figures from public datasets are published here. No transcripts and no private recordings were used. Business recordings are still to be tested (#25).

All tests ran on one laptop: Intel Core Ultra 7 255H (16 logical processors, 32 GB of memory, Intel Arc 140T graphics, Intel AI Boost NPU, NVIDIA RTX 500 Ada 4 GB), Windows 11, on mains power.

## Summary

- **Most accurate: Qwen3-ASR-1.7B.** On eight whole meetings it had a word error rate of 19.4%, against 24.0% for Parakeet v2 (the default) and 23.9% for Whisper large-v3 turbo. It was the most accurate on every one of the eight meetings.
- **Parakeet v2 since #107: 21.2%.** Its speech detection now bridges pauses of up to 1 second, which cut its word error rate on the same eight meetings from 24.0% to 21.2% and used less processor time. Those eight meetings were also used to choose the setting, so 21.2% is slightly optimistic: on four meetings held out from that choice the gain was 1.4 points, not 2.9. See [Speech detection pauses](#speech-detection-pauses-107). The other figures on this page were measured before that change.
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
- Parakeet's peak memory was not recorded in this run. An earlier short-clip run (`tools/compare_engines.py`, 13 October 2026, #12) measured 1.2 GB. That run, like the app's Parakeet worker at the time, loaded faster-whisper's decoder and with it CTranslate2, which the worker has not loaded since R12a (#182), so the figure is likely higher than the worker's today. Since #187 the tool decodes clips as the app does, in its contained decoder process: mono clips differ only by 16-bit rounding and a damaged clip stops the run instead of being cut short. Since #220 the decoder mixes channels equally (their mean): stereo clips reach the engine at the same level as in the earlier runs, and clips with more channels keep every channel at an equal weight, where the earlier runs dropped a channel taken for a low-frequency one and weighted the others by speaker position. Re-measure memory and word error rates before comparing them with the figures here.
- Background Windows services (Defender, the Intel Driver & Support Assistant) briefly used up to about a quarter of the processor's total capacity. Another process used 5% or more in 12% of the 15-second samples during the Qwen3-ASR run and 3% during turbo. That may have slowed Qwen3-ASR slightly. It cannot change any transcript.

## Speech detection pauses (#107)

Benchmark of 19 October 2026 on public AMI meetings, reproduced by an independent reviewer. The full report and the review are in issue #107.

**Finding.** Parakeet hears each stretch of speech that the speech detector (Silero VAD, through onnx-asr) passes it, with no surrounding context. onnx-asr's default ends a stretch after 100 ms of silence. That cut the eight meetings into 5,324 pieces: median 1.4 s, 67% under 2 s. The full pass was losing words mainly through these short pieces, not through quiet speech the detector missed: a lower detection threshold gave no consistent gain, and only 0.7% of reference words lay wholly outside any piece.

**Change.** Since #107 a piece ends only after 1 second of silence (`VAD_MIN_SILENCE_MS` in `asr_engines.py`, passed as onnx-asr's `min_silence_duration_ms`). Every other speech-detection setting is onnx-asr's default, and no piece is longer than 20 s (`MAX_SEGMENT_SECONDS`), as before. Whisper uses faster-whisper's own speech detection and is not affected.

**Method.**
- Twelve AMI meetings, headset mix, scored as in the whole-meeting test above. The eight above (39,100 reference words) were used to choose the setting. Four more (EN2002b, ES2004b, IS1009b, TS3003b; 140 minutes, 22,386 words) were held out and scored only for the settings already chosen.
- The app's processing path at commit 7d5061f (45 s chunks, chunk ownership), Parakeet v2 INT8 in Faster mode (14 threads). Only the speech-detection arguments changed between runs.
- Settings tried: 300, 600, 1,000 and 2,000 ms of silence to end a piece; 300 ms of padding; the #103 live-trial settings; lower detection thresholds; and no speech detection at all.
- Noise floor: each main setting was run again with the input scaled by 0.99. The largest change on any one meeting was 0.82 points, and pooled changes were within 0.1 point. Runs are deterministic, so this measures sensitivity to the input, not run-to-run variation.
- Intervals are 95% from a paired bootstrap over 2-minute blocks (10,000 resamples).

**Results.**

| Silence that ends a piece | Word error rate, 8 meetings | Substitutions | Deletions | Insertions | Word error rate, 4 held out | Pieces (8 meetings) | Words placed in silence | Processor time (8 meetings) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 100 ms (before) | 24.0% | 6.4% | 16.1% | 1.5% | 21.2% | 5,324 | 108 | 2,695 s |
| 600 ms | 21.6% | 5.1% | 15.0% | 1.5% | 19.8% | 2,254 | 98 | 2,204 s |
| **1,000 ms (adopted)** | **21.2%** | 4.9% | 14.8% | 1.5% | **19.8%** | 1,510 | 92 | 2,311 s |
| 2,000 ms | 21.2% | 4.8% | 14.9% | 1.6% | not run | 1,074 | 92 | 2,343 s |

- 1,000 ms against before: 2.9 points better on the eight meetings (interval 2.2 to 3.5), and better on all eight; 1.4 points better on the four held out (interval 0.6 to 2.1), and better on all four.
- Insertions did not rise. Words placed in silence fell from 108 to 92 (held out: 60 to 58).
- Processor time fell by 14% in this run, but varied by meeting (7% to 21% on the three meetings the reviewer reran).
- Overlapped speech improved only a little: 43.9% of words spoken over another speaker were lost before, 41.5% after. One transcript cannot hold two speakers at once.
- Word timing: the share of word starts within 0.5 s of the reference is unchanged (99.2%); within 0.25 s it fell from 97.7% to 97.2% (held out: 97.7% to 96.9%).
- Chunk ownership (#14) is unchanged: the chunk plan and the rule that a word belongs to the chunk holding its start are not touched; the setting only changes how each chunk is split for the engine.

Per meeting, before and with 1,000 ms (word error rate %, then substitutions / deletions / insertions as word counts; speed is audio length over wall time, Faster mode):

| Meeting | Reference words | Before | 1,000 ms | Speed (x real time) |
| --- | --- | --- | --- | --- |
| EN2002a | 7,333 | 34.57 (542 / 1,925 / 68) | 31.09 (453 / 1,765 / 62) | 20.3 to 24.5 |
| EN2002c | 10,271 | 29.08 (801 / 2,028 / 158) | 25.58 (630 / 1,844 / 153) | 20.4 to 24.7 |
| ES2004a | 2,484 | 21.66 (137 / 384 / 17) | 17.39 (89 / 318 / 25) | 23.6 to 28.6 |
| ES2004c | 6,689 | 18.63 (311 / 840 / 95) | 16.74 (219 / 795 / 106) | 20.7 to 24.9 |
| IS1009a | 1,864 | 23.12 (97 / 285 / 49) | 22.37 (78 / 284 / 55) | 23.8 to 29.7 |
| IS1009c | 4,029 | 13.70 (167 / 307 / 78) | 12.46 (157 / 276 / 69) | 21.8 to 25.4 |
| TS3003a | 2,370 | 19.83 (222 / 228 / 20) | 16.41 (144 / 225 / 20) | 25.4 to 32.0 |
| TS3003c | 4,060 | 15.59 (231 / 286 / 116) | 12.51 (150 / 273 / 85) | 23.8 to 28.7 |

Insertions rose on three meetings (ES2004a, ES2004c and IS1009a, by 8, 11 and 6 words), fell on four and were unchanged on one; in total they fell from 601 to 575.

**Checks before adopting (19 October 2026).** Run one at a time on the same laptop, on the code at 7d5061f with and without the change. The decoder change merged since (#220) does not affect these files, which are mono.
- `tools/compare_engines.py` on the eight meetings. This tool gives the engine each whole meeting at once rather than in the app's 45 s chunks, so it is a second, independent path. Word error rate went from 24.10% to 20.99% in both Quiet mode (4 threads, the app's default) and Faster mode (14 threads), and fell on all eight meetings in both. The transcripts were identical in the two modes. Wall time: Quiet 609 s to 528 s, Faster 656 s to 435 s. Peak memory of the tool's process (peak working set): Quiet 1.48 GB to 1.49 GB, Faster 1.49 GB to 1.51 GB.
- An end-to-end run of the app's processing path on ES2004a in Quiet mode, in a throwaway folder: import, the contained decoder, the chunk plan, transcription in the worker, speaker identification in the worker and the store's speaker attribution, with model files from the cache only. Both runs finished. Word error rate 21.66% to 17.39% (the same as the benchmark above). Speaker identification found the same speaker turns in both runs (5 labels; it does not depend on this setting). Sentences went from 372 (median 1.21 s) to 276 (median 1.82 s). With the best match of labels to AMI speakers, 87.85% of words in labelled sentences had the right speaker before and 87.56% after; counting every reference word, 78.62% were recognised and given the right speaker before and 83.01% after, because more words were recognised. Transcription took 59.4 s before and 40.1 s after; speaker identification 743.5 s and 686.5 s.

**Why 1 second, not 0.6.** On the eight meetings 1,000 ms beat 600 ms by 0.41 points (interval 0.08 to 0.75). But those are the meetings the value was chosen on, and on the four held out the two tie (1.37 and 1.39 points better than before). 600 ms was fixed in advance from the #103 live trial; 1,000 ms was the best of four values tried. So the evidence for 1,000 ms over 600 ms is weak. The reviewer recommended 600 ms; the owner chose 1,000 ms on 19 October 2026, knowing this.

**Costs.**
- Speaker labels. The app gives each sentence one speaker, and longer pieces make sentences that span two speakers more common. The reviewer measured the best a perfect speaker model could do (the share of reference words in a sentence whose main speaker said them): ES2004a 90.2% before, 90.3% at 600 ms, 89.2% at 1,000 ms; IS1009a 86.6%, 86.1%, 85.2%; TS3003b 95.7%, 94.5%, 93.7%. 600 ms costs about half as much.
- Longer pieces. In the reviewer's measurement, the share of audio in pieces of 15 s or more rose from 4.7% to 53% (33% at 600 ms). Parakeet occasionally drops a burst of clear speech within one piece (on TS3003b about 48 words in one minute), and a longer piece makes such a burst longer. Bursts happened about as often before.

**Not tested.** Far-field (table microphone) and telephone audio; business recordings with long silences or hold music (#25); Parakeet v3, which uses the same setting; the graphics-card path in `tools/gpu_trial.py`; the 148 short public clips below, which were not re-run; peak memory in the app's worker (only the benchmark tool's process was measured); the four held-out meetings in Quiet mode; and whether Parakeet was trained on AMI.

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
