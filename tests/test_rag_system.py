from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from document.rag_system import CorpusError, IndexIntegrityError, ModelLoadError, RagSystem


class FakeEncoder:
    def __init__(self, dimension: int = 3) -> None:
        self.dimension = dimension
        self.calls: list[tuple[str, ...]] = []

    def encode(self, sentences: list[str], **_: object) -> np.ndarray:
        self.calls.append(tuple(sentences))
        rows = []
        for text in sentences:
            seed = sum((index + 1) * ord(char) for index, char in enumerate(text))
            vector = np.asarray(
                [float((seed + offset * 17) % 101 + 1) for offset in range(self.dimension)],
                dtype=np.float32,
            )
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows)


def make_rag(
    tmp_path: Path,
    corpus_text: str,
    *,
    encoder: FakeEncoder | None = None,
    reindex: bool = True,
) -> RagSystem:
    corpus = tmp_path / "uploads"
    corpus.mkdir(exist_ok=True)
    (corpus / "knowledge.txt").write_text(corpus_text, encoding="utf-8")
    return RagSystem(
        txt_dir=str(corpus),
        emb_file=str(tmp_path / "embeddings.npz"),
        model_name="fake/model",
        reindex=reindex,
        encoder=encoder or FakeEncoder(),
        model_identity="fake/model@1",
        batch_size=2_000,
    )


def rewrite_index(path: Path, embeddings: np.ndarray, manifest: dict[str, object]) -> None:
    with path.open("wb") as handle:
        np.savez_compressed(
            handle,
            embeddings=embeddings,
            manifest=np.asarray(json.dumps(manifest, sort_keys=True, separators=(",", ":"))),
        )


def test_native_sentence_transformer_import_error_keeps_root_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    rag = object.__new__(RagSystem)
    rag.model_name = str(model)
    rag.device = "cpu"

    import builtins

    original_import = builtins.__import__

    def failing_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentence_transformers":
            raise ImportError("libgomp.so.1: cannot allocate memory in static TLS block")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    with pytest.raises(ModelLoadError, match="static TLS block"):
        rag._load_default_encoder()


def test_retrieve_caches_corpus_and_index_and_returns_provenance(tmp_path: Path) -> None:
    encoder = FakeEncoder()
    rag = make_rag(tmp_path, "Alpha sentence. Beta sentence.", encoder=encoder)

    first = rag.retrieve("alpha", top_k=2)
    index_path = tmp_path / "embeddings.npz"
    first_mtime = index_path.stat().st_mtime_ns
    (tmp_path / "uploads" / "knowledge.txt").write_text(
        "Changed after the in-memory snapshot.",
        encoding="utf-8",
    )
    second = rag.retrieve("beta", top_k=2)

    assert len(first) == len(second) == 2
    assert {result.source for result in first} == {"knowledge.txt"}
    assert index_path.stat().st_mtime_ns == first_mtime
    assert len(encoder.calls) == 3  # one corpus batch, then one encoding per query


def test_prepare_loads_existing_index_without_encoding_a_query(tmp_path: Path) -> None:
    make_rag(tmp_path, "Alpha sentence. Beta sentence.").index_database()
    encoder = FakeEncoder()
    fresh = make_rag(
        tmp_path,
        "Alpha sentence. Beta sentence.",
        encoder=encoder,
        reindex=False,
    )

    assert fresh.prepare() is True
    assert fresh.manifest is not None
    assert encoder.calls == []


def test_prepare_never_builds_a_missing_index(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Alpha sentence.", reindex=False)

    assert rag.prepare() is False
    assert rag.manifest is None
    assert not (tmp_path / "embeddings.npz").exists()


def test_legacy_index_without_manifest_is_rejected(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Only one sentence.", reindex=False)
    with (tmp_path / "embeddings.npz").open("wb") as handle:
        np.savez_compressed(handle, embeddings=np.ones((1, 3), dtype=np.float32))

    with pytest.raises(IndexIntegrityError, match="no integrity manifest"):
        rag.load_embedding_matrix()


def test_known_1115_chunk_1116_row_mismatch_is_rejected(tmp_path: Path) -> None:
    corpus_text = " ".join(f"Chunk {index}." for index in range(1_115))
    rag = make_rag(tmp_path, corpus_text)
    original = rag.index_database()
    index_path = tmp_path / "embeddings.npz"

    with np.load(index_path, allow_pickle=False) as archive:
        manifest = json.loads(str(archive["manifest"].item()))
    mismatched = np.vstack((original, original[-1]))
    manifest["row_count"] = 1_116
    rewrite_index(index_path, mismatched, manifest)

    fresh = make_rag(tmp_path, corpus_text, reindex=False)
    with pytest.raises(
        IndexIntegrityError,
        match=r"1116 embedding rows for 1115 corpus chunks",
    ):
        fresh.load_embedding_matrix()


def test_changed_corpus_is_rejected_even_when_row_count_matches(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Alpha. Beta.")
    rag.index_database()
    (tmp_path / "uploads" / "knowledge.txt").write_text("Gamma. Delta.", encoding="utf-8")

    fresh = make_rag(tmp_path, "Gamma. Delta.", reindex=False)
    with pytest.raises(IndexIntegrityError, match="corpus content/order"):
        fresh.load_embedding_matrix()


def test_nonfinite_index_is_rejected(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Alpha.")
    embeddings = rag.index_database()
    index_path = tmp_path / "embeddings.npz"
    with np.load(index_path, allow_pickle=False) as archive:
        manifest = json.loads(str(archive["manifest"].item()))
    embeddings[0, 0] = np.nan
    rewrite_index(index_path, embeddings, manifest)

    fresh = make_rag(tmp_path, "Alpha.", reindex=False)
    with pytest.raises(IndexIntegrityError, match="NaN or infinite"):
        fresh.load_embedding_matrix()


def test_top_k_boundaries_and_ties_are_stable(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Alpha. Beta. Gamma.", encoder=FakeEncoder(dimension=2))
    matrix = np.asarray(
        [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    rag.model.encode = lambda *_args, **_kwargs: np.asarray([[1.0, 0.0]], dtype=np.float32)  # type: ignore[method-assign]

    assert rag.search("query", matrix, top_k=3) == [
        (0, 1.0),
        (1, 1.0),
        (2, 0.0),
    ]
    for invalid in (0, -1, 4, True, 1.5):
        with pytest.raises(ValueError, match="top_k"):
            rag.search("query", matrix, top_k=invalid)  # type: ignore[arg-type]


def test_partial_top_k_keeps_lowest_rows_at_cutoff_tie(tmp_path: Path) -> None:
    rag = make_rag(tmp_path, "Alpha. Beta. Gamma. Delta. Epsilon.")
    matrix = np.asarray(
        [
            [1.0, 0.0],
            [0.8, 0.6],
            [0.8, 0.6],
            [0.8, 0.6],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    rag.model.encode = lambda *_args, **_kwargs: np.asarray(  # type: ignore[method-assign]
        [[1.0, 0.0]],
        dtype=np.float32,
    )

    assert rag.search("query", matrix, top_k=3) == [
        (0, 1.0),
        (1, pytest.approx(0.8)),
        (2, pytest.approx(0.8)),
    ]


def test_already_normalized_encoder_output_is_reused(tmp_path: Path) -> None:
    matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)

    class NormalizedEncoder:
        def encode(self, _sentences: list[str], **_: object) -> np.ndarray:
            return matrix

    rag = make_rag(
        tmp_path,
        "Alpha.",
        encoder=NormalizedEncoder(),  # type: ignore[arg-type]
    )

    encoded = rag._encode_batch(["query"])

    assert encoded is matrix


def test_local_model_identity_is_path_independent(tmp_path: Path) -> None:
    first_model = tmp_path / "first" / "same-model"
    second_model = tmp_path / "second" / "same-model"
    for model_path in (first_model, second_model):
        model_path.mkdir(parents=True)
        (model_path / "config.json").write_text(
            '{"architecture":"fake"}',
            encoding="utf-8",
        )
        (model_path / "model.safetensors").write_bytes(b"same-size")

    first_identity = RagSystem(
        model_name=str(first_model),
        encoder=FakeEncoder(),
    ).model_identity
    second_identity = RagSystem(
        model_name=str(second_model),
        encoder=FakeEncoder(),
    ).model_identity

    assert first_identity == second_identity

    (second_model / "model.safetensors").write_bytes(b"different")
    changed_weight_identity = RagSystem(
        model_name=str(second_model),
        encoder=FakeEncoder(),
    ).model_identity
    assert changed_weight_identity != first_identity

    (second_model / "model.safetensors").write_bytes(b"same-size")
    (second_model / "config.json").write_text(
        '{"architecture":"different"}',
        encoding="utf-8",
    )
    changed_identity = RagSystem(
        model_name=str(second_model),
        encoder=FakeEncoder(),
    ).model_identity
    assert changed_identity != first_identity


def test_encoder_output_is_explicitly_l2_normalized(tmp_path: Path) -> None:
    class RawEncoder:
        def encode(self, sentences: list[str], **_: object) -> np.ndarray:
            return np.tile(
                np.asarray([[3.0, 4.0]], dtype=np.float32),
                (len(sentences), 1),
            )

    rag = make_rag(
        tmp_path,
        "Alpha. Beta.",
        encoder=RawEncoder(),  # type: ignore[arg-type]
    )
    embeddings = rag.index_database()

    assert np.allclose(np.linalg.norm(embeddings, axis=1), 1.0)


def test_supplied_legacy_data_persists_with_real_corpus_provenance(
    tmp_path: Path,
) -> None:
    rag = make_rag(tmp_path, "Alpha. Beta.")
    rag.index_database(rag._read_data())

    fresh = make_rag(tmp_path, "Alpha. Beta.", reindex=False)
    assert fresh.load_embedding_matrix().shape == (2, 3)

    with pytest.raises(CorpusError, match="does not match"):
        fresh.index_database(["unrelated text"])
