"""A saved batch must still load after a pipeline step was removed."""
from __future__ import annotations

import contextlib
import tempfile
import threading
import unittest
from pathlib import Path

import batch


def saved_chapter(**overrides) -> dict:
    chapter = {"n": 1, "plan": ["download", "split", "stitch", "script"], "completed": ["download"], "stage": "split",
               "state": "queued", "error": None, "started_at": None, "finished_at": None,
               "stage_started_at": None, "stage_seconds": {"download": 3.0, "split": 4.0}}
    return {**chapter, **overrides}


class RetiredStageTest(unittest.TestCase):
    def test_the_retired_step_leaves_the_plan_and_the_completed_list(self):
        chapter = saved_chapter(completed=["download", "split"])
        batch._drop_retired_stages(chapter)
        self.assertEqual(chapter["plan"], ["download", "stitch", "script"])
        self.assertEqual(chapter["completed"], ["download"])

    def test_a_chapter_queued_for_the_retired_step_moves_to_the_next_one(self):
        chapter = saved_chapter()
        batch._drop_retired_stages(chapter)
        self.assertEqual((chapter["state"], chapter["stage"]), ("queued", "stitch"))

    def test_a_chapter_on_a_current_step_is_left_alone(self):
        chapter = saved_chapter(stage="stitch", plan=["download", "stitch"])
        before = dict(chapter)
        batch._drop_retired_stages(chapter)
        self.assertEqual(chapter, before)

    def test_the_review_pause_survives(self):
        chapter = saved_chapter(plan=["export", "audio"], completed=["export"], stage="review", state="waiting")
        batch._drop_retired_stages(chapter)
        self.assertEqual((chapter["state"], chapter["stage"], chapter["completed"]), ("waiting", "review", ["export"]))

    def test_a_chapter_whose_only_remaining_step_was_retired_is_done(self):
        chapter = saved_chapter(plan=["download", "split"], completed=["download"])
        batch._drop_retired_stages(chapter)
        self.assertEqual((chapter["state"], chapter["stage"]), ("done", None))


class StageSpanTest(unittest.TestCase):
    def run_one_chapter(self, plan: list[str]) -> dict:
        env = batch.BatchEnv(
            runners={stage: (lambda b, n: None) for stage in batch.LANE_STAGES},
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {})
        with tempfile.TemporaryDirectory() as tmp:
            run = batch.Batch.create(Path(tmp) / "batch.json", env, {1: plan})
            for _ in range(200):
                if run.status == "finished":
                    break
                threading.Event().wait(0.05)
            run.close()
            return run.chapters[0]

    def test_every_run_stage_records_when_it_started_and_ended(self):
        chapter = self.run_one_chapter(["download", "stitch"])
        self.assertEqual(sorted(chapter["stage_spans"]), ["download", "stitch"])
        for start, end in chapter["stage_spans"].values():
            self.assertLessEqual(start, end)


class LowDiskSpaceTest(unittest.TestCase):
    def test_the_started_chapter_finishes_then_space_is_freed_before_the_next_one_starts(self):
        events, low = [], {"on": False}

        def runner(stage):
            def run(b, n):
                events.append(f"{stage}{n}")
                if stage == "download" and n == 1:
                    low["on"] = True          # the disk fills up while chapter 1 is under way
            return run

        def offload(active):
            events.append("offload")
            low["on"] = False
            return 5

        env = batch.BatchEnv(
            runners={stage: runner(stage) for stage in batch.LANE_STAGES},
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {}, space_low=lambda: low["on"], offload=offload)
        with tempfile.TemporaryDirectory() as tmp:
            run = batch.Batch.create(Path(tmp) / "batch.json", env, {1: ["download", "video"], 2: ["download", "video"]})
            for _ in range(200):
                if run.status == "finished":
                    break
                threading.Event().wait(0.05)
            run.close()
        self.assertEqual(run.status, "finished")
        self.assertLess(events.index("video1"), events.index("offload"))
        self.assertLess(events.index("offload"), events.index("download2"))


if __name__ == "__main__":
    unittest.main()
