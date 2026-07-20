"""Turn a customer's voice note into text the AI pipeline can answer.

Voice notes matter here more than they would in an English-first product:
typing Khmer on a phone is slow, so a customer who would never type a
paragraph will happily send ten seconds of audio.

The catch is that Khmer speech recognition is materially worse than English,
and its failures are not graceful. A garbled transcript does not produce a
garbled answer — it produces a fluent, confident answer to a question the
customer never asked, which is worse than silence. So every transcript comes
back with a confidence score, and the caller is expected to refuse to
auto-reply below a threshold (see ``Transcript.is_reliable``).

Confidence is derived from Whisper's per-segment ``avg_logprob`` and
``no_speech_prob``. These are proxies, not calibrated probabilities, and the
thresholds below are starting points taken from general Whisper practice —
they have NOT been tuned against Khmer audio. Treat
``VOICE_MIN_CONFIDENCE`` as a dial to set from measured data (see
``scripts/compare_khmer_asr.py``), not as a value that is known to be right.
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# Whisper's own language code for Khmer. Passing it explicitly matters: left to
# auto-detect, short or noisy Khmer clips are regularly misidentified as Thai or
# Lao, which yields confident nonsense rather than a low score.
KHMER = "km"

# Whisper caps uploads at 25MB. Voice notes are far smaller, so anything near
# this is a forwarded file or an attack, not a customer talking.
MAX_AUDIO_BYTES = 20 * 1024 * 1024

# Past roughly a minute a "voice note" is a monologue, not a question, and both
# transcription accuracy and the seller's patience fall off. Callers surface
# these to the seller instead of transcribing.
MAX_AUDIO_SECONDS = 120


class TranscriptionError(Exception):
    """Audio could not be transcribed."""


@dataclass
class Transcript:
    """A transcription plus the evidence for how much to trust it."""

    text: str
    language: str
    duration: float
    # 0.0–1.0, derived from Whisper's log-probabilities. Not a calibrated
    # probability of correctness — a monotonic "how sure did the model sound".
    confidence: float
    # Whisper's estimate that the clip is silence/noise rather than speech.
    no_speech_prob: float

    def is_reliable(self, threshold: float | None = None) -> bool:
        """Whether this transcript is good enough to answer automatically.

        Deliberately strict. The cost of a false positive (bot confidently
        answers a misheard question) is a lost sale and a seller who stops
        trusting the product; the cost of a false negative is the seller
        reading one message by hand.
        """
        limit = settings.VOICE_MIN_CONFIDENCE if threshold is None else threshold
        return (
            bool(self.text.strip())
            and self.confidence >= limit
            and self.no_speech_prob < 0.5
        )


def _confidence_from_segments(segments: list) -> tuple[float, float]:
    """Collapse Whisper's per-segment stats into (confidence, no_speech_prob).

    ``avg_logprob`` is a natural-log mean token probability: ~-0.1 is a clean
    read, ~-1.0 is the model guessing. Exponentiating maps that onto 0–1 so the
    threshold reads like a probability, which is easier to reason about even
    though it is not literally one.

    Segments are weighted by duration — a long confident sentence should not be
    dragged down by a half-second "um" at the end — and no_speech_prob takes
    the max, since one segment of pure silence is enough to make the whole
    transcript suspect.
    """
    if not segments:
        # No segments but text present means an unexpected response shape.
        # Return a score that fails every threshold rather than inventing
        # confidence we do not have.
        return 0.0, 1.0

    total_weight = 0.0
    weighted_logprob = 0.0
    worst_no_speech = 0.0

    for seg in segments:
        start = getattr(seg, "start", 0.0) or 0.0
        end = getattr(seg, "end", 0.0) or 0.0
        # Guard against zero-length segments so a clip made entirely of them
        # cannot divide by zero below.
        weight = max(end - start, 0.01)
        logprob = getattr(seg, "avg_logprob", -1.0)
        if logprob is None:
            logprob = -1.0

        weighted_logprob += logprob * weight
        total_weight += weight
        worst_no_speech = max(worst_no_speech, getattr(seg, "no_speech_prob", 0.0) or 0.0)

    mean_logprob = weighted_logprob / total_weight
    return math.exp(mean_logprob), worst_no_speech


async def transcribe(
    audio: bytes,
    filename: str = "voice.ogg",
    *,
    language: str = KHMER,
    prompt: str | None = None,
) -> Transcript:
    """Transcribe a voice note, with a confidence score attached.

    ``prompt`` is Whisper's vocabulary hint. Passing the seller's product names
    here is the single highest-leverage accuracy fix available: generic Khmer
    ASR mangles exactly the proper nouns the reply depends on, and the hint
    biases decoding toward words we know are plausible. Build it with
    ``product_vocabulary_prompt``.
    """
    if not settings.OPENAI_API_KEY:
        raise TranscriptionError("OPENAI_API_KEY is not configured")

    if not audio:
        raise TranscriptionError("Audio payload is empty")

    if len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionError(
            f"Audio is {len(audio)} bytes, over the {MAX_AUDIO_BYTES} limit"
        )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # A file-like object with a name: the API infers the container format from
    # the extension, and Telegram voice notes are OGG/Opus.
    buffer = io.BytesIO(audio)
    buffer.name = filename

    try:
        result = await client.audio.transcriptions.create(
            model=settings.VOICE_TRANSCRIBE_MODEL,
            file=buffer,
            language=language,
            prompt=prompt or "",
            # verbose_json is what carries the per-segment log-probabilities.
            # Without it there is no confidence signal at all and the gating
            # below silently degrades to "always reply".
            response_format="verbose_json",
        )
    except Exception as exc:
        raise TranscriptionError("Transcription request failed") from exc

    segments = list(getattr(result, "segments", None) or [])
    confidence, no_speech = _confidence_from_segments(segments)

    transcript = Transcript(
        text=(getattr(result, "text", "") or "").strip(),
        language=getattr(result, "language", language) or language,
        duration=float(getattr(result, "duration", 0.0) or 0.0),
        confidence=confidence,
        no_speech_prob=no_speech,
    )

    logger.info(
        "Transcribed %.1fs of audio: confidence=%.2f no_speech=%.2f chars=%d",
        transcript.duration,
        transcript.confidence,
        transcript.no_speech_prob,
        len(transcript.text),
    )
    return transcript


def product_vocabulary_prompt(product_names: list[str], limit: int = 60) -> str:
    """Build a Whisper vocabulary hint from a seller's catalogue.

    Whisper's prompt is capped (224 tokens) and silently truncates, so this
    takes the shortest names first — long descriptive titles eat the budget
    and are the least likely to be spoken aloud in full anyway.
    """
    names = sorted({n.strip() for n in product_names if n and n.strip()}, key=len)
    if not names:
        return ""
    return ", ".join(names[:limit])
