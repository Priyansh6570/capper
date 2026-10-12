"""Kokoro (82M parameters, no voice cloning) as the alternative narration engine of stage 6.

Chosen per project (Project settings -> Narration voice); stage 6 hands the chapter to `run`
when the chapter's media settings say so. Kokoro ships fixed voices instead of cloning one, so
the setting names one of `VOICES`. Same outputs as the Chatterbox path: one wav per beat in
`work/<chapter>/audio/`, with `panel.audio` and the real `panel.duration_s`.
"""
from __future__ import annotations

import functools
import io
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from gpu import record_usage
from manifest import Manifest

VOICES = {
    "af_heart": "Heart (American, female)",
    "af_bella": "Bella (American, female)",
    "af_nicole": "Nicole (American, female)",
    "af_sarah": "Sarah (American, female)",
    "af_sky": "Sky (American, female)",
    "am_adam": "Adam (American, male)",
    "am_michael": "Michael (American, male)",
    "bf_emma": "Emma (British, female)",
    "bf_isabella": "Isabella (British, female)",
    "bm_george": "George (British, male)",
    "bm_lewis": "Lewis (British, male)",
}
DEFAULT_VOICE = "af_heart"
SAMPLE_RATE = 24000
SAMPLE_TEXT = ("The blade hummed once, and the whole forest went quiet. He knew then that "
               "nothing about this night would be ordinary.")


@functools.lru_cache(maxsize=4)
def _pipeline(lang_code: str, device: str):
    import espeakng_loader
    from kokoro import KPipeline
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    # words missing from Kokoro's dictionary (names, invented terms) are pronounced by espeak;
    # without this pointer to the bundled copy they would fail instead
    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    return KPipeline(lang_code=lang_code, device=device)


def load_pipeline(voice: str, device: str):
    """Kokoro's pipeline for the language of `voice` (its first letter: a = American, b = British)."""
    return _pipeline(voice[0], device)


def synthesize(pipeline, text: str, voice: str) -> np.ndarray:
    """The narration of `text` as one float32 mono waveform at SAMPLE_RATE."""
    return np.concatenate([np.asarray(audio, dtype=np.float32).reshape(-1)
                           for _, _, audio in pipeline(text, voice=voice) if audio is not None])


def sample_wav(voice: str, text: str = SAMPLE_TEXT) -> bytes:
    """A short wav of `voice`, rendered on the CPU so a preview never competes with a render for the GPU."""
    buffer = io.BytesIO()
    sf.write(buffer, synthesize(load_pipeline(voice, "cpu"), text, voice), SAMPLE_RATE, format="WAV")
    return buffer.getvalue()


def run(m: Manifest, audio_dir: Path, voice: str) -> Manifest:
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  [s6] Kokoro voice {voice} on {device}")
    started = time.perf_counter()
    pipeline = load_pipeline(voice, device)
    panels = m.kept_panels()
    spoken = 0
    for panel in panels:
        line = (panel.script_line or "").strip()
        if not line:
            panel.audio, panel.duration_s = None, 0
            continue
        wav = synthesize(pipeline, line, voice)
        out = audio_dir / f"{panel.id}.wav"
        sf.write(str(out), wav, SAMPLE_RATE, subtype="FLOAT")
        panel.audio, panel.duration_s = str(out), round(len(wav) / SAMPLE_RATE, 2)
        spoken += 1
        print(f"    [{panel.order:03d}] ({voice}) {panel.duration_s:5.2f}s on {device}")
    total = sum(p.duration_s or 0 for p in panels)
    print(f"  voiced {spoken}/{len(panels)} panels with Kokoro [{voice}/{device}], ~{total:.1f}s total "
          f"in {time.perf_counter() - started:.1f}s -> {audio_dir}")
    record_usage("audio", f"kokoro/{voice}", device == "cuda")
    m.stage = "audio"
    return m
