import pytest

from meeting_scribe.transcribe.filters import RawSegment, is_hallucination, keep_segments


def seg(text, nsp=0.1, lp=-0.3, start=0.0, end=1.0):
    return RawSegment(start=start, end=end, text=text, avg_logprob=lp, no_speech_prob=nsp)


def test_silence_segment_dropped_by_thresholds():
    assert is_hallucination(seg("algo raro", nsp=0.7, lp=-1.2))
    assert not is_hallucination(seg("algo raro", nsp=0.7, lp=-0.5))  # confident speech is kept
    assert not is_hallucination(seg("algo raro", nsp=0.3, lp=-1.5))


@pytest.mark.parametrize("text", [
    "Thanks for watching!", "Subtítulos realizados por la comunidad de Amara.org",
    "Gracias por ver el video.", "¡Suscríbete!", "  ", "...",
    "la " * 10,
])
def test_known_hallucinations_dropped(text):
    assert is_hallucination(seg(text))


@pytest.mark.parametrize("text", ["gracias gracias gracias gracias gracias", "la la la la la la"])
def test_short_loops_dropped_only_when_unsure(text):
    assert is_hallucination(seg(text, nsp=0.5))
    assert not is_hallucination(seg(text, nsp=0.05, lp=-0.2))


def test_confident_repetition_is_real_speech():
    """Review finding 3: 'no, no, no, no' is a Spanish (and English) reply, not a hallucination."""
    assert not is_hallucination(seg("No, no, no, no.", nsp=0.05, lp=-0.2))


@pytest.mark.parametrize("lang,text", [
    ("es", "¿Podemos publicar la versión mañana?"),
    ("ru", "Мы выпускаем релиз завтра утром."),
    ("zh", "我们明天发布"),
    ("ja", "明日リリースします。"),
    ("ar", "سننشر الإصدار غدا"),
])
def test_confident_non_latin_speech_is_kept(lang, text):
    assert not is_hallucination(seg(text, nsp=0.05, lp=-0.2)), lang


def test_non_latin_ambiguous_still_needs_confidence_logic():
    assert is_hallucination(seg("。。。"))  # punctuation only
    assert is_hallucination(seg("спасибо", nsp=0.7, lp=-1.2))  # silence thresholds apply to all scripts


def test_ambiguous_short_phrases_need_low_confidence():
    # "gracias" is a real thing people say in meetings: only drop it when whisper was unsure.
    assert not is_hallucination(seg("Gracias.", nsp=0.05, lp=-0.2))
    assert is_hallucination(seg("Gracias.", nsp=0.5, lp=-0.2))
    assert is_hallucination(seg("Thank you.", nsp=0.1, lp=-0.9))


def test_normal_repetition_is_kept():
    assert not is_hallucination(seg("no, no, no, eso no es así"))


def test_keep_segments_filters_list():
    kept = keep_segments([seg("hola equipo"), seg("thanks for watching"), seg("vale", nsp=0.9, lp=-2)])
    assert [s.text for s in kept] == ["hola equipo"]
