# GPU offload on Jetson Nano

Helios uses `llama-server` as a separate local process. This keeps CUDA and
`llama.cpp` native libraries outside the assistant process and lets the router
use the same normalized streaming contract as the other providers. The server
only listens on loopback and needs no API key.

## Supported deployment

The measured device is a Jetson Nano 4 GB running L4T R32.7.1, CUDA 10.2 and
the 5 W power mode. The validated CUDA binary is `llama.cpp` b5050, built for
compute capability 5.3. New upstream releases generally require a newer CUDA
toolchain and cannot be assumed to build on JetPack 4.

The tested binary comes from the Jetson Nano compatibility fork
[`kreier/llama.cpp-jetson.nano`](https://github.com/kreier/llama.cpp-jetson.nano)
at commit `9caada5205f104c2b507b0b85c135f7f4fdf8e55`. Its b5050 source snapshot is
based on upstream commit `23106f94`. Use that pinned ARM64 checkout on this
JetPack generation; do not copy binaries from another architecture.

## Provision another Jetson Nano

These instructions reproduce the validated deployment on another 4 GB Nano.
The `gpu_offloading` branch must exist on the remote before starting. Run every
command on the Jetson unless stated otherwise.

### 1. Verify the target

```bash
uname -m
head -n 1 /etc/nv_tegra_release
nvcc --version
sudo nvpmodel -q
free -h
```

The validated combination is `aarch64`, L4T R32.7.1, CUDA 10.2 and 5 W mode.
Do not assume that the pinned llama.cpp binary works on another JetPack release.
Keep at least 3 GB of free storage for the checkout, Python environment, model
and build artifacts.

Helios requires Python 3.10 or newer. JetPack 4 normally leaves Python 3.6 at
`/usr/bin/python3`, so install a separate compatible interpreter and verify it:

```bash
/usr/local/bin/python3.10 --version
```

The NVIDIA PyTorch and ONNX Runtime packages must match the target JetPack and
that Python ABI. Follow [the Jetson installation contract](INSTALLATION.md#nvidia-jetson)
instead of replacing them with generic wheels.

### 2. Check out this branch

```bash
cd "$HOME"
git clone --branch gpu_offloading --single-branch \
  https://github.com/UbiquitousDynamics/helios-ai-jetson-framework.git
cd helios-ai-jetson-framework
git status --short --branch
```

The last command should show `gpu_offloading` with a clean working tree. For a
private repository, authenticate with an account that has read access.

Create the environment with the compatible interpreter:

```bash
/usr/local/bin/python3.10 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install piper-phonemize-fix==1.2.1
./venv/bin/python -m pip install --no-deps piper-tts==1.2.0
./venv/bin/python -m pip install -r requirements-jetson.txt
```

Preserve working NVIDIA PyTorch and ONNX Runtime installations while doing
this. Confirm the complete runtime before continuing:

```bash
./venv/bin/python -c \
  "import piper, torch, onnxruntime; print(torch.__version__, onnxruntime.__version__)"
./venv/bin/python scripts/doctor.py --runtime-only
```

### 3. Install the pinned llama.cpp build

The compatibility repository contains the b5050 ARM64 binaries and their
shared libraries. Pin its tested commit:

```bash
cd "$HOME"
git clone https://github.com/kreier/llama.cpp-jetson.nano.git llama-b5050
cd llama-b5050
git checkout --detach 9caada5205f104c2b507b0b85c135f7f4fdf8e55
chmod +x bin/llama-server bin/llama-cli bin/llama-bench
LD_LIBRARY_PATH="$PWD/bin:/usr/local/cuda/lib64" ./bin/llama-server --version
```

The final command must start without a missing-library or CUDA loader error.

### 4. Download and verify the model

The measured model is Gemma 3 1B Instruct Q4_K_M. Its license is Gemma; review
and accept the model terms before use.

```bash
mkdir -p "$HOME/models"
curl --fail --location \
  --output "$HOME/models/gemma3-1b-it-Q4_K_M.gguf" \
  https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/resolve/f9c28bcd85737ffc5aef028638d3341d49869c27/gemma-3-1b-it-Q4_K_M.gguf
printf '%s  %s\n' \
  8ccc5cd1f1b3602548715ae25a66ed73fd5dc68a210412eea643eb20eb75a135 \
  "$HOME/models/gemma3-1b-it-Q4_K_M.gguf" | sha256sum --check
```

Do not proceed if the checksum fails.

### 5. Free memory and start llama-server

Gemma 3 1B Q4_K_M uses approximately 763 MiB of CUDA memory and a
306 MiB CPU-mapped model buffer, plus its cache, compute buffers and server.
The launcher requires 1.8 GiB of available RAM and reports known competing
processes. Stop other model runners before loading it:

```bash
sudo systemctl disable --now ollama.service 2>/dev/null || true
sudo systemctl disable --now Jetson-AI-Voice-Assistant.service 2>/dev/null || true
docker update --restart=no open-webui 2>/dev/null || true
docker stop open-webui 2>/dev/null || true
free -h
```

From the Helios checkout, inspect the generated command and then start it:

```bash
cd "$HOME/helios-ai-jetson-framework"
./venv/bin/python scripts/run_llama_cpp.py \
  --binary "$HOME/llama-b5050/bin/llama-server" \
  --model "$HOME/models/gemma3-1b-it-Q4_K_M.gguf" \
  --cuda-lib /usr/local/cuda/lib64 \
  --profile gpu \
  --dry-run

./venv/bin/python scripts/run_llama_cpp.py \
  --binary "$HOME/llama-b5050/bin/llama-server" \
  --model "$HOME/models/gemma3-1b-it-Q4_K_M.gguf" \
  --cuda-lib /usr/local/cuda/lib64 \
  --profile gpu
```

Leave this terminal open. The first model load can take several minutes.
The `gpu` profile uses full offload, context 256 and batch 8. Partial offload is
not recommended: a measured 10-layer run was substantially slower than CPU.
The memory guard cannot protect against USB power transients, so keep the board
in 5 W mode and do not enable `jetson_clocks` while it is powered by the PC.

### 6. Validate text inference

In a second SSH session:

```bash
cd "$HOME/helios-ai-jetson-framework"
curl --fail http://127.0.0.1:8081/health
./venv/bin/python scripts/benchmark_llama_cpp.py
HELIOS_LLM_CONFIG="$PWD/examples/llm-routing.llama-cpp.toml" \
  ./venv/bin/python scripts/text_chat.py "Quanto fa due più due?"
```

The health request must succeed, the benchmark must return JSON containing a
non-empty answer, and text chat must print the Helios response. Run
`tegrastats` in another terminal to confirm GPU activity and watch temperature,
RAM and swap.

### 7. Validate the complete assistant

Before enabling boot services, check the bundled speech assets and audio
devices. Configure `HELIOS_AUDIO_INPUT_DEVICE` and
`HELIOS_AUDIO_OUTPUT_DEVICE` when the defaults are wrong.

```bash
cd "$HOME/helios-ai-jetson-framework"
./venv/bin/python scripts/doctor.py
HELIOS_LLM_CONFIG="$PWD/examples/llm-routing.llama-cpp.toml" \
  ./venv/bin/python scripts/run_jetson.py
```

Speak only after this foreground test starts successfully. Stop it with
`Ctrl-C`, then stop the foreground llama-server before installing the services.

### Measured reference

With the validated board powered only over USB, isolated CPU/GPU comparisons
used the same prompt, quantization, context 256, batch 8 and output limit.
Results are single diagnostic samples after a fresh server start:

| Model | Backend | Total | Prompt | Decode |
| --- | --- | ---: | ---: | ---: |
| Qwen 2.5 0.5B Q4_K_M | CPU | 6.26 s | 4.18 token/s | 4.08 token/s |
| Qwen 2.5 0.5B Q4_K_M | full GPU | 1.32 s | 22.79 token/s | 10.70 token/s |
| Gemma 3 1B Q4_K_M | CPU | 12.47 s | 2.15 token/s | 1.81 token/s |
| Gemma 3 1B Q4_K_M | full GPU | 2.86 s | 11.71 token/s | 4.42 token/s |

For Qwen, full offload reduced latency by 79%, accelerated prompt processing
5.45 times and decode 2.62 times. For Gemma, it reduced latency by 77%, with a
5.45-times prompt and 2.45-times decode acceleration. Gemma full offload loaded
stably once the old assistant, Ollama and Open WebUI were stopped. Swap remained
at approximately 121 MiB during the isolated tests.

An earlier Qwen test with all services resident also completed, but reached
1.4 GiB of swap and only 7.09 token/s decode. Avoid concurrent model services
for sustained use.

Record cold loading separately from warm requests. During the test, capture
`tegrastats`, free memory, swap, temperature, server timings and whether the
board disconnects or reboots. Do not compare backends with different prompts,
context sizes, quantizations or output lengths.

### 8. Run at boot with systemd

The repository provides two deployment templates:

- `examples/helios-llama.service.example` starts Gemma 3 1B with full CUDA
  offload and conflicts with the system Ollama service;
- `examples/helios-gpu.service.example` waits for the local health endpoint and
  then starts the voice assistant through the required Jetson launcher.

Review paths, user, audio devices and model before installing them. The commands
below generate device-specific units for the current account, assuming the
checkout and llama.cpp directories created above:

```bash
cd "$HOME/helios-ai-jetson-framework"
sed -e "s|operator|$(id -un)|g" \
    -e "s|Group=$(id -un)|Group=$(id -gn)|g" \
    examples/helios-llama.service.example > /tmp/helios-llama.service
sed -e "s|operator|$(id -un)|g" \
    -e "s|Group=$(id -un)|Group=$(id -gn)|g" \
    -e "s|/run/user/1000|/run/user/$(id -u)|g" \
    examples/helios-gpu.service.example > /tmp/helios-gpu.service

sudo install -m 0644 /tmp/helios-llama.service /etc/systemd/system/
sudo install -m 0644 /tmp/helios-gpu.service /etc/systemd/system/
sudo systemctl disable --now Jetson-AI-Voice-Assistant.service ollama.service
docker update --restart=no open-webui  # only if this container exists
docker stop open-webui                 # only if this container exists
sudo systemctl daemon-reload
sudo systemctl enable --now helios-llama.service helios-gpu.service
```

The service invokes the project virtual environment directly. It must use
Python 3.10 or newer; JetPack's `/usr/bin/python3` may still point to Python
3.6 and must not be used for Helios.

Check startup and follow logs with:

```bash
systemctl status helios-llama.service helios-gpu.service
journalctl -u helios-llama.service -u helios-gpu.service -f
```

A successful deployment has both units in `active (running)`, returns HTTP 200
from `http://127.0.0.1:8081/health`, and shows CUDA activity in `tegrastats`
during a prompt. Reboot once and repeat these checks before considering the
second board provisioned.

To update the branch later:

```bash
cd "$HOME/helios-ai-jetson-framework"
sudo systemctl stop helios-gpu.service helios-llama.service
git pull --ff-only origin gpu_offloading
./venv/bin/python scripts/doctor.py --runtime-only
sudo systemctl start helios-llama.service helios-gpu.service
```

### Troubleshooting

| Symptom | Check |
| --- | --- |
| `insufficient available RAM` | Stop Ollama, Open WebUI and old assistants; confirm `free -h`. Do not use `--force` for normal deployment. |
| Missing `libggml` or `libllama` | Confirm the pinned repository still contains all libraries under `$HOME/llama-b5050/bin`. |
| CUDA initialization failure | Confirm L4T/CUDA versions, `/usr/local/cuda/lib64`, the `video` group and the pinned ARM64 binary. |
| Health check times out | Follow `journalctl -u helios-llama.service`; the first cold model load may take several minutes. |
| Service uses the wrong Python | Run `./venv/bin/python --version`; recreate `venv` with the compatible Python interpreter. |
| Text works but voice does not | Run `scripts/doctor.py`, inspect audio devices and set the two `HELIOS_AUDIO_*_DEVICE` variables in the assistant unit. |
| Jetson disconnects or reboots | Keep 5 W mode, stop concurrent workloads and use a dedicated power supply before further load testing. |

Rollback is explicit:

```bash
sudo systemctl disable --now helios-gpu.service helios-llama.service
sudo systemctl enable --now ollama.service Jetson-AI-Voice-Assistant.service
# Restore the previous Docker restart policy separately if it was changed.
```
