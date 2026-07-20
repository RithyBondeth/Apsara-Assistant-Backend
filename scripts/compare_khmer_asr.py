#!/usr/bin/env python
"""Measure Khmer speech recognition on YOUR audio, not on a benchmark.

Why this exists: every threshold in services/transcription.py is currently a
guess borrowed from general (mostly English) Whisper practice. Khmer is a
low-resource language and behaves differently. Shipping voice auto-reply on
guessed thresholds means either the feature never fires, or it fires on
transcripts it shouldn't — and you won't know which until sellers complain.

What it does:

  1. Runs each audio file through each configured engine.
  2. If you supply reference transcripts, reports character error rate (CER).
     CER, not word error rate — Khmer has no whitespace word boundaries, so
     WER is not meaningful without a segmenter, and a segmenter would measure
     the segmenter as much as the ASR.
  3. Reports each engine's self-reported confidence next to its actual error,
     which is the number that sets VOICE_MIN_CONFIDENCE. A confidence score
     that doesn't correlate with error is useless as a gate, and you can only
     find that out by measuring.

Usage:

    # Drop voice notes in a directory, then:
    python scripts/compare_khmer_asr.py samples/

    # With reference transcripts for error rates — for each foo.ogg, put the
    # correct Khmer text in foo.txt alongside it:
    python scripts/compare_khmer_asr.py samples/ --references

    # Just Whisper (skip engines you haven't set up):
    python scripts/compare_khmer_asr.py samples/ --engines whisper

Getting real samples: ask two or three friendly sellers to forward their
customers' actual voice notes. Studio-quality recordings of you reading a
script will overstate accuracy badly — real ones have market noise, phone
speakers, dialect, and people talking over themselves.

Have a native Khmer speaker write the reference transcripts. Without
references this script still reports confidence and lets you eyeball the
text, but it cannot tell you the error rate, which is the number that matters.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Run from the backend root so `app` imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import transcription  # noqa: E402

AUDIO_SUFFIXES = {".ogg", ".oga", ".mp3", ".m4a", ".wav", ".webm", ".mp4", ".mpga"}


@dataclass
class Result:
    engine: str
    filename: str
    text: str = ""
    confidence: float | None = None
    error: str | None = None
    cer: float | None = None


@dataclass
class EngineStats:
    name: str
    results: list[Result] = field(default_factory=list)

    @property
    def scored(self) -> list[Result]:
        return [r for r in self.results if r.cer is not None]

    @property
    def mean_cer(self) -> float | None:
        scored = self.scored
        return sum(r.cer for r in scored) / len(scored) if scored else None

    @property
    def failures(self) -> int:
        return sum(1 for r in self.results if r.error)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over characters, normalized by reference length.

    Whitespace is stripped from both sides before comparing: Khmer doesn't use
    spaces between words, so where an engine chooses to insert them is a
    formatting artifact, not a recognition error, and counting it would make a
    good engine look bad.
    """
    ref = "".join(reference.split())
    hyp = "".join(hypothesis.split())

    if not ref:
        return 0.0 if not hyp else 1.0

    # Standard two-row dynamic programming; the full matrix is unnecessary
    # since we only need the distance, not the alignment.
    previous = list(range(len(hyp) + 1))
    for i, ref_char in enumerate(ref, start=1):
        current = [i]
        for j, hyp_char in enumerate(hyp, start=1):
            current.append(
                min(
                    previous[j] + 1,           # deletion
                    current[j - 1] + 1,        # insertion
                    previous[j - 1] + (ref_char != hyp_char),  # substitution
                )
            )
        previous = current

    return previous[-1] / len(ref)


async def run_whisper(path: Path, vocabulary: str) -> Result:
    """Transcribe via the same code path production uses."""
    try:
        transcript = await transcription.transcribe(
            path.read_bytes(), path.name, prompt=vocabulary
        )
    except Exception as exc:
        return Result("whisper", path.name, error=str(exc))
    return Result(
        "whisper", path.name, text=transcript.text, confidence=transcript.confidence
    )


async def run_google(path: Path, vocabulary: str) -> Result:
    """Transcribe via Google Cloud Speech-to-Text (km-KH).

    Not wired up. Google is a plausible winner for Khmer given its Southeast
    Asia language investment, but that is an expectation, not a measurement —
    which is the whole reason this script exists. To enable it:

      pip install google-cloud-speech
      export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

    then implement below with language_code="km-KH", passing `vocabulary` as
    SpeechContext phrases (Google's equivalent of Whisper's prompt) so both
    engines get the same product-name advantage and the comparison stays fair.
    """
    return Result("google", path.name, error="not configured — see docstring")


ENGINES = {"whisper": run_whisper, "google": run_google}


def load_reference(audio_path: Path) -> str | None:
    sidecar = audio_path.with_suffix(".txt")
    return sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("directory", type=Path, help="Directory of audio samples")
    parser.add_argument(
        "--engines",
        default="whisper",
        help=f"Comma-separated: {', '.join(ENGINES)} (default: whisper)",
    )
    parser.add_argument(
        "--references",
        action="store_true",
        help="Score against <name>.txt sidecar files and report error rates",
    )
    parser.add_argument(
        "--vocabulary",
        default="",
        help="Comma-separated product names, as production passes at runtime",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"Not a directory: {args.directory}", file=sys.stderr)
        return 1

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        print(f"Unknown engine(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    samples = sorted(
        p for p in args.directory.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES
    )
    if not samples:
        print(f"No audio files in {args.directory}", file=sys.stderr)
        return 1

    if not os.environ.get("OPENAI_API_KEY") and "whisper" in engines:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    print(f"{len(samples)} sample(s), engines: {', '.join(engines)}\n")
    if not args.references:
        print(
            "No --references: reporting confidence and text only. Error rates\n"
            "need reference transcripts, and error rate is the number that\n"
            "actually decides whether to ship this.\n"
        )

    stats = {name: EngineStats(name) for name in engines}

    for path in samples:
        reference = load_reference(path) if args.references else None
        print(f"── {path.name} " + "─" * max(0, 60 - len(path.name)))
        if reference:
            print(f"   reference: {reference}")

        for name in engines:
            result = await ENGINES[name](path, args.vocabulary)
            if reference and result.text:
                result.cer = character_error_rate(reference, result.text)
            stats[name].results.append(result)

            if result.error:
                print(f"   {name:8} FAILED: {result.error}")
                continue

            line = f"   {name:8} {result.text}"
            annotations = []
            if result.confidence is not None:
                annotations.append(f"confidence={result.confidence:.2f}")
            if result.cer is not None:
                annotations.append(f"CER={result.cer:.1%}")
            if annotations:
                line += f"\n   {'':8} [{'  '.join(annotations)}]"
            print(line)
        print()

    print("═" * 68)
    print("SUMMARY\n")
    for name in engines:
        s = stats[name]
        mean = s.mean_cer
        mean_text = f"{mean:.1%}" if mean is not None else "n/a (no references)"
        print(f"  {name:8} mean CER: {mean_text}    failures: {s.failures}/{len(s.results)}")

    # The calibration table. This, not the mean, is what sets the threshold:
    # a confidence gate only works if low confidence actually predicts high
    # error. If the two columns don't move together, the gate is noise and you
    # need a different signal (or human review on every voice note).
    whisper_scored = [
        r for r in stats.get("whisper", EngineStats("whisper")).scored
        if r.confidence is not None
    ]
    if whisper_scored:
        print("\n  Whisper confidence vs. actual error — does the gate predict anything?\n")
        buckets = [(0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 1.01)]
        for low, high in buckets:
            in_bucket = [r for r in whisper_scored if low <= r.confidence < high]
            if not in_bucket:
                continue
            bucket_cer = sum(r.cer for r in in_bucket) / len(in_bucket)
            print(
                f"    confidence {low:.2f}–{high:.2f}: "
                f"{len(in_bucket):3d} sample(s), mean CER {bucket_cer:.1%}"
            )
        print(
            "\n  Set VOICE_MIN_CONFIDENCE at the bucket where CER becomes\n"
            "  tolerable for auto-reply. If CER is flat across buckets, the\n"
            "  confidence score is not predictive on your audio — keep\n"
            "  VOICE_AUTO_REPLY off and leave voice in review mode."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
