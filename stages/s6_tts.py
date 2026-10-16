"""Stage 6 - Voiceover.  Runs on: laptop (Chatterbox-Turbo TTS, local).

Synthesizes a narration clip per panel with Resemble AI's **Chatterbox-Turbo** TTS
running locally (package `chatterbox-tts`). Tuned to survive a 4GB RTX 3050:
the model is loaded ONCE, panels are synthesized sequentially (no batching), and
CUDA cache is freed between lines. The model is loaded in its native fp32; on the
GPU, generation runs under `torch.inference_mode()` and
`torch.autocast("cuda", dtype=torch.float16)` so the fp16 casting is consistent
and automatic (half-casting the submodules left some internal tensors fp32 and
broke the matmuls).

Model selection falls back automatically on failure/OOM:
    Chatterbox-Turbo on GPU  ->  Chatterbox-Turbo on CPU
Each candidate is validated with a tiny test synth at load time, so a runtime or
VRAM problem is caught immediately and we drop to the next option instead of
crashing mid-run. If an individual line OOMs during the run, that one line is
retried on CPU (a CPU model is lazily loaded once) and the run continues.

Pacing: each line is voiced one sentence at a time and joined by `pacing.voice_line`, which trims the
silence the engine leaves around a sentence and lays the pauses itself, from the line's mood
(`panel.emotion`: a pace such as tense, reveal or sad). Pauses therefore fall only between
sentences and after the line, and speaking speed follows the mood and the chapter's tone
(Turbo has no speed control, so its sentences are time-stretched). The clip written to disk includes
those pauses; stage 7 adds none of its own.

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

Kokoro: a project can choose Kokoro instead (Project settings -> Narration voice). The
chapter's media settings then carry `tts: {engine: "kokoro", kokoro_voice}` and the whole chapter
is voiced by `tts_kokoro.run`; everything above applies to the Chatterbox engine only.

"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from atomic_io import clear_directory
import pacing
import tones
from config import ROOT, chapter_work_dir
from gpu import nvidia_gpu, record_usage
from manifest import Manifest
from stages import tts_kokoro

# Reference clip for voice cloning. If absent, the default voice is used.
VOICE_REF = ROOT / "assets" / "voice_ref.wav"


# --------------------------------------------------------------------------- #
# Render-time safety net for a missing/incomplete model cache
# --------------------------------------------------------------------------- #
# setup.bat pre-downloads the Chatterbox repo at install time (see
# tools/predownload_tts_models.py) specifically so this never has to happen
# here - but that step is best-effort (it can exhaust its retries on a bad
# connection, and on an installer-driven unattended install its own warning
# never reaches the user). If a repo genuinely isn't cached yet, falling
# straight through to from_pretrained() means an un-tracked huggingface_hub
# download with no visible progress (its tqdm output isn't byte-based per
# line the way this app's job log expects, so it just looks stuck at
# "Fetching N files: 0%" until an entire multi-GB file finishes) and no
# retry if the connection stalls. Reuse the exact same retry/stall/resume
# logic here instead, but only when the cache actually needs it - a fully
# cached model (the normal case, post-setup) hits the local_files_only check
# below and returns immediately, no network touched.
def _ensure_model_cached() -> None:
    try:
        tools_dir = str(ROOT / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import predownload_tts_models as _pm

        repo_id, patterns = _pm.MODEL
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repo_id, allow_patterns=patterns, local_files_only=True)
            return  # already fully cached - no network touched
        except Exception:  # noqa: BLE001 - not (fully) cached; fetch it below
            pass
        # A saved HF_TOKEN (see setup.bat step 5 / SETUP_GUIDE.md) avoids
        # anonymous-IP throttling, but nothing else in this process loaded .env yet.
        # load_dotenv() never overwrites an already-set env var.
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:  # noqa: BLE001
            pass
        print(f"  [voice-model] downloading {repo_id} (one-time, needs internet - "
              f"can be several GB; the app will do this only once)...")
        ok = _pm.download_with_retry(repo_id, patterns)
        if ok:
            print(f"  [voice-model] ready: {repo_id}")
        else:
            print(f"  [voice-model] still incomplete after retries - the model "
                  f"loader will attempt its own download next: {repo_id}")
    except Exception as e:  # noqa: BLE001 - best-effort only; never block the actual load
        print(f"  [voice-model] pre-fetch check skipped ({type(e).__name__}: {e})")


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

# Devices to load the model on, tried in order. The first that loads AND passes a tiny validation
# synth wins. The model is always loaded in its native fp32; on cuda, generation runs under
# torch.autocast fp16 instead of half-casting the modules (which left some internal tensors fp32
# and broke the matmuls with "mat1 and mat2 must have the same dtype").
_DEVICES = ("cuda", "cpu")


# --------------------------------------------------------------------------- #
# Chatterbox loading + synthesis
# --------------------------------------------------------------------------- #
def _load_model(device: str):
    """Construct Chatterbox-Turbo on `device` in its native fp32. On cuda we do NOT half-cast the
    submodules (that left internal tensors fp32 and broke matmuls); fp16 is applied at generation
    time via torch.autocast instead."""
    _ensure_model_cached()
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    model = ChatterboxTurboTTS.from_pretrained(device=device)
    if device == "cuda":
        _halve_t3(model.t3)
    return model


def _halve_t3(t3) -> None:
    """Run the T3 token decoder with fp16 weights. Its per-token step is bound by GPU
    memory bandwidth (it re-reads every weight), so half-width weights cut the step time
    by about 40% while the token logits stay within ~1e-2 of fp32 (identical top-1, KL
    < 1e-4). Embeddings are cast in and logits cast back out so sampling stays fp32."""
    t3.tfmr.half()
    t3.speech_head.half()
    t3.speech_emb.half()
    t3.tfmr.register_forward_pre_hook(
        lambda _mod, _args, kwargs: ((), {**kwargs, "inputs_embeds": kwargs["inputs_embeds"].half()}),
        with_kwargs=True)
    t3.speech_head.register_forward_hook(lambda _mod, _args, out: out.float())


def _build_engine():
    """Walk the candidate list and return the first model that loads and can
    actually produce audio on this machine. Returns (model, device, sr, label)."""
    import torch

    if not torch.cuda.is_available() and nvidia_gpu():
        print(f"  [s6] WARNING: torch {torch.__version__} cannot use the NVIDIA GPU - narration "
              f"will run on the CPU. See Settings -> GPU acceleration.")
    last_err: Exception | None = None
    for device in _DEVICES:
        if device == "cuda" and not torch.cuda.is_available():
            continue
        label = f"turbo/{device}"
        try:
            print(f"  loading Chatterbox-Turbo: trying {label} ...")
            model = _load_model(device)
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
    raise RuntimeError(f"Chatterbox-Turbo could not be loaded: {last_err}")


def _generate(model, device: str, text: str, autocast: bool):
    """One synth under inference_mode, freeing CUDA cache afterwards.

    The reference conditioning was computed once up front (`_Engine.prepare_ref`), in
    fp32 and outside autocast: running it inside the fp16 region crashes s3tokenizer's
    log_mel_spectrogram with "expected scalar type Half but found Double". `autocast`
    is therefore only on for the no-reference path on cuda."""
    import torch
    import contextlib

    region = (torch.autocast("cuda", dtype=torch.float16)
              if autocast else contextlib.nullcontext())
    with torch.inference_mode(), region:
        wav = model.generate(text)
    if device == "cuda":
        torch.cuda.empty_cache()
    return wav


class _Engine:
    """Holds the chosen model and a lazily-loaded CPU fallback for per-line OOM."""

    def __init__(self):
        self.model, self.device, self.sr, self.label = _build_engine()
        self._cpu = None  # lazy Turbo-on-CPU for OOM recovery
        self._ref = None  # float32/mono/24k voice-reference wav the models are conditioned on

    def prepare_ref(self, ref_path: str | None, chapter_id: str) -> None:
        """Preprocess the voice-reference clip and condition the model on it, ONCE per
        run; every line then clones the voice without re-reading the clip."""
        if not ref_path:
            return
        tmp = chapter_work_dir(chapter_id) / "_voice_ref_f32.wav"
        self._ref = _preprocess_ref(ref_path, tmp)
        print(f"    prepared float32 voice ref -> {tmp}")
        self._condition(self.model)

    def _condition(self, model) -> None:
        import torch

        with torch.inference_mode():
            model.prepare_conditionals(self._ref)

    def _cpu_model(self):
        if self._cpu is None:
            if self.device == "cpu":
                self._cpu = self.model
            else:
                print("    loading CPU fallback model (Chatterbox-Turbo) ...")
                self._cpu = _load_model("cpu")
                if self._ref:
                    self._condition(self._cpu)
        return self._cpu

    def synth(self, text):
        """Returns (wav_tensor, sample_rate, device_used)."""
        import torch

        try:
            wav = _generate(self.model, self.device, text, autocast=self.device == "cuda" and not self._ref)
            return wav, self.sr, self.device
        except torch.cuda.OutOfMemoryError:
            print("    CUDA OOM on this line; retrying it on CPU")
            torch.cuda.empty_cache()
            cpu = self._cpu_model()
            wav = _generate(cpu, "cpu", text, autocast=False)
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


def _save_wav(wav: np.ndarray, sr: int, out: Path) -> float:
    """Write the mono float32 `wav` to `out` and return its real duration in seconds.

    Uses `soundfile` (already a dep for `_preprocess_ref` above) instead of
    `torchaudio.save`: recent torchaudio releases dropped their built-in
    sox/soundfile I/O backends and require the separate `torchcodec` package
    for save()/load(), which isn't installed by requirements.txt/setup.bat.
    """
    import soundfile as sf

    sf.write(str(out), wav, sr, subtype="FLOAT")
    return len(wav) / float(sr)


# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    audio_dir = chapter_work_dir(m.chapter_id) / "audio"
    clear_directory(audio_dir)   # the voice of the previous run, including lines that no longer exist
    (chapter_work_dir(m.chapter_id) / "_voice_ref_f32.wav").unlink(missing_ok=True)

    settings = _load_media_settings(m.chapter_id)
    tts = settings.get("tts") or {}
    if tts.get("engine") == "kokoro":
        return tts_kokoro.run(m, audio_dir, tts.get("kokoro_voice") or tts_kokoro.DEFAULT_VOICE,
                              tones.delivery(settings.get("tone")).speed)

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
    engine.prepare_ref(ref, m.chapter_id)
    panels = m.kept_panels()
    spoken = 0
    for panel in panels:
        line = (panel.script_line or "").strip()
        if not line:
            panel.audio, panel.duration_s = None, 0
            continue

        mood = pacing.pace_of(panel.emotion, line)
        used = engine.device

        def speak(sentence: str, speed: float) -> np.ndarray:
            nonlocal used
            wav, _, used = engine.synth(sentence)
            return pacing.stretch(wav.detach().to("cpu").numpy().reshape(-1), engine.sr, speed)

        wav = pacing.voice_line(line, mood, speak, engine.sr, tones.delivery(settings.get("tone")).speed)
        out = audio_dir / f"{panel.id}.wav"
        dur = _save_wav(wav, engine.sr, out)

        panel.audio = str(out)
        panel.duration_s = round(dur, 2)
        spoken += 1
        print(f"    [{panel.order:03d}] ({mood}) {dur:5.2f}s on {used}")

    total = sum(p.duration_s or 0 for p in panels)
    print(f"  voiced {spoken}/{len(panels)} panels with Chatterbox "
          f"[{engine.label}], ~{total:.1f}s total -> {audio_dir}")
    record_usage("audio", engine.label, engine.device == "cuda")
    m.stage = "audio"
    return m
