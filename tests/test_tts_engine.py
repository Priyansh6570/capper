"""Narration: Chatterbox-Turbo on the GPU, then on the CPU, with Kokoro as the per-project alternative."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import predownload_tts_models
import torch

import config
import tones
from manifest import Manifest, Panel
from stages import s6_tts


class FakeModel:
    sr = 24000

    def __init__(self, device: str, fails: bool = False):
        self.device, self.fails = device, fails
        self.generated: list[tuple[tuple, dict]] = []

    def generate(self, *args, **kwargs):
        self.generated.append((args, kwargs))
        if self.fails:
            raise RuntimeError("cannot synthesize")
        return torch.zeros(1, 2400)


class EngineChainTest(unittest.TestCase):
    def build(self, cuda: bool, broken: tuple[str, ...] = (), unusable: tuple[str, ...] = ()):
        loads: list[str] = []

        def load(device: str):
            loads.append(device)
            if device in broken:
                raise RuntimeError(f"{device} out of memory")
            return FakeModel(device, fails=device in unusable)

        with mock.patch.object(s6_tts, "_load_model", side_effect=load), \
                mock.patch.object(torch.cuda, "is_available", return_value=cuda), \
                mock.patch.object(torch.cuda, "empty_cache"):
            return s6_tts._build_engine(), loads

    def test_the_gpu_is_preferred_when_it_works(self):
        (model, device, sr, label), loads = self.build(cuda=True)
        self.assertEqual((device, label, sr, loads), ("cuda", "turbo/cuda", 24000, ["cuda"]))

    def test_a_gpu_that_fails_to_load_falls_back_to_the_cpu(self):
        (_, device, _, label), loads = self.build(cuda=True, broken=("cuda",))
        self.assertEqual((device, label, loads), ("cpu", "turbo/cpu", ["cuda", "cpu"]))

    def test_a_gpu_model_that_cannot_synthesize_falls_back_too(self):
        (_, device, _, _), loads = self.build(cuda=True, unusable=("cuda",))
        self.assertEqual((device, loads), ("cpu", ["cuda", "cpu"]))

    def test_without_cuda_only_the_cpu_is_tried(self):
        (_, device, _, _), loads = self.build(cuda=False)
        self.assertEqual((device, loads), ("cpu", ["cpu"]))

    def test_the_failure_says_what_went_wrong(self):
        with self.assertRaisesRegex(RuntimeError, "Chatterbox-Turbo could not be loaded.*cpu out of memory"):
            self.build(cuda=True, broken=("cuda", "cpu"))


class ModelTest(unittest.TestCase):
    def load(self, device: str):
        model = mock.Mock()
        with mock.patch.object(s6_tts, "_ensure_model_cached") as cached, \
                mock.patch.object(s6_tts, "_halve_t3") as halve, \
                mock.patch("chatterbox.tts_turbo.ChatterboxTurboTTS.from_pretrained", return_value=model) as loader:
            result = s6_tts._load_model(device)
        return result, model, cached, halve, loader

    def test_the_gpu_model_runs_its_decoder_in_half_precision(self):
        result, model, cached, halve, loader = self.load("cuda")
        self.assertIs(result, model)
        cached.assert_called_once_with()
        loader.assert_called_once_with(device="cuda")
        halve.assert_called_once_with(model.t3)

    def test_the_cpu_model_stays_in_full_precision(self):
        _, _, _, halve, loader = self.load("cpu")
        loader.assert_called_once_with(device="cpu")
        halve.assert_not_called()

    def test_synthesis_passes_the_text_and_temperature(self):
        model = FakeModel("cpu")
        s6_tts._generate(model, "cpu", "Hello there.", autocast=False)
        self.assertEqual(model.generated, [(("Hello there.",), {"temperature": 0.8})])

    def test_setup_downloads_the_turbo_repo_only(self):
        repo, patterns = predownload_tts_models.MODEL
        self.assertEqual(repo, "ResembleAI/chatterbox-turbo")
        self.assertIn("*.safetensors", patterns)

    def test_a_tone_only_changes_the_speaking_speed(self):
        self.assertEqual(tones.Delivery._fields, ("speed",))
        self.assertEqual(tones.delivery(None).speed, 1.0)
        self.assertLess(tones.delivery("epic").speed, 1.0)


class FakeEngine:
    sr, device, label = 24000, "cpu", "turbo/cpu"

    def __init__(self):
        self.said: list[str] = []

    def prepare_ref(self, ref_path, chapter_id):
        self.ref = ref_path

    def synth(self, text, temperature=0.8):
        self.said.append(text)
        return torch.full((2400 * len(text.split()),), 0.1), self.sr, "cpu"


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tts-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        scope = config.chapter_scope(self.tmp / "work", self.tmp / "out")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        (self.tmp / "work").mkdir()
        patch = mock.patch.object(tempfile, "gettempdir", return_value=str(self.tmp / "temp"))   # the sentence cache
        patch.start()
        self.addCleanup(patch.stop)
        self.beats = [Panel("L000", [0, 0, 1, 1], 0, script_line="He walked in. Nobody noticed.", emotion="tense"),
                      Panel("L001", [0, 0, 1, 1], 1, script_line="   "),
                      Panel("L002", [0, 0, 1, 1], 2, script_line="Then the door slammed.", emotion="reveal")]
        self.manifest = Manifest("ch", "", str(self.tmp / "work"), stage="frames", beats=self.beats)

    def run_stage(self, media_settings: dict | None = None, engine: FakeEngine | None = None) -> Manifest:
        engine = engine or FakeEngine()
        with mock.patch.object(s6_tts, "_Engine", return_value=engine), \
                mock.patch.object(s6_tts, "_load_media_settings", return_value=media_settings or {}), \
                mock.patch.object(s6_tts, "record_usage"):
            return s6_tts.run(self.manifest)

    def test_each_line_with_text_gets_a_clip_and_the_marker_advances(self):
        m = self.run_stage()
        self.assertEqual(m.stage, "audio")
        voiced, silent, last = m.beats
        for beat in (voiced, last):
            self.assertTrue(Path(beat.audio).is_file())
            self.assertGreater(beat.duration_s, 0)
        self.assertEqual((silent.audio, silent.duration_s), (None, 0))

    def test_a_line_is_voiced_sentence_by_sentence(self):
        engine = FakeEngine()
        self.run_stage(engine=engine)
        self.assertEqual(engine.said, ["He walked in.", "Nobody noticed.", "Then the door slammed."])

    def test_a_project_can_choose_kokoro(self):
        settings = {"tts": {"engine": "kokoro", "kokoro_voice": "am_adam"}, "tone": "epic"}
        with mock.patch.object(s6_tts.tts_kokoro, "run", side_effect=lambda m, audio_dir, voice, speed: m) as kokoro:
            self.run_stage(settings)
        kokoro.assert_called_once()
        self.assertEqual(kokoro.call_args.args[2], "am_adam")
        self.assertAlmostEqual(kokoro.call_args.args[3], tones.delivery("epic").speed)

    def test_clips_of_an_earlier_run_are_cleared(self):
        audio = self.tmp / "work" / "audio"
        audio.mkdir()
        stale = audio / "gone.wav"
        stale.write_bytes(b"x")
        self.run_stage()
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    np.seterr(all="ignore")
    unittest.main()
