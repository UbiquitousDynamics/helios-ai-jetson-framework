# Knowledge base and assets


The active knowledge base consists of every top-level UTF-8 `.txt` file in
`uploads/`, sorted deterministically:

- `qa_pairs.txt`;
- `regolamento.txt`;
- `team_notice.txt`.

The system does not recurse into subdirectories and does not ingest PDFs.
Convert other formats to reviewed UTF-8 text before adding them.

### Index lifecycle

`embeddings.npz` is generated output and is not tracked by Git. Build it
explicitly before production deployment:

```bash
python scripts/build_index.py
```

Available overrides:

```bash
python scripts/build_index.py \
  --corpus uploads \
  --model models/all-MiniLM-L6-v2 \
  --output embeddings.npz \
  --batch-size 16 \
  --device cpu
```

If the index is missing, the first RAG query builds it automatically.
Prebuilding is recommended on constrained devices because loading the model and
encoding all chunks adds first-use latency.

### Integrity manifest

Every generated index stores both `embeddings` and a JSON manifest. Validation
binds the matrix to:

- schema version;
- splitter version;
- ordered source filenames, ordinals, and chunk text;
- corpus SHA-256;
- content-derived embedding-model identity;
- row count and vector dimension;
- NumPy dtype;
- the normalized-vector contract;
- embedding-matrix SHA-256.

The runtime rejects:

- old NPZ files without a manifest;
- a different number of rows and corpus chunks;
- content changes even when row counts remain equal;
- embedding-model content changes while allowing the repository to be relocated;
- incompatible dimensions or dtypes;
- NaN, infinite, zero, or non-unit vectors;
- a corrupted matrix checksum.

Index writes use a temporary file in the destination directory, flush and
`fsync` it, and atomically replace the target. A failed build cannot silently
leave a half-written canonical index.

### Retrieval API

```python
from document.rag_system import RagSystem

rag = RagSystem()
passages = rag.retrieve("How much water is required?", top_k=4)

for passage in passages:
    print(passage.source, passage.score, passage.text)
```

For compatibility:

```python
text = rag.run("How much water is required?", top_k=4)
```

`run()` returns a semicolon-joined string.

## Asset validation and provenance

[`assets-manifest.json`](assets-manifest.json) inventories the bundled
SentenceTransformer, Piper voices, Vosk models, corpus, cues, images, and
generated RAG index. It records:

- required or optional status;
- role;
- companion files;
- upstream information when known;
- licensing status;
- representative SHA-256 checksums.

Validate the checkout without loading a neural model or opening audio devices:

```bash
python scripts/doctor.py --assets-only --check-hashes
```

Validate installed runtime imports as well:

```bash
python scripts/doctor.py
```

On Jetson, run the same check through the launcher:

```bash
python3 scripts/run_jetson.py --doctor --runtime-only
```

In addition to checking that packages are installed, the runtime doctor imports
the native Piper, audio, Torch, scikit-learn, and SentenceTransformer chain in
an isolated subprocess. It therefore detects loader failures that a package
presence check cannot see, including `cannot allocate memory in static TLS
block`.

Missing generated `embeddings.npz` is an expected warning before the first
build. Missing provenance or license metadata is also reported as a warning;
hash mismatches and absent required assets are errors.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before redistribution.

