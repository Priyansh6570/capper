"""The manifest keeps loading files written by earlier versions; the orchestrator runs what is left."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import manifest
import orchestrator
from manifest import Manifest, Page, Panel

LEGACY_PANEL = {"id": "L000", "bbox": [0, 10, 1000, 300], "order": 0, "crop": "frame_000.png", "speaker": None,
                "dialogue": None, "has_person": True, "has_face": True, "person_count": 1, "face_count": 1,
                "is_text_only": False, "white_fraction": 0.2, "crop_clean": None, "frame_kind": "closeup",
                "selected": True, "context": "a hero", "emotion": "tense", "script_line": "He stood.",
                "audio": "a.wav", "duration_s": 2.5}


def write_manifest(folder: Path, **overrides) -> Path:
    document = {"chapter_id": "ch", "source_pdf": "x.pdf", "work_dir": str(folder), "stage": "clean",
                "doc_type": "webtoon",
                "pages": [{"index": 1, "image": "strip.png", "clean_image": None, "panels": [dict(LEGACY_PANEL)]}],
                "beats": [dict(LEGACY_PANEL)], **overrides}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return folder


class LegacyManifestTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="manifest-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_fields_of_the_removed_stages_are_ignored(self):
        loaded = Manifest.load(write_manifest(self.folder))
        beat = loaded.beats[0]
        self.assertEqual((beat.id, beat.crop, beat.emotion, beat.script_line, beat.audio, beat.duration_s),
                         ("L000", "frame_000.png", "tense", "He stood.", "a.wav", 2.5))
        self.assertFalse(hasattr(beat, "has_face"))
        self.assertEqual(loaded.pages[0].panels[0].bbox, [0, 10, 1000, 300])
        self.assertFalse(hasattr(loaded.pages[0], "clean_image"))

    def test_markers_of_the_removed_stages_map_onto_the_current_ones(self):
        for old, now in (("clean", "frames"), ("context", "pages"), ("match", "pages"), ("script", "pages"),
                         ("vision", "pages"), ("panels", "pages")):
            with self.subTest(marker=old):
                self.assertEqual(Manifest.load(write_manifest(self.folder, stage=old)).stage, now)

    def test_current_and_unknown_markers(self):
        self.assertEqual(Manifest.load(write_manifest(self.folder, stage="audio")).stage, "audio")
        self.assertEqual(Manifest.load(write_manifest(self.folder, stage="selection")).stage, "init")

    def test_a_manifest_with_no_pages_or_beats_loads(self):
        folder = write_manifest(self.folder, pages=[], beats=[])
        loaded = Manifest.load(folder)
        self.assertEqual((loaded.pages, loaded.beats, loaded.kept_panels()), ([], [], []))

    def test_save_and_load_round_trip_without_the_old_fields(self):
        loaded = Manifest.load(write_manifest(self.folder))
        loaded.save()
        saved = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved["beats"][0]), {"id", "bbox", "order", "crop", "emotion", "script_line", "audio", "duration_s"})
        self.assertEqual(Manifest.load(self.folder), loaded)

    def test_kept_panels_are_the_beats_in_order(self):
        m = Manifest("c", "", str(self.folder), beats=[Panel("b", [0, 0, 1, 1], 1), Panel("a", [0, 0, 1, 1], 0)])
        self.assertEqual([p.id for p in m.kept_panels()], ["b", "a"])


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        write_manifest(self.folder, stage="clean")
        self.ran = []

        def fake_import(name: str):
            stage = mock.Mock()
            stage.run.side_effect = lambda m: (self.ran.append(name), setattr(m, "stage", self.marker[name]), m)[-1]
            return stage

        self.marker = {"stages.s6_tts": "audio", "stages.s7_assemble": "video"}
        patcher = mock.patch.object(orchestrator.importlib, "import_module", fake_import)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_orchestrator(self, **kwargs):
        with config.chapter_scope(self.folder, self.folder / "out"):
            orchestrator.run("ch", **kwargs)

    def test_an_exported_chapter_runs_narration_then_video(self):
        self.run_orchestrator()
        self.assertEqual(self.ran, ["stages.s6_tts", "stages.s7_assemble"])
        self.assertEqual(Manifest.load(self.folder).stage, "video")

    def test_to_stops_after_that_stage(self):
        self.run_orchestrator(from_stage="audio", to_stage="audio")
        self.assertEqual(self.ran, ["stages.s6_tts"])
        self.assertEqual(Manifest.load(self.folder).stage, "audio")

    def test_from_video_skips_narration(self):
        self.run_orchestrator(from_stage="video")
        self.assertEqual(self.ran, ["stages.s7_assemble"])

    def test_a_chapter_that_is_done_runs_nothing(self):
        write_manifest(self.folder, stage="video")
        self.run_orchestrator()
        self.assertEqual(self.ran, [])

    def test_a_chapter_without_a_manifest_says_to_export_it(self):
        (self.folder / "manifest.json").unlink()
        with self.assertRaisesRegex(SystemExit, "Export the chapter"):
            self.run_orchestrator()

    def test_the_stages_and_markers_agree(self):
        self.assertEqual([produces for produces, _ in orchestrator.STAGES], manifest.STAGE_ORDER[-2:])


if __name__ == "__main__":
    unittest.main()
