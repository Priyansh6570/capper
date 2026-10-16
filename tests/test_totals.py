"""Cached project totals: correct, and never stale after a chapter changes."""
from __future__ import annotations

import json
import shutil
import time
from unittest import mock

import claude_cli
import projects
import totals
from manifest import Manifest, Panel
from tests.support import IsolatedProjects, touch

LINE = "x" * 40


class TotalsTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.slug = self.new_project(chapters=6)["slug"]

    def voice_chapter(self, n: int, seconds: float = 4.0, lines: int = 1) -> None:
        """A chapter whose narration is voiced: `lines` clips of 40 characters taking `seconds` each."""
        work = projects.chapter_work_dir(self.slug, n)
        beats = []
        for i in range(lines):
            clip = touch(work / "audio" / f"{i}.wav")
            beats.append(Panel(id=f"b{i}", bbox=[0, 0, 1, 1], order=i, script_line=LINE, audio=str(clip), duration_s=seconds))
        Manifest(chapter_id=f"c{n}", source_pdf="", work_dir=str(work), beats=beats).save()

    def claude_chapter(self, n: int, output_tokens: int) -> None:
        usage = {**claude_cli.empty_usage(), "output": output_tokens}
        touch(projects.chapter_work_dir(self.slug, n) / "claude" / "state.json",
              json.dumps({"usage": {"total": usage}}).encode())

    def test_no_audio_means_no_rate_and_no_usage(self):
        self.assertIsNone(totals.speech_rate(self.slug))
        self.assertEqual(totals.claude_usage(self.slug)["output"], 0)

    def test_rate_is_characters_per_second_over_voiced_chapters(self):
        self.voice_chapter(0, seconds=4.0, lines=2)
        self.voice_chapter(3, seconds=2.0)
        self.assertAlmostEqual(totals.speech_rate(self.slug), 120 / 10.0)

    def test_short_clips_do_not_count_towards_the_rate(self):
        work = projects.chapter_work_dir(self.slug, 1)
        clip = touch(work / "audio" / "0.wav")
        Manifest(chapter_id="c1", source_pdf="", work_dir=str(work),
                 beats=[Panel(id="b", bbox=[0, 0, 1, 1], order=0, script_line="hi", audio=str(clip), duration_s=1.0)]).save()
        self.assertIsNone(totals.speech_rate(self.slug))

    def test_claude_usage_is_summed_over_chapters(self):
        self.claude_chapter(0, 100)
        self.claude_chapter(2, 250)
        self.assertEqual(totals.claude_usage(self.slug)["output"], 350)

    def test_untouched_chapters_are_not_scanned(self):
        self.voice_chapter(2)
        with mock.patch.object(totals.Manifest, "load", wraps=totals.Manifest.load) as load:
            totals.speech_rate(self.slug)
        self.assertEqual(load.call_count, 1)

    def test_an_unchanged_chapter_is_read_once(self):
        self.voice_chapter(0)
        totals.speech_rate(self.slug)
        with mock.patch.object(totals.Manifest, "load") as load:
            totals.speech_rate(self.slug)
            totals.claude_usage(self.slug)
        load.assert_not_called()

    def test_new_audio_changes_the_rate(self):
        self.voice_chapter(0, seconds=4.0)
        self.assertAlmostEqual(totals.speech_rate(self.slug), 10.0)
        time.sleep(0.02)
        self.voice_chapter(0, seconds=8.0)
        self.assertAlmostEqual(totals.speech_rate(self.slug), 5.0)

    def test_deleting_the_audio_files_is_noticed(self):
        self.voice_chapter(0, lines=2)
        self.assertIsNotNone(totals.speech_rate(self.slug))
        time.sleep(0.02)
        for clip in (projects.chapter_work_path(self.slug, 0) / "audio").iterdir():
            clip.unlink()
        self.assertIsNone(totals.speech_rate(self.slug))

    def test_new_claude_usage_is_noticed(self):
        self.claude_chapter(0, 100)
        self.assertEqual(totals.claude_usage(self.slug)["output"], 100)
        time.sleep(0.02)
        self.claude_chapter(0, 900)
        self.assertEqual(totals.claude_usage(self.slug)["output"], 900)

    def test_a_deleted_chapter_folder_drops_out_of_the_totals(self):
        self.claude_chapter(0, 100)
        self.claude_chapter(1, 50)
        totals.claude_usage(self.slug)
        shutil.rmtree(projects.chapter_dir(self.slug, 0))
        self.assertEqual(totals.claude_usage(self.slug)["output"], 50)

    def test_the_cache_is_local_even_for_a_shared_project(self):
        self.use_shared_folder()
        slug = self.new_project("Shared Totals")["slug"]
        touch(projects.chapter_work_dir(slug, 0) / "claude" / "state.json", b"{}")
        totals.claude_usage(slug)
        self.assertTrue((self.local / slug / totals.CACHE_FILE).is_file())
        self.assertFalse((self.shared / slug / totals.CACHE_FILE).exists())

    def test_a_corrupt_cache_is_rebuilt(self):
        self.claude_chapter(0, 100)
        totals.claude_usage(self.slug)
        (projects.local_dir(self.slug) / totals.CACHE_FILE).write_text("{not json")
        self.assertEqual(totals.claude_usage(self.slug)["output"], 100)
