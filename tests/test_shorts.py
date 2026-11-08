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

import audio_sample
import batch
import config
import library
import projects
import short_music
import shorts
from manifest import Manifest, Panel
from services import batching, jobs
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
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(s8_shorts, "add_logo")   # the logo is burned in with ffmpeg
        patcher.start()
        self.addCleanup(patcher.stop)

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
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(s8_shorts, "add_logo")   # the logo is burned in with ffmpeg
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_planned_segment_is_rendered_from_its_own_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, output = Path(tmp) / "work", Path(tmp) / "output"
            (work / "shorts").mkdir(parents=True)
            plan = [{"id": 1, "first_line": 2, "last_line": 3, "title": "One", "description": "d", "reason": "r"},
                    {"id": 2, "first_line": 5, "last_line": 5, "title": "Two", "description": "d", "reason": "r"}]
            (work / "shorts" / "plan.json").write_text(json.dumps({"shorts": plan}), encoding="utf-8")
            m = Manifest(chapter_id="c", source_pdf="", work_dir=str(work), beats=beats(10, 10, 10, 10, 10))
            seen = []

            def prepare(segment, mapping, settings, canvas, cover, *music):
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

            def prepare(segment, mapping, used, canvas, cover, *music):
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
        self.assertEqual(projects.media_settings_snapshot(self.slug, 1)["shorts"],
                         {"layout": "frame", "look": None, "sound": None, "library": [], "music_share": 0.4})
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


def scored(chapter: int, first: int, last: int, score: int, title: str = "A title") -> dict:
    return {**proposal(first, last, title), "chapter": chapter, "score": score}


class SelectTest(unittest.TestCase):
    def entry(self, number: int, score: int) -> dict:
        return {"id": number, "first_line": number, "last_line": number, "score": score}

    def test_every_segment_is_kept_without_a_limit(self):
        chosen = shorts.select({3: [self.entry(1, 8)], 6: [self.entry(1, 7), self.entry(2, 9)]}, None)
        self.assertEqual({n: [s["id"] for s in plan] for n, plan in chosen.items()}, {3: [1], 6: [1, 2]})

    def test_a_limit_keeps_the_best_scoring_and_numbers_them_again_in_story_order(self):
        chosen = shorts.select({3: [self.entry(1, 8)], 6: [self.entry(1, 7), self.entry(2, 9)]}, 2)
        self.assertEqual([(n, s["first_line"], s["id"]) for n, plan in chosen.items() for s in plan],
                         [(3, 1, 1), (6, 2, 1)])

    def test_a_chapter_with_nothing_left_is_not_listed(self):
        self.assertEqual(list(shorts.select({3: [self.entry(1, 7)], 6: [self.entry(1, 9)]}, 1)), [6])


class FindRangeTest(unittest.TestCase):
    def find(self, scripts: dict, answer: dict, max_shorts: int | None = None):
        prompts = []

        def call(content, schema, model, cwd, **kwargs):
            prompts.append(content[0]["text"])
            return answer

        with mock.patch.object(shorts.claude_cli, "call", call), \
                mock.patch.object(shorts.claude_cli, "empty_usage", return_value={}):
            found = shorts.find_range(
                scripts=scripts, brief="", model="opus", work=Path(tempfile.gettempdir()), max_shorts=max_shorts,
                seconds_of=lambda n, run: length(run), on_event=print, on_limit=print, on_proc=print, before_call=None)
        return found, prompts

    def test_the_whole_range_is_studied_in_one_call_and_only_strong_moments_in_known_chapters_are_kept(self):
        scripts = {3: beats(10, 10, 10), 4: beats(10, 10), 6: beats(10, 10, 10)}
        answer = {"shorts": [scored(3, 1, 2, 9), scored(4, 1, 2, 5), scored(6, 2, 3, 7), scored(9, 1, 2, 10)]}
        found, prompts = self.find(scripts, answer)
        self.assertEqual(len(prompts), 1)
        self.assertTrue(all(f"=== CHAPTER {n} ===" in prompts[0] for n in scripts))
        self.assertEqual({n: [(s["first_line"], s["last_line"], s["score"]) for s in plan]
                          for n, plan in found["shorts"].items()}, {3: [(1, 2, 9)], 6: [(2, 3, 7)]})

    def test_a_range_with_nothing_strong_gives_no_shorts(self):
        found, _ = self.find({1: beats(10, 10)}, {"shorts": []})
        self.assertEqual(found["shorts"], {})

    def test_the_limit_applies_across_chapters(self):
        scripts = {1: beats(10, 10), 2: beats(10, 10)}
        found, _ = self.find(scripts, {"shorts": [scored(1, 1, 2, 8), scored(2, 1, 2, 10)]}, max_shorts=1)
        self.assertEqual(list(found["shorts"]), [2])

    def test_a_long_range_is_read_in_several_calls(self):
        scripts = {n: beats(10, 10) for n in (1, 2, 3)}
        with mock.patch.object(shorts, "GROUP_CHARS", len(shorts.script_lines(scripts[1])) + 1):
            self.assertEqual(shorts.chapter_groups(scripts), [[1], [2], [3]])
            found, prompts = self.find(scripts, {"shorts": [scored(2, 1, 2, 9)]})
        self.assertEqual(len(prompts), 3)
        self.assertEqual(list(found["shorts"]), [2])


class ShortsSoundTest(ApiProjects):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(s8_shorts, "add_logo")   # the logo is burned in with ffmpeg
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_shorts_use_the_main_sound_until_they_have_their_own(self):
        self.assertIsNone(projects.media_settings_snapshot(self.slug, 1)["shorts"]["sound"])
        projects.update_settings(self.slug, {"shorts": {"own_audio": True, "music": {"mode": "none"},
                                                         "audio": {"narration_vol": 99, "music_vol": 0.1}}})
        snapshot = projects.media_settings_snapshot(self.slug, 1)
        self.assertEqual(snapshot["shorts"]["sound"], {"music": None, "music_off": True,
                                                       "audio": {"narration_vol": 6.0, "music_vol": 0.1}})
        self.assertEqual(snapshot["audio"], {"narration_vol": 2.5, "music_vol": 0.03})

    def test_an_uploaded_track_lands_in_the_shorts_settings_and_can_be_removed(self):
        response = self.client.post(f"/api/projects/{self.slug}/asset", data={
            "kind": "shorts_music", "file": (io.BytesIO(b"mp3"), "theme.mp3")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        settings = projects.load(self.slug)["settings"]
        self.assertEqual((settings["shorts"]["music"]["file"], settings["music"]["file"]),
                         ("assets/shorts_music.mp3", None))
        self.client.delete(f"/api/projects/{self.slug}/asset/shorts_music")
        self.assertEqual(projects.load(self.slug)["settings"]["shorts"]["music"], {"mode": "default", "file": None})
        self.assertIsNone(projects.find_asset(self.slug, "shorts_music"))

    def test_the_render_stage_uses_the_shorts_own_sound(self):
        settings = {"music": "main.mp3", "audio": {"narration_vol": 2.5, "music_vol": 0.03},
                    "shorts": {"layout": "frame", "sound": {"music": "own.mp3", "music_off": False,
                                                            "audio": {"narration_vol": 1.0, "music_vol": 0.1}}}}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            (work / "shorts").mkdir(parents=True)
            (work / "shorts" / "plan.json").write_text(json.dumps({"shorts": [
                {"id": 1, "first_line": 1, "last_line": 1, "title": "t", "description": "d", "reason": "r"}]}),
                encoding="utf-8")
            seen = []
            with config.chapter_scope(work, Path(tmp) / "output"), \
                    mock.patch.object(s7_assemble, "prepare_render", lambda *args: seen.append(args[2]) or SimpleNamespace(total=12.0)), \
                    mock.patch.object(s7_assemble, "execute_render", lambda render, out: out.write_bytes(b"mp4")), \
                    mock.patch.object(s7_assemble, "load_mapping", return_value={}), \
                    mock.patch.object(s7_assemble, "load_media_settings", return_value=settings):
                s8_shorts.run(Manifest(chapter_id="c", source_pdf="", work_dir=str(work), beats=beats(12)))
        self.assertEqual((seen[0]["music"], seen[0]["audio"]["music_vol"]), ("own.mp3", 0.1))


class AudioSampleTest(ApiProjects):
    def test_the_mix_is_the_one_a_render_uses(self):
        filters, label = s7_assemble._audio_filters([(0, 0), (1, 2500)], 2, 2.0, 0.05)
        joined = ";".join(filters)
        self.assertEqual(label, "[aout]")
        self.assertIn("volume=2.0", joined)
        self.assertIn("adelay=2500:all=1", joined)
        self.assertIn("volume=0.05[music]", joined)
        self.assertIn("sidechaincompress", joined)
        self.assertEqual(s7_assemble._audio_filters([], None, 2.0, 0.05), ([], None))

    def test_music_follows_the_mode_and_whose_file_it_is(self):
        projects.assets_dir(self.slug).joinpath("shorts_music.mp3").write_bytes(b"x")
        with mock.patch.object(s7_assemble, "_music_file", return_value=Path("default.mp3")):
            self.assertEqual(audio_sample.music_track(self.slug, "file", True).name, "shorts_music.mp3")
            self.assertEqual(audio_sample.music_track(self.slug, "file", False), Path("default.mp3"))
            self.assertEqual(audio_sample.music_track(self.slug, "default", True), Path("default.mp3"))
            self.assertIsNone(audio_sample.music_track(self.slug, "none", True))

    def test_without_rendered_narration_the_voice_sample_is_played(self):
        voice = Path(tempfile.mkdtemp()) / "voice_ref.wav"
        voice.write_bytes(b"x")
        self.addCleanup(shutil.rmtree, voice.parent, True)
        with mock.patch.object(audio_sample, "DEFAULT_VOICE", voice):
            self.assertEqual(audio_sample.narration_lines(self.slug), [(voice, audio_sample.SAMPLE_S)])

    def test_there_is_nothing_to_play_without_narration_or_a_voice_sample(self):
        with mock.patch.object(audio_sample, "DEFAULT_VOICE", Path("missing.wav")):
            response = self.client.get(f"/api/projects/{self.slug}/preview/audio.wav?own=1&mode=none")
        self.assertEqual(response.status_code, 404)
        self.assertIn("narration", response.get_json()["error"])


class RangeBatchTest(unittest.TestCase):
    def env(self, steps: list):
        return batch.BatchEnv(
            runners={"shorts_find": steps[0], "shorts_render": steps[1]}, stages=batch.SHORTS_STAGES,
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {})

    def test_chapters_picked_by_the_range_join_the_batch_and_are_rendered(self):
        rendered = []

        def find(b, n):
            b.set_meta(found={"picked": {"3": 1, "6": 2}})
            b.join({3: ["shorts_render"], 6: ["shorts_render"]})

        with tempfile.TemporaryDirectory() as tmp:
            run = batch.Batch.create(Path(tmp) / "b.json", self.env([find, lambda b, n: rendered.append(n)]),
                                     {1: list(batch.SHORTS_STAGES)}, meta={"find": {"chapters": [1, 3, 6]}})
            for _ in range(200):
                if run.status == "finished":
                    break
                threading.Event().wait(0.05)
            run.close()
            saved = json.loads((Path(tmp) / "b.json").read_text(encoding="utf-8"))
        self.assertEqual(run.status, "finished")
        self.assertEqual(sorted(rendered), [1, 3, 6])
        self.assertEqual(saved["meta"]["find"], {"chapters": [1, 3, 6]})
        self.assertEqual([c["n"] for c in saved["chapters"]], [1, 3, 6])


class RangeApiTest(ApiProjects):
    def start(self, body: dict):
        with mock.patch("services.batching.shorts_blocker", lambda slug, n: "it has no finished video yet" if n == 2 else None), \
                mock.patch.object(batch.Batch, "create") as create:
            response = self.client.post(f"/api/projects/{self.slug}/shorts/batch", json=body)
        batching.SHORTS_BATCHES.pop(self.slug, None)   # the mock stands in for a batch that never ran
        return response, create

    def test_a_range_starts_one_batch_at_its_first_chapter_that_can_have_shorts(self):
        response, create = self.start({"first": 1, "last": 3, "max_shorts": 4})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["started"], [1, 3])
        self.assertEqual(response.get_json()["skipped"][0]["n"], 2)
        _, env, plans = create.call_args.args
        self.assertEqual(plans, {1: ["shorts_find", "shorts_render"]})
        self.assertEqual(create.call_args.kwargs["meta"], {"find": {"chapters": [1, 3], "max_shorts": 4,
                                                                "skipped": response.get_json()["skipped"]}})

    def test_a_limit_below_one_is_refused(self):
        self.assertEqual(self.start({"first": 1, "last": 3, "max_shorts": 0})[0].status_code, 400)


class MusicLibraryTest(ApiProjects):
    def track(self, name, theme, seconds):
        return {"file": f"/m/{name}.mp3", "theme": theme, "duration_s": seconds, "name": name}

    def test_a_track_of_the_shorts_theme_that_fits_the_narration_is_picked(self):
        lib = [self.track("a", "comedic", 20), self.track("b", "dramatic", 40), self.track("c", "dramatic", 55),
               self.track("d", "dramatic", 30)]
        self.assertEqual(s8_shorts.pick_track(lib, "dramatic", 35, set())["name"], "b")
        self.assertEqual(s8_shorts.pick_track(lib, "dramatic", 25, set())["name"], "d")

    def test_without_a_track_of_the_theme_any_track_is_used_and_tracks_are_not_repeated(self):
        lib = [self.track("a", "comedic", 40), self.track("b", "comedic", 45)]
        self.assertEqual(s8_shorts.pick_track(lib, "op", 38, set())["name"], "a")
        self.assertEqual(s8_shorts.pick_track(lib, "op", 38, {"/m/a.mp3"})["name"], "b")
        self.assertEqual(s8_shorts.pick_track(lib, "op", 38, {"/m/a.mp3", "/m/b.mp3"})["name"], "a")

    def test_when_the_narration_outlasts_every_track_the_longest_one_is_used(self):
        lib = [self.track("a", "aura", 20), self.track("b", "aura", 30)]
        self.assertEqual(s8_shorts.pick_track(lib, "aura", 50, set())["name"], "b")

    def test_the_timeline_is_stretched_to_the_music_length(self):
        items = [{"dur": 3.0, "start": 0.0}, {"dur": 3.0, "start": 0.0}]
        total = s7_assemble._pad_to(items, 5.5, 20.0, 0.5)
        self.assertAlmostEqual(total, 20.0, places=2)
        self.assertEqual(s7_assemble._pad_to(items, 20.0, 10.0, 0.5), 20.0)

    def test_the_stills_before_the_peak_are_held_until_it_lands_on_the_drop(self):
        items = [{"order": n, "dur": 4.0, "start": 3.5 * (n - 1)} for n in (1, 2, 3, 4)]
        self.assertTrue(s7_assemble._hold_before(items, 3, 27.0, 0.5))
        self.assertAlmostEqual(items[2]["start"], 27.0, places=2)
        self.assertFalse(s7_assemble._hold_before(items, 3, 10.0, 0.5))    # already later: left alone
        self.assertFalse(s7_assemble._hold_before(items, 1, 5.0, 0.5))     # nothing before the first line

    def test_the_track_claude_chose_is_the_one_played(self):
        lib = [{**self.track("a", "op", 34), "id": "a1"}, {**self.track("b", "op", 36), "id": "b1"}]
        self.assertEqual(s8_shorts.pick_track(lib, "op", 30, set(), "b1")["name"], "b")

    def test_a_drop_point_has_to_be_inside_the_track(self):
        self.assertEqual(short_music.clean_drop("27", 34.0), 27.0)
        self.assertIsNone(short_music.clean_drop("", 34.0))
        for bad in ("0", "34", "x"):
            with self.assertRaises(short_music.LibraryError):
                short_music.clean_drop(bad, 34.0)

    def test_the_balanced_mix_levels_both_and_does_not_duck(self):
        filters, label = s7_assemble._audio_filters([(1, 0)], 2, 0.6, 0.4, balanced=True)
        graph = ";".join(filters)
        self.assertEqual(label, "[aout]")
        self.assertIn("volume=0.6", graph)
        self.assertIn("volume=0.4", graph)
        self.assertNotIn("sidechaincompress", graph)

    def test_tracks_are_listed_for_the_stage_and_deleted_with_their_file(self):
        folder = projects.assets_dir(self.slug) / "shorts_music"
        folder.mkdir()
        (folder / "t1.mp3").write_bytes(b"x")
        projects.update_settings(self.slug, {"shorts": {"music_share": 5}})
        project = projects.load(self.slug)
        project["settings"]["shorts"]["library"] = [{"id": "t1", "name": "T", "theme": "aura", "duration_s": 30.0,
                                                     "file": "assets/shorts_music/t1.mp3"}]
        projects.save(project)
        snap = projects.media_settings_snapshot(self.slug, 1)["shorts"]
        self.assertEqual([(t["name"], t["theme"]) for t in snap["library"]], [("T", "aura")])
        self.assertEqual(snap["music_share"], 0.7)
        self.assertEqual(short_music.themes_of(projects.load(self.slug)), ["aura"])
        self.assertTrue(self.client.delete(f"/api/projects/{self.slug}/shorts_music/t1").get_json()["ok"])
        self.assertFalse((folder / "t1.mp3").exists())
        self.assertEqual(projects.media_settings_snapshot(self.slug, 1)["shorts"]["library"], [])

if __name__ == "__main__":
    unittest.main()


class ScriptedShortTest(unittest.TestCase):
    def test_a_script_line_shows_the_panels_of_its_source_lines(self):
        beat = {n: Panel(id=f"p{n}", bbox=[0, 0, 0, 0], order=n, crop=f"crop{n}.png") for n in (1, 2, 3, 4)}
        mapping = {2: {"frames": [{"image": "a.png"}, {"image": "b.png"}]}}
        short = {"id": 3, "script": [{"text": "Hook.", "pace": "action", "source_lines": [2, 3]},
                                     {"text": "Watch on.", "pace": "tense", "source_lines": [4, 4, 1, 2], "closing": True}]}
        panels, new_mapping = s8_shorts.script_beats(short, beat, mapping)
        self.assertEqual([(p.id, p.order, p.script_line, p.emotion) for p in panels],
                         [("short03_01", 1, "Hook.", "action"), ("short03_02", 2, "Watch on.", "tense")])
        self.assertEqual([f["image"] for f in new_mapping[1]["frames"]], ["a.png", "b.png", "crop3.png"])
        self.assertEqual([f["image"] for f in new_mapping[2]["frames"]], ["crop4.png", "crop1.png", "a.png", "b.png"])
        # voiced: ~1.6s per frame, so a 3.2s line keeps two of its frames and a 1s line one
        panels[0].duration_s, panels[1].duration_s = 3.2, 1.0
        s8_shorts.fit_frames(short, panels, new_mapping, beat, mapping)
        self.assertEqual(len(new_mapping[1]["frames"]), 2)
        self.assertEqual(len(new_mapping[2]["frames"]), 1)

    def test_the_logo_has_curved_corners_and_a_border(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = s8_shorts.rounded_logo(200, Path(tmp) / "logo.png")
            logo = Image.open(out)
            self.assertEqual(logo.width, 200)
            self.assertEqual(logo.getpixel((0, 0))[3], 0)               # the corner is cut away
            self.assertEqual(logo.getpixel((100, 3))[:3], (255, 255, 255))   # the border runs along the edge


class WriteScriptTest(unittest.TestCase):
    def write(self, answer: dict):
        scene = beats(10, 10, 10)
        with mock.patch.object(shorts.claude_cli, "call", return_value=answer):
            return shorts.write_script({"title": "T", "description": "D"}, scene, [], brief="", model="m",
                                       work=Path(tempfile.gettempdir()), usage={})

    def test_the_closing_comes_last_and_unknown_source_lines_are_replaced(self):
        script = self.write({"lines": [{"text": "One.", "pace": "action", "source_lines": [2, 99]},
                                       {"text": "Two.", "pace": "tense", "source_lines": [99]}],
                             "closing": {"text": "Watch on.", "pace": "reveal", "source_lines": [3]}})
        self.assertEqual([(e["text"], e["source_lines"], e.get("closing")) for e in script],
                         [("One.", [2], None), ("Two.", [3], None), ("Watch on.", [3], True)])

    def test_a_script_too_long_for_a_minute_loses_its_last_lines_but_keeps_the_closing(self):
        long = " ".join(["word"] * 60)
        script = self.write({"lines": [{"text": long, "pace": "calm", "source_lines": [1]} for _ in range(3)],
                             "closing": {"text": "Watch on.", "pace": "reveal", "source_lines": [3]}})
        self.assertEqual(len(script), 3)
        self.assertTrue(script[-1]["closing"])

    def test_claude_picks_a_track_and_the_line_that_lands_on_its_drop(self):
        short = {"title": "T", "description": "D"}
        tracks = [{"id": "aa", "theme": "op", "duration_s": 34.0, "drop_s": 27.0},
                  {"id": "bb", "theme": "sad", "duration_s": 40.0, "drop_s": None}]
        answer = {"lines": [{"text": "One.", "pace": "tense", "source_lines": [1]},
                            {"text": "Two.", "pace": "action", "source_lines": [2]}],
                  "closing": {"text": "Watch on.", "pace": "reveal", "source_lines": [3]}, "track": "aa", "peak_line": 1}
        with mock.patch.object(shorts.claude_cli, "call", return_value=answer) as call:
            shorts.write_script(short, beats(10, 10, 10), [], brief="", model="m", work=Path(tempfile.gettempdir()),
                                usage={}, tracks=tracks)
        self.assertEqual((short["track"], short["theme"], short["peak"]), ("aa", "op", 1))
        self.assertIn("aa | op | 34s | drop at 27s", call.call_args.args[0][0]["text"])
        short = {"title": "T", "description": "D"}      # a track without a drop, or an unknown one: no peak
        with mock.patch.object(shorts.claude_cli, "call", return_value={**answer, "track": "bb"}):
            shorts.write_script(short, beats(10, 10, 10), [], brief="", model="m", work=Path(tempfile.gettempdir()),
                                usage={}, tracks=tracks)
        self.assertEqual((short["track"], "peak" in short), ("bb", False))

    def test_an_answer_without_a_closing_is_refused(self):
        with self.assertRaises(ValueError):
            self.write({"lines": [{"text": "One.", "pace": "calm", "source_lines": [1]}],
                        "closing": {"text": "", "pace": "calm", "source_lines": []}})


class SpreadTest(unittest.TestCase):
    def entry(self, number: int, score: int) -> dict:
        return {"id": number, "first_line": number, "last_line": number, "score": score}

    def test_a_limit_goes_by_score_so_a_strong_chapter_can_take_several_and_weak_ones_none(self):
        chosen = shorts.select({1: [self.entry(1, 9), self.entry(2, 9)], 2: [self.entry(1, 6)], 3: [self.entry(1, 7)]}, 3)
        self.assertEqual({n: len(plan) for n, plan in chosen.items()}, {1: 2, 3: 1})

    def test_equal_scores_prefer_a_chapter_best_over_its_second(self):
        chosen = shorts.select({1: [self.entry(1, 8), self.entry(2, 8)], 2: [self.entry(1, 8)]}, 2)
        self.assertEqual({n: len(plan) for n, plan in chosen.items()}, {1: 1, 2: 1})


class ShortsDeleteTest(ApiProjects):
    def make(self, n: int, ids=(1, 2), rendered=(1,)):
        work, output = projects.chapter_work_path(self.slug, n), projects.chapter_output_path(self.slug, n)
        plan = [{"id": i, "first_line": i, "last_line": i, "title": f"T{i}", "description": "D", "reason": "R",
                 "duration_s": 20} for i in ids]
        shorts.save_plan(work, output, f"c{n}", plan)
        made = []
        for i in rendered:
            touch(output / "shorts" / f"short_{i:02d}.mp4", b"video")
            made.append({"id": i, "file": f"short_{i:02d}.mp4", "title": f"T{i}", "description": "D", "duration_s": 20})
        if made:
            (output / "shorts" / "shorts.json").write_text(json.dumps({"shorts": made}), encoding="utf-8")
        return work, output

    def test_a_planned_short_without_a_video_is_listed_with_the_plan(self):
        self.make(3)
        chapter = self.client.get(f"/api/projects/{self.slug}/shorts").get_json()["chapters"][0]
        self.assertEqual([s["id"] for s in chapter["shorts"]], [1])
        self.assertEqual([s["id"] for s in chapter["plan"]], [1, 2])

    def test_one_short_goes_with_its_video_and_its_place_in_the_plan(self):
        work, output = self.make(3)
        self.assertEqual(self.client.delete(f"/api/projects/{self.slug}/shorts/3/1").status_code, 200)
        self.assertEqual([s["id"] for s in shorts.read_plan(work)], [2])
        self.assertEqual(shorts.rendered(output), [])
        self.assertEqual(self.client.delete(f"/api/projects/{self.slug}/shorts/3/9").status_code, 404)

    def test_the_last_short_takes_the_folders_with_it(self):
        work, output = self.make(3, ids=(1,))
        self.client.delete(f"/api/projects/{self.slug}/shorts/3/1")
        self.assertFalse((work / "shorts").exists() or (output / "shorts").exists())

    def test_all_of_a_chapters_shorts_are_deleted_at_once(self):
        work, output = self.make(3)
        self.assertEqual(self.client.delete(f"/api/projects/{self.slug}/shorts/3").status_code, 200)
        self.assertEqual((shorts.read_plan(work), shorts.rendered(output)), ([], []))

    def test_nothing_is_deleted_while_shorts_are_being_made(self):
        self.make(3)
        with mock.patch.dict(batching.SHORTS_BATCHES, {self.slug: SimpleNamespace(active=True)}):
            self.assertEqual(self.client.delete(f"/api/projects/{self.slug}/shorts/3").status_code, 409)
        self.assertEqual(len(shorts.read_plan(projects.chapter_work_path(self.slug, 3))), 2)
