"""Launch a guarded local llama.cpp server for Helios."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    gpu_layers: int
    ctx_size: int
    batch_size: int
    threads: int
    minimum_available_mib: int


PROFILES = {
    "cpu": Profile(0, 512, 64, 2, 900),
    # Full offload avoids the severe graph-splitting penalty measured with 10 layers.
    # Small context and batches limit transient allocations on a 4 GB Jetson Nano.
    "gpu": Profile(99, 256, 8, 2, 1800),
}
CONFLICT_MARKERS = ("ollama runner", "open_webui", "Jetson-AI-Voice-Assistant")
LOOPBACK_HOST = "127.0.0.1"


def available_memory_mib(meminfo: Path = Path("/proc/meminfo")) -> int:
    """Return MemAvailable in MiB, or zero when Linux does not expose it."""

    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def competing_processes(proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    """Return heavyweight processes known to contend with local LLM inference."""

    matches: set[str] = set()
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except (OSError, UnicodeError):
            continue
        if command and any(marker in command for marker in CONFLICT_MARKERS):
            matches.add(command)
    return tuple(sorted(matches))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True, help="Path to llama-server")
    parser.add_argument("--model", type=Path, required=True, help="Path to a GGUF model")
    parser.add_argument("--cuda-lib", type=Path, help="Directory containing CUDA libraries")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="gpu")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--ctx-size", type=int, help="Override the profile context size")
    parser.add_argument("--batch-size", type=int, help="Override profile batch and ubatch sizes")
    parser.add_argument("--threads", type=int, help="Override profile thread count")
    parser.add_argument("--gpu-layers", type=int, help="Override layers to offload")
    parser.add_argument("--min-available-mib", type=int, help="Override the RAM safety threshold")
    parser.add_argument("--force", action="store_true", help="Run despite failed RAM preflight")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and print without starting"
    )
    return parser.parse_args()


def positive(value: int, name: str) -> int:
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


def main() -> None:
    args = parse_args()
    profile = PROFILES[args.profile]
    binary = args.binary.expanduser().resolve()
    model = args.model.expanduser().resolve()
    if not binary.is_file():
        raise SystemExit(f"llama-server not found: {binary}")
    if not os.access(binary, os.X_OK):
        raise SystemExit(f"llama-server is not executable: {binary}")
    if not model.is_file() or model.stat().st_size == 0:
        raise SystemExit(f"non-empty GGUF model not found: {model}")

    ctx_size = positive(args.ctx_size or profile.ctx_size, "ctx-size")
    batch_size = positive(args.batch_size or profile.batch_size, "batch-size")
    threads = positive(args.threads or profile.threads, "threads")
    gpu_layers = profile.gpu_layers if args.gpu_layers is None else args.gpu_layers
    minimum_mib = args.min_available_mib or profile.minimum_available_mib
    if gpu_layers < 0:
        raise SystemExit("gpu-layers cannot be negative")

    available_mib = available_memory_mib()
    competitors = competing_processes()
    print(
        f"llama.cpp preflight: profile={args.profile}, available={available_mib} MiB, "
        f"required={minimum_mib} MiB",
        file=sys.stderr,
    )
    for command in competitors:
        print(f"llama.cpp preflight: competing process: {command}", file=sys.stderr)
    if available_mib < minimum_mib and not args.force:
        raise SystemExit(
            "insufficient available RAM; stop resident assistants, model runners, and web UI "
            "or use --force after reviewing the risk"
        )

    env = os.environ.copy()
    library_dirs = [str(binary.parent)]
    if args.cuda_lib:
        library_dirs.append(str(args.cuda_lib.expanduser().resolve()))
    if env.get("LD_LIBRARY_PATH"):
        library_dirs.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = os.pathsep.join(library_dirs)

    command = [
        str(binary),
        "--model",
        str(model),
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(args.port),
        "--ctx-size",
        str(ctx_size),
        "--batch-size",
        str(batch_size),
        "--ubatch-size",
        str(batch_size),
        "--threads",
        str(threads),
        "--threads-batch",
        str(threads),
        "--n-gpu-layers",
        str(gpu_layers),
        "--parallel",
        "1",
    ]
    print(f"llama.cpp command: {shlex.join(command)}", file=sys.stderr)
    if args.dry_run:
        return
    os.execve(binary, command, env)


if __name__ == "__main__":
    main()
