"""The call-to-action clip: its script and plan, the s9 stage's beats, and where a merge places it."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cta
from services import merging
from services.common import Refused
from stages import s9_cta


def info(width=1920, height=1080, rate="48000", duration=10.0):
    return {"video": {"width": width, "height": height, "codec_name": "h264"},
            "audio": {"codec_name": "aac", "sample_rate": rate, "channels": 1}, "duration": duration, "video_duration": duration}


class ScriptAndPlan(unittest.TestCase):
    def test_parse_script_reads_pace_and_skips_blank_rows(self):
        lines = cta.parse_script("tense: Hello there.\n\nJust words\nunknown: kept whole\n")
        self.assertEqual([(l["pace"], l["text"]) for l in lines],
                         [("tense", "Hello there."), ("calm", "Just words"), ("calm", "unknown: kept whole")])

    def test_default_script_parses_and_needs_two_frames_per_line_but_the_last(self):
        script = cta.parse_script(cta.DEFAULT_SCRIPT)
        self.assertGreaterEqual(len(script), 4)
        self.assertEqual(cta.frames_needed(script), cta.FRAMES_PER_LINE * (len(script) - 1))

    def test_plan_spreads_frames_in_order_and_ends_on_the_card(self):
        script = [{"text": f"l{i}", "pace": "calm"} for i in range(3)]
        plan = cta.make_plan(script, ["a", "b", "c", "d"], "card.png")
        self.assertEqual([l["frames"] for l in plan["lines"]], [["a", "b"], ["c", "d"], ["card.png"]])


class Stage(unittest.TestCase):
    def test_beats_drop_missing_frames_and_fit_keeps_one_per_few_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in "abcd":
                f = Path(tmp) / f"{name}.png"
                f.write_bytes(b"x")
                files.append(str(f))
            plan = {"lines": [{"text": "one", "pace": "calm", "frames": files + [str(Path(tmp) / "gone.png")]}]}
            panels, mapping = s9_cta.beats_of(plan)
            self.assertEqual(len(mapping[1]["frames"]), 4)
            panels[0].duration_s = 5.2          # about two frames' worth
            s9_cta.fit_frames(panels, mapping)
            self.assertEqual(len(mapping[1]["frames"]), 2)
            self.assertEqual(panels[0].crop, mapping[1]["frames"][0]["image"])

    def test_small_quality_is_raised_to_1080p_and_big_one_kept(self):
        self.assertEqual(s9_cta.full_hd({"quality": "tiny"})["quality"], "standard")
        self.assertEqual(s9_cta.full_hd({"quality": "high"})["quality"], "high")


class MergePlacement(unittest.TestCase):
    def prepare(self, chapters, cta_after, probes):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            clip = folder / "cta.mp4"
            clip.write_bytes(b"x")
            with mock.patch.object(merging.projects, "get_chapter", return_value={}), \
                 mock.patch.object(merging.projects, "load", return_value={}), \
                 mock.patch.object(merging.chapter_backup, "restore"), \
                 mock.patch.object(merging.projects, "chapter_video_path", side_effect=lambda s, n: folder / f"{n}.mp4"), \
                 mock.patch.object(merging.projects, "merged_dir", return_value=folder), \
                 mock.patch.object(merging.cta, "clip_path", return_value=clip), \
                 mock.patch.object(merging.branding, "probe", side_effect=probes), \
                 mock.patch.object(merging.branding, "normalized_clip", return_value=clip) as normalize, \
                 mock.patch.object(merging.gpu, "ffmpeg_path", return_value="ffmpeg"):
                for n in chapters:
                    (folder / f"{n}.mp4").write_bytes(b"x")
                plan = merging.prepare_merge("p", chapters, cta_after)
                listing = (folder / "_concat_list.txt").read_text(encoding="utf-8") if (folder / "_concat_list.txt").exists() else ""
                return plan, listing, normalize

    def test_clip_goes_right_after_the_chosen_chapter_and_the_merge_stays_a_copy(self):
        plan, listing, normalize = self.prepare([4, 5, 6], 5, [info(), info(), info(), info(duration=30)])
        order = [Path(row.split("'")[1]).name for row in listing.splitlines()]
        self.assertEqual(order, ["4.mp4", "5.mp4", "cta.mp4", "6.mp4"])
        self.assertIn("-c", plan.cmd)
        self.assertEqual(plan.cmd[plan.cmd.index("-c") + 1], "copy")
        self.assertIn("_cta5_ch4-5-6.mp4", plan.out.name)
        normalize.assert_called_once()

    def test_after_the_last_chapter_puts_it_at_the_end(self):
        _, listing, _ = self.prepare([4, 5], 5, [info(), info(), info()])
        self.assertTrue(listing.splitlines()[-1].endswith("cta.mp4'"))

    def test_chapter_not_in_the_merge_is_refused(self):
        with self.assertRaises(Refused):
            self.prepare([4, 5], 9, [info(), info()])

    def test_without_cta_the_merge_is_unchanged(self):
        plan, listing, normalize = self.prepare([4, 5], None, [info(), info()])
        self.assertEqual(len(listing.splitlines()), 2)
        self.assertNotIn("_cta", plan.out.name)
        normalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
