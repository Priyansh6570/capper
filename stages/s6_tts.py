"""Stage 6 - Emotional voiceover.  Runs on: laptop (Chatterbox TTS, local).

Synthesizes a narration clip per panel with Resemble AI's **Chatterbox** TTS
running locally (package `chatterbox-tts`). Tuned to survive a 4GB RTX 3050:
the model is loaded ONCE, panels are synthesized sequentially (no batching), and
CUDA cache is freed between lines. The model is loaded in its native fp32; on the
GPU, generation runs under `torch.inference_mode()` and
`torch.autocast("cuda", dtype=torch.float16)` so the fp16 casting is consistent
and automatic (half-casting the submodules left some internal tensors fp32 and
broke the matmuls).

Model selection falls back automatically on failure/OOM:
    Chatterbox-Turbo on GPU  ->  full Chatterbox on GPU  ->  CPU
Each candidate is validated with a tiny test synth at load time, so a runtime or
VRAM problem is caught immediately and we drop to the next option instead of
crashing mid-run. If an individual line OOMs during the run, that one line is
retried on CPU (a CPU model is lazily loaded once) and the run continues.

Emotion -> prosody: `panel.emotion` maps to small, deliberately subtle
exaggeration + cfg_weight tweaks (tense/angry more expressive, excited up and a
touch faster, sad softer and slower; neutral is baseline). These knobs only take
effect on the FULL model - Turbo's generate() ignores them (flat affect). So the
emotion mapping is a no-op under the default Turbo path.

Emotional voice: set env `MANHWA_TTS_EMOTION=1` to DROP Turbo and use the full
Chatterbox model, where the per-emotion exaggeration/cfg_weight actually apply.
Turbo stays the fast default; the full model is slower but emotional. The run
prints which model is active and whether emotion is being applied.

Voice cloning: a PROJECT-specific reference clip (set in the framer's project
settings, uploaded as `projects/<slug>/assets/voice.wav`) is used if set,
resolved via the same `<chapter work>/media_settings.json` snapshot stage 7
reads (see `_load_media_settings` below - written by
`tools/framer/projects.py`). Otherwise `assets/voice_ref.wav` is used if it
exists; if neither is set, the model's default voice is used. Either way the
reference clip goes through the same float32/mono/24kHz preprocessing
(`_preprocess_ref`) before Chatterbox ever sees it.

Output: one wav per panel in `work/<chapter>/audio/<panel_id>.wav`, with
`panel.audio` + `panel.duration_s` (the REAL clip length, which drives slide
timing in stage 7). Empty `script_line` -> `audio=None`, `duration_s=0`.

Draft mode: set env `MANHWA_TTS_DRAFT=1` to use the fast edge-tts path instead
(no GPU, seconds per chapter) when you just want timings to iterate on stage 7.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from config import ROOT, chapter_work_dir, TTS_VOICE
from manifest import Manifest

# Reference clip for voice cloning. If absent, the default voice is used.
VOICE_REF = ROOT / "assets" / "voice_ref.wav"


def _load_media_settings(chapter_id: str) -> dict:
    """Read the framer's project media settings snapshot (music/voice/
    watermark/background, absolute paths), written by
    tools/framer/projects.py. Mirrors s7_assemble._load_media_settings():
    returns {} on any absence/failure, so run() falls back to the global
    VOICE_REF default untouched."""
    path = chapter_work_dir(chapter_id) / "media_settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad settings file must not sink the run
        return {}


# --------------------------------------------------------------------------- #
# Monkeypatch: force float32 at the entry of Chatterbox's audio pipelines
# --------------------------------------------------------------------------- #
# Even after pre-baking the reference clip to float32, Chatterbox's own loader
# re-widens the conditioning wav to float64 inside generate(), and its voice
# encoder builds mels with librosa (also float64). Both then crash where a
# float64 tensor meets float32 weights:
#   * s3tokenizer.log_mel_spectrogram, at `self._mel_filters @ magnitudes`
#     -> "expected scalar type Float but found Double" (magnitudes is float64).
#   * voice_encoder.VoiceEncoder.forward, at the LSTM
#     -> "RNN input dtype (float64) does not match weight dtype (float32)".
# We wrap each entry point once, at import, casting its input tensor to float32
# so every downstream dtype stays Float. Both methods document their inputs as
# float32 already, so the cast only repairs an upstream widening. We patch the
# CLASSES here (NOT the installed package files) and guard so it runs only once.
_MEL_PATCHED = False


def _patch_chatterbox_float32() -> None:
    """Coerce audio/mel tensors to float32 at the entry of Chatterbox's mel and
    voice-encoder pipelines. Each patch is independent and defensive: a no-op
    (with a note) if that internal path moved in this version."""
    global _MEL_PATCHED
    if _MEL_PATCHED:
        return
    import torch

    # --- 1) s3tokenizer mel pipeline ---------------------------------------
    try:
        from chatterbox.models.s3tokenizer.s3tokenizer import S3Tokenizer

        _orig_mel = S3Tokenizer.log_mel_spectrogram

        def log_mel_spectrogram(self, audio, *args, **kwargs):
            # Cast the waveform so magnitudes (and _mel_filters @ magnitudes)
            # stay Float, not Double.
            if torch.is_tensor(audio) and audio.dtype != torch.float32:
                audio = audio.float()
            return _orig_mel(self, audio, *args, **kwargs)

        S3Tokenizer.log_mel_spectrogram = log_mel_spectrogram
        print("  [s6] applied float32 patch to S3Tokenizer.log_mel_spectrogram")
    except ImportError as e:  # internal path changed in this version
        print(f"  [s6] s3tokenizer float32 patch skipped ({type(e).__name__}: {e})")

    # --- 2) voice-encoder LSTM pipeline ------------------------------------
    try:
        from chatterbox.models.voice_encoder.voice_encoder import VoiceEncoder

        _orig_fwd = VoiceEncoder.forward

        def forward(self, mels, *args, **kwargs):
            # librosa builds these mels as float64; the LSTM weights are float32.
            if torch.is_tensor(mels) and mels.dtype != torch.float32:
                mels = mels.float()
            return _orig_fwd(self, mels, *args, **kwargs)

        VoiceEncoder.forward = forward
        print("  [s6] applied float32 patch to VoiceEncoder.forward")
    except ImportError as e:
        print(f"  [s6] voice_encoder float32 patch skipped ({type(e).__name__}: {e})")

    _MEL_PATCHED = True


_patch_chatterbox_float32()

# Load order. Each tuple is (kind, device). Tried top to bottom; the first that
# loads AND passes a tiny validation synth wins. The model is always loaded in
# its native fp32; on cuda, generation runs under torch.autocast fp16 instead of
# half-casting the modules (which left some internal tensors fp32 and broke the
# matmuls with "mat1 and mat2 must have the same dtype").
#
# Turbo is the FAST DEFAULT but its generate() ignores the exaggeration/cfg_weight
# kwargs (see `_generate`'s TypeError fallback) -> flat affect. MANHWA_TTS_EMOTION=1
# drops Turbo and uses the FULL model, which honours those per-emotion knobs (the
# slower, genuinely emotional option).
_CANDIDATES_FAST = [
    ("turbo", "cuda"),  # Chatterbox-Turbo, GPU  (lightest, fastest; flat affect)
    ("full",  "cuda"),  # full Chatterbox, GPU
    ("full",  "cpu"),   # full Chatterbox, CPU   (always works)
]
_CANDIDATES_EMOTION = [
    ("full",  "cuda"),  # full Chatterbox, GPU   (honours emotion knobs)
    ("full",  "cpu"),   # full Chatterbox, CPU   (always works)
]


def _emotion_mode() -> bool:
    """MANHWA_TTS_EMOTION=1 -> use the FULL Chatterbox model so the per-emotion
    exaggeration/cfg_weight actually apply (Turbo ignores them). Slower; the fast
    default stays Turbo."""
    return os.environ.get("MANHWA_TTS_EMOTION", "").strip() not in ("", "0", "false", "False")


def _candidates() -> list[tuple[str, str]]:
    """Model load order for this run: emotion mode skips Turbo, fast default leads
    with it."""
    return _CANDIDATES_EMOTION if _emotion_mode() else _CANDIDATES_FAST

# emotion -> (exaggeration, cfg_weight). Baseline is 0.5 / 0.5. In Chatterbox a
# higher `exaggeration` is more expressive; a higher `cfg_weight` tightens and
# slightly speeds up pacing, lower is slower/more deliberate. Kept subtle.
_EMOTION = {
    "neutral":   (0.50, 0.50),
    "tense":     (0.60, 0.55),
    "angry":     (0.70, 0.55),
    "excited":   (0.70, 0.60),
    "surprised": (0.65, 0.55),
    "sad":       (0.35, 0.40),
}
_EMOTION_DEFAULT = _EMOTION["neutral"]


def _emotion_params(emotion: str | None) -> tuple[float, float]:
    return _EMOTION.get((emotion or "").strip().lower(), _EMOTION_DEFAULT)


# --------------------------------------------------------------------------- #
# Chatterbox loading + synthesis
# --------------------------------------------------------------------------- #
def _load_model(kind: str, device: str):
    """Construct one Chatterbox model on `device` in its native fp32. On cuda we
    do NOT half-cast the submodules (that left internal tensors fp32 and broke
    matmuls); fp16 is applied at generation time via torch.autocast instead."""
    if kind == "turbo":
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        model = ChatterboxTurboTTS.from_pretrained(device=device)
    else:
        from chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device=device)
    return model


def _build_engine():
    """Walk the candidate list and return the first model that loads and can
    actually produce audio on this machine. Returns (model, device, sr, label)."""
    import torch

    last_err: Exception | None = None
    for kind, device in _candidates():
        if device == "cuda" and not torch.cuda.is_available():
            continue
        label = f"{kind}/{device}"
        try:
            print(f"  loading Chatterbox: trying {label} ...")
            model = _load_model(kind, device)
            with torch.inference_mode():       # validate it can really generate
                if device == "cuda":
                    with torch.autocast("cuda", dtype=torch.float16):
                        model.generate("Test.")
                else:
                    model.generate("Test.")
            if device == "cuda":
                torch.cuda.empty_cache()
            print(f"  -> using {label} (sr={model.sr})")
            return model, device, model.sr, label
        except Exception as e:  # noqa: BLE001 - OOM or fp16/runtime issue: fall back
            last_err = e
            print(f"     {label} unavailable: {type(e).__name__}: {e}")
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError(f"No Chatterbox model could be loaded: {last_err}")


def _generate(model, device: str, text: str, exaggeration: float,
              cfg_weight: float, ref: str | None):
    """One synth under inference_mode, freeing CUDA cache afterwards. Turbo may
    not accept the prosody kwargs; fall back to a plain call if so.

    Autocast caveat: the fp16 autocast region corrupts reference-audio
    conditioning -- the ref wav loads as float64/double and s3tokenizer's
    log_mel_spectrogram then crashes with "expected scalar type Half but found
    Double". The conditioning runs inside model.generate, so we run the WHOLE
    ref call in native fp32 (no autocast). Autocast fp16 is kept only for the
    no-ref path on cuda, where casting stays consistent."""
    import torch
    import contextlib

    base = {"audio_prompt_path": ref} if ref else {}
    # fp16 autocast only when there is no ref clip; the ref path runs fp32 so the
    # reference-conditioning math keeps consistent dtypes.
    autocast = (torch.autocast("cuda", dtype=torch.float16)
                if device == "cuda" and not ref else contextlib.nullcontext())
    with torch.inference_mode(), autocast:
        try:
            wav = model.generate(text, exaggeration=exaggeration,
                                 cfg_weight=cfg_weight, **base)
        except TypeError:
            wav = model.generate(text, **base)
    if device == "cuda":
        torch.cuda.empty_cache()
    return wav


class _Engine:
    """Holds the chosen model and a lazily-loaded CPU fallback for per-line OOM."""

    def __init__(self):
        self.model, self.device, self.sr, self.label = _build_engine()
        self._cpu = None  # lazy full-Chatterbox-on-CPU for OOM recovery
        self._ref = None  # cached float32/mono/24k temp ref wav (once per run)

    def prepare_ref(self, ref_path: str | None, chapter_id: str) -> str | None:
        """Preprocess the voice-reference clip ONCE per run and cache the result.
        Returns the path to the float32 temp wav (or None when no ref is set)."""
        if not ref_path:
            return None
        if self._ref is None:
            tmp = chapter_work_dir(chapter_id) / "_voice_ref_f32.wav"
            self._ref = _preprocess_ref(ref_path, tmp)
            print(f"    prepared float32 voice ref -> {tmp}")
        return self._ref

    def _cpu_model(self):
        if self._cpu is None:
            if self.device == "cpu":
                self._cpu = self.model
            else:
                print("    loading CPU fallback model (full Chatterbox) ...")
                self._cpu = _load_model("full", "cpu")
        return self._cpu

    def synth(self, text, exaggeration, cfg_weight, ref):
        """Returns (wav_tensor, sample_rate, device_used)."""
        import torch

        try:
            wav = _generate(self.model, self.device, text,
                            exaggeration, cfg_weight, ref)
            return wav, self.sr, self.device
        except torch.cuda.OutOfMemoryError:
            print("    CUDA OOM on this line; retrying it on CPU")
            torch.cuda.empty_cache()
            cpu = self._cpu_model()
            wav = _generate(cpu, "cpu", text, exaggeration, cfg_weight, ref)
            return wav, cpu.sr, "cpu(oom-fallback)"


def _preprocess_ref(ref_path: str, out_path: Path) -> str:
    """Normalize a voice-reference clip to a clean float32 / mono / 24kHz wav.

    The crash "expected scalar type Float but found Double" in Chatterbox's
    log_mel_spectrogram comes from its OWN audio loading/resampling producing a
    float64 tensor -- the file on disk (16-bit mono 24kHz) is fine, so editing
    it won't help. We pre-bake the reference once here with soundfile: explicit
    float32, downmixed to mono, resampled to 24kHz, written back as a 32-bit
    float wav. Chatterbox's loader then has nothing left to widen to double."""
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(ref_path, always_2d=True)          # (frames, channels)
    data = data.astype(np.float32, copy=False)            # force float32
    if data.shape[1] > 1:                                 # downmix to mono
        data = data.mean(axis=1, keepdims=True).astype(np.float32, copy=False)

    target_sr = 24000
    if sr != target_sr:                                   # resample if needed
        import torch
        import torchaudio.functional as AF
        t = torch.from_numpy(data.T).contiguous()         # (channels, frames)
        t = AF.resample(t, sr, target_sr).to(torch.float32)
        data = t.T.contiguous().numpy().astype(np.float32, copy=False)
        sr = target_sr

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), data, sr, subtype="FLOAT")    # 32-bit float wav
    return str(out_path)


def _save_wav(wav, sr: int, out: Path) -> float:
    """Write `wav` to `out` and return its real duration in seconds."""
    import torchaudio as ta

    w = wav.detach().to("cpu")
    if w.dim() == 1:
        w = w.unsqueeze(0)           # (frames,) -> (1, frames) for torchaudio
    ta.save(str(out), w, sr)
    return w.shape[-1] / float(sr)   # frames / sample_rate = true length


# --------------------------------------------------------------------------- #
# Draft path (edge-tts) - fast, no GPU. Enabled with MANHWA_TTS_DRAFT=1.
# --------------------------------------------------------------------------- #
_DRAFT_PROSODY = {
    "neutral": ("+0%", "+0Hz"), "tense": ("+8%", "+0Hz"),
    "angry": ("+6%", "-2Hz"),   "excited": ("+12%", "+6Hz"),
    "surprised": ("+8%", "+8Hz"), "sad": ("-10%", "-6Hz"),
}


async def _edge_synth(text: str, out: Path, rate: str, pitch: str) -> None:
    import edge_tts

    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, TTS_VOICE, rate=rate,
                                       pitch=pitch).save(str(out))
            if out.exists() and out.stat().st_size > 0:
                return
        except Exception:  # noqa: BLE001 - retry transient network errors
            pass
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"edge-tts draft failed for {out.name}")


def _run_draft(m: Manifest, audio_dir: Path) -> Manifest:
    from mutagen.mp3 import MP3

    panels = m.kept_panels()  # the matched narration beats (STAGE B)
    spoken = 0
    for panel in panels:
        line = (panel.script_line or "").strip()
        if not line:
            panel.audio, panel.duration_s = None, 0
            continue
        out = audio_dir / f"{panel.id}.mp3"
        ex, cfg = _emotion_params(panel.emotion)  # noqa: F841 - draft uses rate/pitch
        rate, pitch = _DRAFT_PROSODY.get(
            (panel.emotion or "").strip().lower(), ("+0%", "+0Hz"))
        asyncio.run(_edge_synth(line, out, rate, pitch))
        panel.audio = str(out)
        panel.duration_s = round(float(MP3(str(out)).info.length), 2)
        spoken += 1

    total = sum(p.duration_s or 0 for p in panels)
    print(f"  [draft] voiced {spoken}/{len(panels)} panels with edge-tts "
          f"({TTS_VOICE}), ~{total:.1f}s total -> {audio_dir}")
    m.stage = "audio"
    return m


# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    audio_dir = chapter_work_dir(m.chapter_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    if os.environ.get("MANHWA_TTS_DRAFT", "").strip() not in ("", "0", "false", "False"):
        return _run_draft(m, audio_dir)

    if _emotion_mode():
        print("  [s6] MANHWA_TTS_EMOTION=1: FULL Chatterbox (Turbo disabled) - "
              "per-emotion exaggeration/cfg_weight ARE applied")
    else:
        print("  [s6] fast default: Chatterbox-Turbo (flat affect; emotion knobs "
              "ignored). Set MANHWA_TTS_EMOTION=1 for the emotional full model")

    settings = _load_media_settings(m.chapter_id)
    project_voice = settings.get("voice")
    if project_voice and Path(project_voice).exists():
        ref = project_voice
        print(f"  voice cloning from project voice ref: {ref}")
    elif VOICE_REF.exists():
        ref = str(VOICE_REF)
        print(f"  voice cloning from default voice ref: {ref}")
    else:
        ref = None

    engine = _Engine()
    # Pre-bake the reference to float32/mono/24k once; passing the raw clip lets
    # Chatterbox's loader widen it to float64 and crash log_mel_spectrogram.
    ref = engine.prepare_ref(ref, m.chapter_id)
    panels = m.kept_panels()  # the matched narration beats (STAGE B)
    spoken = 0
    for panel in panels:
        line = (panel.script_line or "").strip()
        if not line:
            panel.audio, panel.duration_s = None, 0
            continue

        ex, cfg = _emotion_params(panel.emotion)
        wav, sr, used = engine.synth(line, ex, cfg, ref)
        out = audio_dir / f"{panel.id}.wav"
        dur = _save_wav(wav, sr, out)

        panel.audio = str(out)
        panel.duration_s = round(dur, 2)
        spoken += 1
        print(f"    [{panel.order:03d}] ({panel.emotion or 'neutral'}: "
              f"ex={ex:.2f} cfg={cfg:.2f}) {dur:5.2f}s on {used}")

    total = sum(p.duration_s or 0 for p in panels)
    print(f"  voiced {spoken}/{len(panels)} panels with Chatterbox "
          f"[{engine.label}], ~{total:.1f}s total -> {audio_dir}")
    m.stage = "audio"
    return m
