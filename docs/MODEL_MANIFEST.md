# Model integrity manifest

Generated from the manifests in `asr_engines.py` by
`python asr_engines.py manifest --out docs/MODEL_MANIFEST.md`;
a unit test fails if this file and the code disagree.
Every file must match its size and SHA-256 before a model counts as
installed. Large-file hashes match Hugging Face's published LFS SHA-256;
small files were checked against Hugging Face's git blob IDs.

| Model | Licence | Source | Repository | Revision |
| --- | --- | --- | --- | --- |
| parakeet-tdt-0.6b-v2 | CC BY 4.0 | NVIDIA parakeet-tdt-0.6b-v2, ONNX export by istupakov | `istupakov/parakeet-tdt-0.6b-v2-onnx` | `0bbb45a3365852604aef28b538a8f066f4ccaa85` |
| parakeet-tdt-0.6b-v3 | CC BY 4.0 | NVIDIA parakeet-tdt-0.6b-v3, ONNX export by istupakov | `istupakov/parakeet-tdt-0.6b-v3-onnx` | `8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce` |
| silero-vad | MIT | Silero VAD, ONNX packaging by istupakov | `istupakov/silero-vad-onnx` | `b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6` |
| large-v3-turbo | MIT | OpenAI whisper-large-v3-turbo, CTranslate2 conversion | `dropbox-dash/faster-whisper-large-v3-turbo` | `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` |
| large-v3 | MIT | OpenAI whisper-large-v3, CTranslate2 conversion by Systran | `Systran/faster-whisper-large-v3` | `edaa852ec7e145841d8ffdb056a99866b5f0a478` |
| speaker-diarization-community-1 | CC BY 4.0 (gated) | pyannoteAI speaker-diarization-community-1; accept terms to download | `pyannote/speaker-diarization-community-1` | `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee` |

## parakeet-tdt-0.6b-v2

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.json` | 97 | `666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466` |
| `decoder_joint-model.int8.onnx` | 8998286 | `a449f49acd68979d418651dd2dcb737cc0f1bf0225e009e29ee326354edbf7d3` |
| `encoder-model.int8.onnx` | 652184014 | `3e0581fda6ab843888b51e56d7ee78b6d5bc3237ec113af1f732d1d5286aa155` |
| `vocab.txt` | 9384 | `ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d` |

## parakeet-tdt-0.6b-v3

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.json` | 97 | `666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466` |
| `decoder_joint-model.int8.onnx` | 18202004 | `eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70` |
| `encoder-model.int8.onnx` | 652183999 | `6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09` |
| `vocab.txt` | 93939 | `d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d` |

## silero-vad

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.json` | 30 | `1094039d370c82889582ba739a3d1caac5754c8b3a17a66a534200c9f72086e2` |
| `silero_vad.onnx` | 2327524 | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` |

## large-v3-turbo

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.json` | 2263 | `b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e` |
| `model.bin` | 1617884929 | `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da` |
| `tokenizer.json` | 2710337 | `297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd` |
| `preprocessor_config.json` | 340 | `7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711` |
| `vocabulary.json` | 1068114 | `c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1` |

## large-v3

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.json` | 2394 | `a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9` |
| `model.bin` | 3087284237 | `69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1` |
| `tokenizer.json` | 2480617 | `6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca` |
| `preprocessor_config.json` | 340 | `7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711` |
| `vocabulary.json` | 1068114 | `c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1` |

## speaker-diarization-community-1

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `config.yaml` | 444 | `5ce2bfa9a938dc132cec1172592d65173cbb8f444ea1e4133f10f9391de155be` |
| `embedding/pytorch_model.bin` | 26646242 | `6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929` |
| `plda/plda.npz` | 133852 | `9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255` |
| `plda/xvec_transform.npz` | 134376 | `325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f` |
| `segmentation/pytorch_model.bin` | 5906507 | `7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e` |
