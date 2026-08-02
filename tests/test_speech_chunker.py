import pytest

from api.speech_chunker import SpeechChunker


@pytest.mark.parametrize(
    "kwargs",
    [
        {"first_speech_min_chars": -1},
        {"speech_chunk_max_chars": -1},
    ],
)
def test_negative_limits_are_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        SpeechChunker(**kwargs)


def test_complete_sentences_are_emitted_without_splitting_at_commas() -> None:
    chunker = SpeechChunker()

    assert list(chunker.finish()) == []
    assert list(chunker.push("Hello, crew. Second sentence.")) == [
        "Hello, crew.",
        "Second sentence.",
    ]
    assert chunker.speech_committed


def test_first_fragment_waits_for_the_minimum_generated_text() -> None:
    chunker = SpeechChunker(first_speech_min_chars=10)

    assert list(chunker.push("Hi.")) == []
    assert list(chunker.push(" More words.")) == ["Hi.", "More words."]


def test_soft_limit_uses_whitespace_and_never_splits_a_word() -> None:
    chunker = SpeechChunker(speech_chunk_max_chars=12)

    assert list(chunker.push("alpha beta gamma delta")) == ["alpha beta"]
    assert list(chunker.finish()) == ["gamma delta"]

    long_word = SpeechChunker(speech_chunk_max_chars=4)
    assert list(long_word.push("abcdefgh")) == []
    assert list(long_word.finish()) == ["abcdefgh"]


def test_finish_removes_speech_markup_and_ignores_punctuation_only_text() -> None:
    chunker = SpeechChunker()

    assert list(chunker.push("***Ready. :")) == ["Ready."]
    assert list(chunker.finish()) == []
