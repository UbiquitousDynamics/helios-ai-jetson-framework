from pathlib import Path

from scripts.run_llama_cpp import available_memory_mib, competing_processes


def test_available_memory_mib_reads_linux_meminfo(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 4096000 kB\nMemAvailable: 2097152 kB\n", encoding="utf-8")

    assert available_memory_mib(meminfo) == 2048


def test_competing_processes_finds_known_memory_consumers(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "cmdline").write_bytes(b"ollama\0runner\0--model\0test")
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "cmdline").write_bytes(b"python\0worker.py")
    (tmp_path / "self").mkdir()

    assert competing_processes(tmp_path) == ("ollama runner --model test",)
