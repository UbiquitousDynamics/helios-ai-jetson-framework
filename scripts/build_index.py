"""Build an integrity-checked RAG index from local text files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from document.rag_system import RagSystem  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        default=str(PROJECT_ROOT / "uploads"),
        help="Directory containing .txt files",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "embeddings.npz"),
        help="Output NPZ index",
    )
    parser.add_argument(
        "--model",
        default=str(PROJECT_ROOT / "models/all-MiniLM-L6-v2"),
        help="Local SentenceTransformer model directory",
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="cpu")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rag = RagSystem(
        txt_dir=args.corpus,
        emb_file=args.output,
        model_name=args.model,
        reindex=True,
        batch_size=args.batch_size,
        device=args.device,
    )
    embeddings = rag.index_database()
    manifest = rag.manifest
    assert manifest is not None
    print(
        f"Indexed {manifest.row_count} chunks at dimension {manifest.dimension} "
        f"into {Path(args.output).resolve()} (shape={embeddings.shape})"
    )
    print(f"Corpus SHA-256: {manifest.corpus_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
