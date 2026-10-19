"""The Shorts pipeline: picking and checking segments, the plan's files, the portrait render and the batch."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import threading
import zipfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import batch
import config
import library
import projects
import shorts
from manifest import Manifest, Panel
from services import jobs
from stages import s7_assemble, s8_shorts
from tests.support import ApiProjects, touch


def beats(*seconds: float) -> list[Panel]:
    return [Panel(id=f"L{i:03d}", bbox=[0, 0, 1, 1], order=i, script_line=f"line {i}", duration_s=s)
            for i, s in enumerate(seconds, start=1)]


def length(run: list[Panel]) -> float:
    return sum(b.duration_s for b in run)


def proposal(first: int, last: int, title: str = "A title", description: str = "A description") -> dict:
    return {"first_line": first, "last_line": last, "title": title, "description": description, "reason": "Strong."}


class AcceptTest(unittest.TestCase):
    def test_a_segment_that_fits_becomes_a_plan_entry(self):
        accepted, notes = shorts.accept([proposal(2, 4)], beats(5, 10, 10, 10, 5), length)
        self.assertEqual(notes, [])
        self.assertEqual([(s["id"], s["first_line"], s["last_line"], s["duration_s"]) for s in accepted], [(1, 2, 4, 30.0)])

    def test_nothing_is_added_when_the_model_proposes_nothing(self):
        self.assertEqual(shorts.accept([], beats(10, 10), length), ([], []))

    def test_a_segment_over_a_minute_loses_its_last_lines_until_it_fits(self):
        accepted, _ = shorts.accept([proposal(1, 4)], beats(20, 20, 15, 15), length)
        self.assertEqual((accepted[0]["last_line"], accepted[0]["duration_s"]), (3, 55.0))

    def test_an_overlap_with_an_earlier_segment_is_dropped(self):
        accepted, notes = shorts.accept([proposal(1, 2), proposal(2, 3)], beats(10, 10, 10), length)
        self.assertEqual([s["first_line"] for s in accepted], [1])
        self.assertIn("overlaps", notes[0])

    def test_segments_are_numbered_in_story_order(self):
        accepted, _ = shorts.accept([proposal(4, 5), proposal(1, 2)], beats(10, 10, 10, 10, 10), length)
        self.assertEqual([(s["id"], s["first_line"]) for s in accepted], [(1, 1), (2, 4)])

    def test_a_too_short_segment_and_one_without_a_title_are_dropped(self):
        accepted, notes = shorts.accept([proposal(1, 1), proposal(3, 4, title=" ")], beats(4, 20, 15, 15), length)
        self.assertEqual(accepted, [])
        self.assertEqual(len(notes), 2)

    def test_lines_that_do_not_exist_are_dropped(self):
        self.assertEqual(shorts.accept([proposal(8, 9)], beats(10, 10), length)[0], [])


class PlanFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work, self.output = self.tmp / "work", self.tmp / "output"

    def render_one(self, name: str = "short_01.mp4") -> None:
        touch(self.output / "shorts" / name)
        (self.output / "shorts" / "shorts.json").write_text(json.dumps(
            {"shorts": [{"id": 1, "file": name, "title": "T", "description": "D"}]}), encoding="utf-8")

    def test_saving_a_plan_deletes_the_old_shorts_first(self):
        self.render_one()
        shorts.save_plan(self.work, self.output, "c", [{"id": 1, "title": "New"}])
        self.assertEqual(shorts.rendered(self.output), [])
        self.assertFalse((self.output / "shorts").exists())
        self.assertEqual(shorts.read_plan(self.work), [{"id": 1, "title": "New"}])

    def test_an_empty_plan_leaves_the_chapter_with_no_shorts(self):
        self.render_one()
        shorts.save_plan(self.work, self.output, "c", [])
        self.assertEqual((shorts.read_plan(self.work), shorts.rendered(self.output)), ([], []))

    def test_a_short_whose_video_is_gone_is_not_listed(self):
        self.render_one()
        (self.output / "shorts" / "short_01.mp4").unlink()
        self.assertEqual(shorts.rendered(self.output), [])


class PortraitRenderTest(unittest.TestCase):
    def test_the_output_size_follows_the_quality_preset(self):
        self.assertEqual(s7_assemble.LANDSCAPE.output_size(1080), (1920, 1080))
        self.assertEqual(s7_assemble.LANDSCAPE.output_size(720), (1280, 720))
        self.assertEqual(s7_assemble.PORTRAIT.output_size(1080), (1080, 1920))
        self.assertEqual(s7_assemble.PORTRAIT.output_size(720), (720, 1280))

    def test_a_portrait_still_is_composed_on_the_tall_canvas_with_the_frame_filling_its_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            Image.new("RGB", (1000, 800), (200, 30, 30)).save(frame)
            still = s7_assemble._compose_still([str(frame)], canvas=s7_assemble.PORTRAIT)
        self.assertEqual(still.size, (1080, 1920))
        self.assertEqual(still.getpixel((540, 960)), (200, 30, 30))
        self.assertNotEqual(still.getpixel((10, 960)), (200, 30, 30))

    def test_the_filter_graph_runs_at_the_portrait_size(self):
        items = [{"still": "a.png", "frames": ["a"], "dur": 3.0, "start": 0.0, "audio": None, "audio_len": 0.0}]
        graphs, _, _ = s7_assemble._build_filtergraph(items, None, 3.0, "cut", 0.0, None, bg_in=1, out_size=(1080, 1920))
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", "".join(graphs))


class IncrementalIndexTest(unittest.TestCase):
    def test_a_finished_short_is_listed_before_the_next_one_is_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, output = Path(tmp) / "work", Path(tmp) / "output"
            (work / "shorts").mkdir(parents=True)
            plan = [{"id": i, "first_line": i, "last_line": i, "title": f"T{i}", "description": "d", "reason": "r"}
                    for i in (1, 2)]
            (work / "shorts" / "plan.json").write_text(json.dumps({"shorts": plan}), encoding="utf-8")

            def execute(render, out):
                if out.name == "short_02.mp4":
                    raise RuntimeError("ffmpeg failed")
                out.write_bytes(b"mp4")

            with config.chapter_scope(work, output),                     mock.patch.object(s7_assemble, "prepare_render", lambda *args: SimpleNamespace(total=12.0)),                     mock.patch.object(s7_assemble, "execute_render", execute),                     mock.patch.object(s7_assemble, "load_mapping", return_value={}),                     mock.patch.object(s7_assemble, "load_media_settings", return_value={}):
                with self.assertRaises(RuntimeError):
                    s8_shorts.run(Manifest(chapter_id="c", source_pdf="", work_dir=str(work), beats=beats(12, 12)))
            self.assertEqual([s["title"] for s in shorts.rendered(output)], ["T1"])


class CoverLayoutTest(unittest.TestCase):
    def frame(self, tmp: str) -> str:
        path = Path(tmp) / "frame.png"
        Image.new("RGB", (1000, 800), (200, 30, 30)).save(path)
        return str(path)

    def test_a_covering_frame_fills_the_whole_canvas_and_the_watermark_is_above_it(self):
        mark = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        mark.putpixel((10, 10), (255, 255, 255, 255))
        with tempfile.TemporaryDirectory() as tmp:
            still = s7_assemble._compose_still([self.frame(tmp)], wm=mark, canvas=s7_assemble.PORTRAIT, cover=True)
        self.assertEqual(still.getpixel((10, 960)), (200, 30, 30))
        self.assertEqual(still.getpixel((10, 10)), (255, 255, 255))

    def test_a_whole_frame_keeps_the_watermark_behind_it(self):
        mark = Image.new("RGBA", (1080, 1920), (255, 255, 255, 255))
        with tempfile.TemporaryDirectory() as tmp:
            still = s7_assemble._compose_still([self.frame(tmp)], wm=mark, canvas=s7_assemble.PORTRAIT)
        self.assertEqual(still.getpixel((540, 960)), (200, 30, 30))

    def test_a_covering_render_shows_one_frame_at_a_time_and_loads_no_background(self):
        mapping = {1: {"order": 1, "mode": "together", "frames": [{"image": "a.png"}, {"image": "b.png"}]}}
        with mock.patch.object(s7_assemble, "_resolve_video_background") as video:
            plan = s7_assemble.prepare_render(beats(10), mapping, {"background": {"kind": "video", "file": "x"}},
                                              s7_assemble.PORTRAIT, cover=True)
        self.assertEqual([item["layout"] for item in plan.items], ["single", "single"])
        video.assert_not_called()
        self.assertIsNone(plan.video_bg)


class ShortsProgressTest(unittest.TestCase):
    def test_progress_follows_the_short_being_rendered_and_its_ffmpeg_run(self):
        job = jobs.Job("c", "shorts_render", "p", "1", 0)
        for line in ("shorts 2/4: A title", "  render  50%  frame=1/2"):
            jobs._track_shorts_render(job, line)
        self.assertEqual(job.fraction, 0.375)
        self.assertEqual(job.message, "Rendering short 2 of 4: A title")


class RenderStageTest(unittest.TestCase):
    def test_each_planned_segment_is_rendered_from_its_own_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, output = Path(tmp) / "work", Path(tmp) / "output"
            (work / "shorts").mkdir(parents=True)
            plan = [{"id": 1, "first_line": 2, "last_line": 3, "title": "One", "description": "d", "reason": "r"},
                    {"id": 2, "first_line": 5, "last_line": 5, "title": "Two", "description": "d", "reason": "r"}]
            (work / "shorts" / "plan.json").write_text(json.dumps({"shorts": plan}), encoding="utf-8")
            m = Manifest(chapter_id="c", source_pdf="", work_dir=str(work), beats=beats(10, 10, 10, 10, 10))
            seen = []

            def prepare(segment, mapping, settings, canvas, cover):
                seen.append(([b.order for b in segment], canvas, cover))
                return SimpleNamespace(total=20.04 if len(segment) == 2 else 10.0)

            def execute(render, out):
                out.write_bytes(b"mp4")

            with config.chapter_scope(work, output), \
                    mock.patch.object(s7_assemble, "prepare_render", prepare), \
                    mock.patch.object(s7_assemble, "execute_render", execute), \
                    mock.patch.object(s7_assemble, "load_mapping", return_value={}), \
                    mock.patch.object(s7_assemble, "load_media_settings", return_value={}):
                s8_shorts.run(m)
            self.assertEqual(seen, [([2, 3], s7_assemble.PORTRAIT, False), ([5], s7_assemble.PORTRAIT, False)])
            index = json.loads((output / "shorts" / "shorts.json").read_text(encoding="utf-8"))["shorts"]
            self.assertEqual([(s["file"], s["duration_s"], s["title"]) for s in index],
                             [("short_01.mp4", 20.0, "One"), ("short_02.mp4", 10.0, "Two")])
            self.assertEqual(m.stage, "init")


    def test_a_projects_own_shorts_look_replaces_the_main_watermark_and_background(self):
        settings = {"watermark": {"text": "MAIN"}, "background": None, "music": "m.mp3",
                    "shorts": {"layout": "cover",
                               "look": {"watermark": {"text": "SHORTS"}, "background": {"kind": "video"}}}}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            (work / "shorts").mkdir(parents=True)
            (work / "shorts" / "plan.json").write_text(json.dumps({"shorts": [
                {"id": 1, "first_line": 1, "last_line": 1, "title": "t", "description": "d", "reason": "r"}]}),
                encoding="utf-8")
            seen = []

            def prepare(segment, mapping, used, canvas, cover):
                seen.append((used, cover))
                return SimpleNamespace(total=12.0)

            with config.chapter_scope(work, Path(tmp) / "output"), \
                    mock.patch.object(s7_assemble, "prepare_render", prepare), \
                    mock.patch.object(s7_assemble, "execute_render", lambda render, out: out.write_bytes(b"mp4")), \
                    mock.patch.object(s7_assemble, "load_mapping", return_value={}), \
                    mock.patch.object(s7_assemble, "load_media_settings", return_value=settings):
                s8_shorts.run(Manifest(chapter_id="c", source_pdf="", work_dir=str(work), beats=beats(12)))
        used, cover = seen[0]
        self.assertEqual((used["watermark"], used["background"], used["music"], cover),
                         ({"text": "SHORTS"}, {"kind": "video"}, "m.mp3", True))


class ShortsSettingsTest(ApiProjects):
    def test_the_shorts_look_is_off_until_chosen_and_its_sliders_are_clamped(self):
        self.assertEqual(projects.media_settings_snapshot(self.slug, 1)["shorts"], {"layout": "frame", "look": None})
        projects.update_settings(self.slug, {"shorts": {"own_look": True, "layout": "bogus", "watermark": {"enabled": True, "text": "S", "opacity": 9},
                                                         "background": {"mode": "bogus", "dim": 0}}})
        shorts_look = projects.media_settings_snapshot(self.slug, 1)["shorts"]["look"]
        self.assertEqual(projects.media_settings_snapshot(self.slug, 1)["shorts"]["layout"], "frame")
        self.assertEqual((shorts_look["watermark"]["text"], shorts_look["watermark"]["opacity"]), ("S", 1.0))
        self.assertIsNone(shorts_look["background"])
        self.assertIsNone(projects.media_settings_snapshot(self.slug, 1)["watermark"])

    def test_uploads_for_the_shorts_look_land_in_the_shorts_settings(self):
        response = self.client.post(f"/api/projects/{self.slug}/asset", data={
            "kind": "shorts_background", "file": (io.BytesIO(b"png"), "bg.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        settings = projects.load(self.slug)["settings"]
        self.assertEqual(settings["shorts"]["background"]["file"], "assets/shorts_background.png")
        self.assertIsNone(settings["background"]["file"])


class ShortsBatchTest(unittest.TestCase):
    def test_starting_a_batch_does_not_enter_the_chapter(self):
        entered = []
        env = batch.BatchEnv(
            runners={}, stages=batch.SHORTS_STAGES, scope=lambda n: entered.append(n) or contextlib.nullcontext(),
            done_stages=lambda n: [], review_enabled=lambda: False, thresholds=lambda: {}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None)
        chapter = batch._new_chapter(env, 7, list(batch.SHORTS_STAGES))
        self.assertEqual((entered, chapter["stage"]), ([], "shorts_find"))


    def test_the_two_steps_run_in_order_one_chapter_at_a_time_per_step(self):
        events, lock, busy = [], threading.Lock(), {"shorts_find": 0, "shorts_render": 0}
        overlapped = []

        def runner(stage):
            def run(b, n):
                with lock:
                    busy[stage] += 1
                    overlapped.append(busy[stage] > 1)
                threading.Event().wait(0.05)
                events.append((stage, n))
                with lock:
                    busy[stage] -= 1
            return run

        env = batch.BatchEnv(
            runners={stage: runner(stage) for stage in batch.SHORTS_STAGES}, stages=batch.SHORTS_STAGES,
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {})
        with tempfile.TemporaryDirectory() as tmp:
            run = batch.Batch.create(Path(tmp) / "shorts_batch.json", env, {n: list(batch.SHORTS_STAGES) for n in (1, 2)})
            for _ in range(200):
                if run.status == "finished":
                    break
                threading.Event().wait(0.05)
            run.close()
        self.assertEqual(run.status, "finished")
        self.assertFalse(any(overlapped))
        for n in (1, 2):
            self.assertLess(events.index(("shorts_find", n)), events.index(("shorts_render", n)))


class ShortsJobTest(unittest.TestCase):
    def test_the_render_job_runs_the_shorts_stage_and_the_others_the_orchestrator(self):
        command = lambda what: jobs._pipeline_command(SimpleNamespace(what=what, chapter_id="c"))
        self.assertEqual(command("shorts_render")[-4:], ["-m", "stages.s8_shorts", "--chapter", "c"])
        self.assertEqual(command("video")[-2:], ["--from", "video"])
        self.assertEqual(command("voice")[-4:], ["--from", "audio", "--to", "audio"])
        self.assertEqual(command("both")[-2:], ["--from", "audio"])


class ShortsApiTest(ApiProjects):
    def test_chapters_with_shorts_are_listed_with_their_titles_and_descriptions(self):
        output = projects.chapter_output_path(self.slug, 3)
        touch(output / "shorts" / "short_01.mp4")
        (output / "shorts" / "shorts.json").write_text(json.dumps({"shorts": [
            {"id": 1, "file": "short_01.mp4", "title": "T", "description": "D", "duration_s": 30}]}), encoding="utf-8")
        data = self.client.get(f"/api/projects/{self.slug}/shorts").get_json()
        self.assertEqual([(c["n"], c["shorts"][0]["title"]) for c in data["chapters"]], [(3, "T")])
        self.assertIsNone(data["batch"])

    def test_a_chapter_without_a_finished_video_is_skipped_and_nothing_starts(self):
        response = self.client.post(f"/api/projects/{self.slug}/shorts/batch", json={"chapters": [2]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("no finished video", response.get_json()["error"])

    def test_chapters_outside_the_project_are_refused(self):
        response = self.client.post(f"/api/projects/{self.slug}/shorts/batch", json={"chapters": [99]})
        self.assertEqual(response.status_code, 400)


    def test_a_chapters_shorts_download_as_one_zip_with_their_titles(self):
        output = projects.chapter_output_path(self.slug, 3)
        touch(output / "shorts" / "short_01.mp4", b"video")
        (output / "shorts" / "shorts.json").write_text(json.dumps({"shorts": [
            {"id": 1, "file": "short_01.mp4", "title": "T", "description": "D"}]}), encoding="utf-8")
        response = self.client.get(f"/api/projects/{self.slug}/shorts/3/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["short_01.mp4", "titles.txt"])
            self.assertIn("T\nD", archive.read("titles.txt").decode())
        self.assertEqual(self.client.get(f"/api/projects/{self.slug}/shorts/4/zip").status_code, 404)


class ShortsLibraryTest(ApiProjects):
    def test_shorts_are_their_own_file_type_and_can_be_served_and_deleted(self):
        short = touch(projects.chapter_output_path(self.slug, 1) / "shorts" / "short_01.mp4", b"video")
        touch(projects.chapter_output_path(self.slug, 1) / "recap.mp4")
        self.assertEqual(library.kind_files(self.slug, 1, "shorts"), [short])
        self.assertEqual(library.kind_files(self.slug, 1, "video"), [projects.chapter_output_path(self.slug, 1) / "recap.mp4"])
        self.assertEqual(library.servable_file(self.slug, 1, "output/shorts/short_01.mp4"), short)
        library.delete_kind(self.slug, [1], "shorts")
        self.assertFalse(short.exists())


if __name__ == "__main__":
    unittest.main()
