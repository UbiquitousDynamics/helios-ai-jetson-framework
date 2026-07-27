# Third-party assets and services

The repository's `LICENSE` covers the Helios source code. It does not
automatically relicense bundled models, voices, corpora, sounds, images, or
models downloaded by Ollama. Each of those artifacts remains subject to its
own upstream terms.

`assets-manifest.json` is the machine-readable inventory. A missing source,
revision, or SPDX identifier means that redistribution clearance has not yet
been demonstrated; it does not mean that the artifact is unlicensed or public
domain.

## Recorded model information

| Artifact | Information present in this repository | Gap to close before redistribution |
| --- | --- | --- |
| `models/all-MiniLM-L6-v2/` | The bundled model card identifies `sentence-transformers/all-MiniLM-L6-v2` and declares Apache-2.0. | Record the exact upstream revision and preserve any additional upstream notices. |
| `audio/models/*-medium.onnx` | Piper configuration files identify language, dataset name, quality, and Piper format version. | Record the exact download URL/revision, voice-model license, dataset terms, and required attribution for both Paola and Alba. |
| `recognizer/models/vosk-model-small-en-us-0.15/` | The bundled README identifies the model and credits Alpha Cephei Inc. | Add the authoritative source URL, archive checksum, and the license/notice files that apply to this model snapshot. |
| `recognizer/models/vosk-model-small-it-0.22/` | The directory name and bundled README identify the model/version and accuracy results. | Add the authoritative source URL, archive checksum, copyright statement, and applicable license/notice files. |

## Other content

- `uploads/` contains the source corpus used to generate the retrieval index.
  Its origin and redistribution terms are not recorded here.
- `embeddings.npz` is derived data. Its redistribution rights follow the source
  corpus and any relevant model terms. New indexes carry an embedded integrity
  manifest that binds them to the source-corpus fingerprint and encoder.
- `sounds/` and `pictures/` do not currently include authorship, source, or
  license records.
- Ollama model names in configuration refer to models obtained outside this
  repository. Review the terms for the exact model tags deployed on a device.

## Release gate

Before publishing a binary image or redistributing this repository:

1. resolve every `not-recorded` entry used by that build;
2. record immutable source revisions and checksums;
3. include upstream license and attribution files;
4. confirm that corpus, voice-dataset, sound, and image terms allow the intended
   distribution; and
5. regenerate and validate the retrieval index from the approved corpus.
