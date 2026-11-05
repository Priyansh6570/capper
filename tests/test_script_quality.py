"""The script step's quality modes: what each sends to Claude, what is cached, and where the mode is kept."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import claude_engine
import panel_inputs
import projects
import quality
import script_usage
from manifest import Panel
from tests.support import ApiProjects, IsolatedProjects

BUBBLE = {"text": "Hello there", "box": [0.1, 0.2, 0.6, 0.4]}


def make_panels(folder: Path, count: int = 3, size: tuple[int, int] = (800, 1600)) -> list[Panel]:
    folder.mkdir(parents=True, exist_ok=True)
    noise = np.random.default_rng(1)
    panels = []
    for order in range(1, count + 1):
        path = folder / f"P{order:03d}.png"
        Image.fromarray(noise.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)).save(path)
        panels.append(Panel(id=f"P{order:03d}", bbox=[0.0, 0.0, float(size[0]), float(size[1])], order=order, crop=str(path)))
    return panels


def image_sizes(content: list[dict]) -> list[tuple[int, int]]:
    return [Image.open(io.BytesIO(base64.b64decode(b["source"]["data"]))).size for b in content if b["type"] == "image"]


def texts_of(content: list[dict]) -> str:
    return "\n".join(b["text"] for b in content if b["type"] == "text")


class PanelCacheTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.panels = make_panels(self.tmp / "panels")
        self.cache = panel_inputs.PanelCache(self.tmp / "claude")

    def test_text_is_read_once_and_reused(self):
        with mock.patch.object(panel_inputs.ocr, "read_panel", return_value=[BUBBLE]) as read:
            first = self.cache.texts(self.panels)
            again = panel_inputs.PanelCache(self.tmp / "claude").texts(self.panels)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(first, again)
        self.assertEqual(first["P001"], [BUBBLE])

    def test_a_changed_panel_is_read_again(self):
        with mock.patch.object(panel_inputs.ocr, "read_panel", return_value=[BUBBLE]) as read:
            self.cache.texts(self.panels)
            Image.new("RGB", (400, 400)).save(self.panels[1].crop)
            self.cache.texts(self.panels)
        self.assertEqual(read.call_count, 4)

    def test_downscaled_copies_fit_the_side_and_are_kept(self):
        data = self.cache.image(self.panels[0], 512)
        self.assertEqual(max(Image.open(io.BytesIO(data)).size), 512)
        with mock.patch.object(panel_inputs.Image, "open", side_effect=AssertionError("resized again")):
            self.assertEqual(self.cache.image(self.panels[0], 512), data)

    def test_the_original_panel_is_untouched(self):
        before = Path(self.panels[0].crop).read_bytes()
        self.cache.image(self.panels[0], 256)
        self.assertEqual(Path(self.panels[0].crop).read_bytes(), before)


class SideTest(IsolatedProjects):
    def test_the_balanced_side_is_kept_inside_what_claude_accepts(self):
        self.assertEqual(quality.balanced_side(100), quality.MIN_BALANCED_SIDE)
        self.assertEqual(quality.balanced_side(9000), quality.MAX_IMAGE_SIDE)
        self.assertEqual(quality.balanced_side("junk"), quality.DEFAULT_BALANCED_SIDE)

    def test_only_lite_sends_no_images(self):
        self.assertEqual(quality.image_side(quality.FULL, 700), 1024)
        self.assertEqual(quality.image_side(quality.BALANCED, 700), 700)
        self.assertIsNone(quality.image_side(quality.LITE, 700))


class BatchContentTest(IsolatedProjects):
    def content(self, mode: str, side: int | None) -> list[dict]:
        panels = make_panels(self.tmp / "panels")
        texts = {p.id: [BUBBLE] for p in panels}
        return claude_engine._batch_content(panels, "ctx", [], 1, 1, 1, mode, panel_inputs.PanelCache(self.tmp / "claude"),
                                            side, texts)

    def test_full_sends_the_images_alone(self):
        content = self.content(quality.FULL, 1024)
        self.assertEqual(image_sizes(content), [(512, 1024)] * 3)
        self.assertNotIn("Hello there", texts_of(content))

    def test_balanced_sends_smaller_images_with_their_text_and_place(self):
        content = self.content(quality.BALANCED, 512)
        self.assertEqual(image_sizes(content), [(256, 512)] * 3)
        self.assertIn("P002:\n[10,20,60,40] Hello there", texts_of(content))

    def test_lite_sends_no_images_and_says_so(self):
        content = self.content(quality.LITE, None)
        self.assertEqual(len(content), 1)
        self.assertIn("You have no images", content[0]["text"])
        self.assertIn("P003:\n[10,20,60,40] Hello there", content[0]["text"])

    def test_no_image_exceeds_the_cap(self):
        panels = make_panels(self.tmp / "tall", count=1, size=(900, 4000))
        sent = claude_engine._image_block(panel_inputs.PanelCache(self.tmp / "c").image(panels[0], quality.balanced_side(9000)))
        self.assertLessEqual(max(image_sizes([sent])[0]), quality.MAX_IMAGE_SIDE)


class RunChapterTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.panels = make_panels(self.tmp / "panels", count=4, size=(200, 300))
        self.calls: list[list[dict]] = []
        patches = (mock.patch.object(claude_engine.claude_cli, "call", side_effect=self.fake_call),
                   mock.patch.object(panel_inputs.ocr, "read_panel", return_value=[BUBBLE]))
        self.read = patches[1].start()
        patches[0].start()
        for patch in patches:
            self.addCleanup(patch.stop)

    def fake_call(self, content, schema, model, work_dir, *, usage, **kwargs):
        self.calls.append(content)
        usage["input"] += 10
        if schema is claude_engine.BREAKDOWN_SCHEMA:
            ids = re.findall(r"^(P\d{3}):", texts_of(content), re.M)
            return {"catalogue": [{"id": i, "description": "d", "kind": "scene", "has_bubble": False, "subjects": []} for i in ids],
                    "scenes": [{"panel_ids": ids, "location": "l", "characters_present": [], "what_happens": "w", "wants": [],
                                "objects": [], "importance": "normal", "feelings": "f", "stakes": "s", "world_info": ""}],
                    "characters": [], "uncertain": [], "chapter_summary": "summary"}
        return {"blocks": [{"narration": "A line.", "pace": "calm", "mode": "sequence",
                            "frames": [{"panel_id": f"P{i:03d}", "subject": -1, "flop": False}]} for i in (1, 3)]}

    def generate(self, mode: str, **kwargs) -> dict:
        folder = self.tmp / "claude"
        return claude_engine.run_chapter(
            panels=self.panels, work_dir=folder, state_path=folder / "state.json", memory_path=self.tmp / "memory.json",
            ch_no=1, brief="", style="", models={"comprehension": "opus", "narration": "sonnet"}, min_frame_s=3,
            chars_per_sec=15, mode=mode, balanced_side=512, on_event=lambda event: None, on_limit=lambda at: None,
            on_proc=lambda proc: None, **kwargs)

    def state(self) -> dict:
        return json.loads((self.tmp / "claude" / "state.json").read_text(encoding="utf-8"))

    def test_lite_needs_one_reading_call_and_one_writing_call(self):
        self.generate(quality.LITE)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.state()["mode"], quality.LITE)
        self.assertIn("do not invent visual action", self.calls[1][0]["text"])

    def test_the_other_modes_keep_the_same_script_prompt(self):
        self.generate(quality.FULL)
        self.generate(quality.BALANCED)
        full, balanced = (call[0]["text"] for call in self.calls if "recap narration" in call[0]["text"])
        self.assertEqual(full, balanced)

    def test_another_mode_starts_over_but_reuses_the_text_already_read(self):
        self.generate(quality.BALANCED)
        read = self.read.call_count
        self.generate(quality.LITE)
        self.assertEqual(self.read.call_count, read)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.state()["mode"], quality.LITE)

    def test_an_interrupted_run_in_the_same_mode_resumes(self):
        self.generate(quality.LITE)
        state = self.state()
        del state["narration"], state["memory_applied"]
        (self.tmp / "claude" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.generate(quality.LITE)
        self.assertEqual(len(self.calls), 3)


class ChapterModeTest(ApiProjects):
    def mode(self, n: int = 1) -> str:
        return projects.chapter_quality(projects.load(self.slug), n)

    def test_a_chapter_follows_the_project_until_it_sets_its_own(self):
        self.assertEqual(self.mode(), quality.FULL)
        projects.update_settings(self.slug, {"claude": {"quality": "lite"}})
        self.assertEqual(self.mode(), quality.LITE)
        projects.set_chapter_quality(self.slug, 1, "balanced")
        self.assertEqual((self.mode(1), self.mode(2)), (quality.BALANCED, quality.LITE))
        projects.set_chapter_quality(self.slug, 1, None)
        self.assertEqual(self.mode(1), quality.LITE)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            projects.set_chapter_quality(self.slug, 1, "tiny")
        reply = self.client.post(f"/api/projects/{self.slug}/chapters/1/quality", json={"quality": "tiny"})
        self.assertEqual(reply.status_code, 400)

    def test_the_route_sets_the_mode(self):
        reply = self.client.post(f"/api/projects/{self.slug}/chapters/2/quality", json={"quality": "lite"})
        self.assertEqual(reply.get_json()["chapter"]["quality"], "lite")
        self.assertEqual(self.mode(2), quality.LITE)

    def test_the_mode_a_script_was_made_with_is_kept_in_the_chapter(self):
        projects.record_script_mode(self.slug, 3, "balanced")
        self.assertEqual(projects.get_chapter(projects.load(self.slug), 3)["script_mode"], "balanced")
        self.assertEqual(projects.get_chapter(projects.load(self.slug), 4)["script_mode"], None)

    def test_project_settings_are_kept_inside_their_ranges(self):
        saved = projects.update_settings(self.slug, {"claude": {"quality": "nonsense", "balanced_max_side": 99999}})
        self.assertEqual((saved["settings"]["claude"]["quality"], saved["settings"]["claude"]["balanced_max_side"]),
                         (quality.FULL, quality.MAX_IMAGE_SIDE))


class UsageLogTest(IsolatedProjects):
    def test_a_line_is_added_per_run_with_the_mode_and_tokens(self):
        path = self.tmp / "log" / "script_usage.jsonl"
        usage = {"input": 5, "output": 7, "cache_read": 1, "cache_write": 2, "cost_usd": 0.5, "seconds": 3.0}
        script_usage.record(path, project="p", chapter="p_ch1", mode="lite", panels=9, usage=usage)
        script_usage.record(path, project="p", chapter="p_ch2", mode="full", panels=4, usage=usage)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(l["chapter"], l["mode"], l["input"], l["output"]) for l in lines],
                         [("p_ch1", "lite", 5, 7), ("p_ch2", "full", 5, 7)])


class AutoShortsPauseTest(IsolatedProjects):
    def test_a_project_that_had_automatic_shorts_on_has_them_paused_once(self):
        slug = self.new_project()["slug"]
        path = projects.project_dir(slug) / "project.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema"] = 3
        document["settings"]["shorts"]["auto_make"] = True
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertFalse(projects.load(slug)["settings"]["shorts"]["auto_make"])
        projects.update_settings(slug, {"shorts": {"auto_make": True}})
        self.assertTrue(projects.load(slug)["settings"]["shorts"]["auto_make"])
        self.assertTrue((projects.project_dir(slug) / "project.json.v3").exists())
