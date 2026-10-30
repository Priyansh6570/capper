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



class FinishedHookTest(unittest.TestCase):
    def test_the_environment_hears_once_that_the_batch_ran_out_of_work(self):
        heard = []
        env = batch.BatchEnv(
            runners={stage: (lambda b, n: None) for stage in batch.LANE_STAGES},
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {}, finished=lambda run: heard.append(dict(run.meta)))
        with tempfile.TemporaryDirectory() as tmp:
            run = batch.Batch.create(Path(tmp) / "batch.json", env, {1: ["download"], 2: ["download"]}, meta={"find": {"chapters": [1, 2]}})
            for _ in range(200):
                if run.status == "finished":
                    break
                threading.Event().wait(0.05)
            run.close()
        self.assertEqual(heard, [{"find": {"chapters": [1, 2]}}])


class RetryTest(unittest.TestCase):
    """A chapter that failed at a step can be run again from that step or from the start, or be given up on."""

    def setUp(self):
        self.ran: list[str] = []
        self.failures: list[tuple[int, str, str]] = []
        self.finished = 0
        self.fail_next = {"stitch"}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def runner(self, stage):
        def run(b, n):
            self.ran.append(stage)
            if stage in self.fail_next:
                self.fail_next.discard(stage)
                raise RuntimeError("boom")
        return run

    def start(self) -> batch.Batch:
        env = batch.BatchEnv(
            runners={stage: self.runner(stage) for stage in batch.LANE_STAGES},
            scope=lambda n: contextlib.nullcontext(), done_stages=lambda n: [], review_enabled=lambda: False,
            thresholds=lambda: {"five_hour": 100, "seven_day": 100}, auto_wait=lambda: False,
            cancel_job=lambda job: None, notify=lambda title, message: None, claude_total=lambda n: None,
            usage=lambda: {}, finished=lambda run: setattr(self, "finished", self.finished + 1),
            failed=lambda run, n, stage, error: self.failures.append((n, stage, error)))
        run = batch.Batch.create(Path(self.tmp.name) / "batch.json", env, {1: ["download", "stitch", "script"]})
        self.addCleanup(run.close)
        self.settle(run)
        return run

    def settle(self, run: batch.Batch) -> None:
        for _ in range(200):
            if run.status == "finished":
                return
            threading.Event().wait(0.05)
        raise AssertionError(f"the batch did not finish: {run.status}")

    def test_a_failure_is_reported_with_its_chapter_stage_and_error(self):
        run = self.start()
        self.assertEqual(self.failures, [(1, "stitch", "RuntimeError: boom")])
        self.assertEqual((run.chapters[0]["state"], run.chapters[0]["stage"]), ("failed", "stitch"))

    def test_retrying_goes_on_from_the_step_that_failed(self):
        run = self.start()
        run.retry(1)
        self.settle(run)
        self.assertEqual(self.ran, ["download", "stitch", "stitch", "script"])
        self.assertEqual(run.chapters[0]["state"], "done")
        self.assertEqual(self.finished, 2)

    def test_redoing_starts_the_chapter_over_from_its_first_step(self):
        run = self.start()
        run.retry(1, from_start=True)
        self.settle(run)
        self.assertEqual(self.ran, ["download", "stitch", "download", "stitch", "script"])
        self.assertEqual(run.chapters[0]["state"], "done")

    def test_a_skipped_chapter_is_cancelled_and_stays_out_of_the_batch(self):
        run = self.start()
        run.skip(1)
        self.assertEqual(run.chapters[0]["state"], "cancelled")
        self.assertEqual(run.status, "finished")

    def test_only_a_failed_chapter_of_a_running_batch_can_be_retried_or_skipped(self):
        run = self.start()
        run.skip(1)
        with self.assertRaisesRegex(ValueError, "has not failed"):
            run.retry(1)
        with self.assertRaisesRegex(ValueError, "has not failed"):
            run.skip(1)

    def test_a_cancelled_batch_cannot_be_retried(self):
        run = self.start()
        run.status = "cancelled"
        with self.assertRaisesRegex(ValueError, "cancelled or replaced"):
            run.retry(1)


if __name__ == "__main__":
    unittest.main()
