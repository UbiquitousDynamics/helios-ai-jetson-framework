# Installation


### Hardware

- NVIDIA Jetson or another machine capable of running the selected backends;
- microphone available to PyAudio, optionally selected through
  `HELIOS_AUDIO_INPUT_DEVICE`;
- speaker or audio device available to `sounddevice`, optionally selected
  through `HELIOS_AUDIO_OUTPUT_DEVICE`;
- ALSA output and `aplay` for notification cues on Linux;
- enough storage and memory for Vosk, Piper, SentenceTransformer, and Ollama
  models.

The exact production Jetson model, JetPack release, microphone, and audio-device
configuration are deployment-specific. **This could not be determined from the
current codebase.**

### Software

- Python 3.10 or newer;
- a virtual environment;
- PortAudio development/runtime support for PyAudio;
- a running Ollama service;
- the configured Ollama model tags;
- platform-compatible PyTorch and ONNX Runtime builds.

## Installation

Clone the repository and enter it:

```bash
git clone https://github.com/UbiquitousDynamics/helios-ai-jetson-framework.git
cd helios-ai-jetson-framework
```

### Desktop development

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

PowerShell activation:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Install the portable desktop dependencies:

```bash
python -m pip install -r requirements.txt
```

On Linux, PyAudio may require PortAudio headers supplied by the distribution.
Install `requirements-remote.txt` in addition when this deployment will use an
HTTP/SSE provider or Codex:

```bash
python -m pip install -r requirements-remote.txt
```

### NVIDIA Jetson

Provision PyTorch and ONNX Runtime for the exact JetPack/L4T image first. Do not
allow generic pip wheels to replace working NVIDIA/vendor backends.

Verify the platform installations:

```bash
python -c "import torch, onnxruntime; print(torch.__version__, onnxruntime.__version__)"
```

The known Helios target uses `piper-phonemize-fix`. Preserve that backend and
install Piper without transitive dependency resolution:

```bash
python -m pip install piper-phonemize-fix==1.2.1
python -m pip install --no-deps piper-tts==1.2.0
python -m pip install -r requirements-jetson.txt
python -c "import piper, torch, onnxruntime; print('Jetson backends import successfully')"
```

`requirements-jetson.txt` contains runtime dependencies only. To run the
model-free test and quality suite on the Jetson, install the separate developer
dependencies:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Use the repository launcher for validation and normal operation:

```bash
python3 scripts/run_jetson.py --doctor --runtime-only
python3 scripts/run_jetson.py
```

For systemd, `ExecStart` must invoke `scripts/run_jetson.py`, not `main.py`
directly; otherwise the Jetson OpenMP preload is bypassed. See
[`examples/helios.service.example`](examples/helios.service.example) and adapt
its user, project path, and audio-device selectors before installation.

The launcher deliberately starts `venv/bin/python3` or `.venv/bin/python3`
instead of relying on whichever `python3` is currently on `PATH`. On AArch64 it
prefers the OpenMP runtime bundled with scikit-learn, preserves any existing
`LD_PRELOAD` entries, and falls back to the system `libgomp` only when a private
copy is unavailable. This setup occurs before the runtime interpreter starts,
which is required to avoid Jetson static-TLS loader failures. Set
`HELIOS_PYTHON` to an explicit virtualenv interpreter when neither conventional
directory name is used. The launcher intentionally preserves the
`venv/bin/python3` symlink path: resolving it to the base interpreter would
bypass the virtual environment.

Revalidate these versions whenever JetPack changes. The repository does not
embed a third-party wheel URL because those URLs and ABI combinations are tied
to the target image.

## Ollama model setup

The default Italian profile expects `emilia-gemma3:1b`:

```bash
ollama create emilia-gemma3:1b -f api/Modelfile-IT
```

The English profile expects `emilia-en-gemma3:1b`:

```bash
ollama create emilia-en-gemma3:1b -f api/Modelfile-EN
```

The secondary `think()` API defaults to `qwen3:0.6b`:

```bash
ollama pull qwen3:0.6b
```

Both Emilia Modelfiles derive from
`hf.co/unsloth/gemma-3-1b-it-GGUF:Q4_K_M`, request very short answers, and use a
512-token context window. Creating the models may require network access the
first time Ollama retrieves the base model.

Check the installed tags:

```bash
ollama list
```

