"""A saved batch must still load after a pipeline step was removed."""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
