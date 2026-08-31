# Vendored model: Smart Turn v3.2 (CPU)

File: `smart_turn_v3_2_cpu.onnx`
SHA-256: `2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f`

## Provenance

- Upstream model: [pipecat-ai/smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3) (Daily.co)
- License: BSD-2-Clause
- Extracted from the `pipecat-ai` PyPI package, version `0.0.108`
  (`pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx`), obtained
  via a standard `pip download pipecat-ai==0.0.108 --no-deps`.
- Architecture: Whisper-Tiny encoder + linear classifier head, ~8M
  parameters, int8-quantized CPU variant.

## Why this file is vendored directly instead of taken via the `pipecat-ai`
## package at runtime

See `app/livekit_agent/turn_detector.py`'s module docstring for the full
explanation: importing the `pipecat-ai` package globally monkeypatches
`asyncio.wait_for` on Python < 3.12 (this project's runtime), which would
alter timeout/cancellation behavior inside `livekit-agents`' own job-process
supervision code running in the same worker process. This project instead
loads this ONNX file directly via `onnxruntime` and reproduces the model's
documented preprocessing recipe (Whisper feature extraction, 8-second
window, sigmoid output) without depending on the `pipecat-ai` package.

## Usage

Loaded by `app/livekit_agent/turn_detector.py`'s `SmartTurnDetector`. Runs
CPU-only, offline, no network access. Input: 16kHz mono float32 PCM.
Output: a single sigmoid probability in `[0, 1]` (COMPLETE if `> 0.5`).
