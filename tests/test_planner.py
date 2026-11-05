"""The Telegram planner, driven through the real bot handler against a fake Telegram: failure recovery, shorts,
the upload plan, proactive suggestions and merge approval."""
from __future__ import annotations

import dataclasses
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import batch
import branding
import projects
from publishing import telegram, uploads
from services import advisor, autopublish, batching, cards, planning, remote
from tests.support import touch
from tests.test_publishing import Publishing

REAL_NOTIFY = telegram.notify
CHAPTER_MINUTES = 10


class ImmediateThread:
    """A thread that runs its work when started, on the caller's thread."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class Saturday(date):
    @classmethod
    def today(cls):
        return date(2026, 10, 24)


def wait_for(condition, seconds: float = 5) -> None:
    deadline = time.time() + seconds
    while not condition():
        if time.time() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)


class PlannerCase(Publishing):
    def setUp(self):
        super().setUp()
        self.api_calls: list[tuple] = []
        for target, name, value in (
            (telegram, "notify", REAL_NOTIFY), (telegram, "_api", lambda method, **kw: self.api_calls.append((method, kw)) or {}),
            (branding, "duration_of", lambda path: CHAPTER_MINUTES * 60.0), (batching.notify, "toast", mock.Mock()),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        telegram._controls = remote.CONTROLS
        self.addCleanup(setattr, telegram, "_controls", None)
        self.addCleanup(setattr, telegram, "_reply_handler", None)
        telegram._save(token="tok", chat_id=42)
        self.connect()

    def project(self, name: str = "Plan Series", chapters: int = 8, finished=(), channel: bool = True, **plan) -> str:
        slug = self.new_project(name, chapters=0)["slug"]
        project = projects.load(slug)
        projects.fill_chapters(project, [{"n": n} for n in range(1, chapters + 1)])
        projects.save(project)
        for n in finished:
            touch(projects.chapter_video_path(slug, n))
            projects.set_status(slug, n, "rendered")
        projects.update_settings(slug, {"youtube": {"channel": "UC1" if channel else None}, "plan": plan,
                                        "branding": {"intro": {"enabled": False}, "outro": {"enabled": False}}})
        return slug

    def say(self, text: str) -> None:
        telegram._handle({"update_id": 1, "message": {"chat": {"id": 42}, "text": text}})

    def sent(self) -> list[dict]:
        return [kw for method, kw in self.api_calls if method == "sendMessage"]

    def texts(self) -> list[str]:
        return [kw["text"] for kw in self.sent()]

    def buttons(self) -> dict[str, str]:
        """The buttons of the newest message that has any, label -> callback data."""
        for kw in reversed(self.sent()):
            if "reply_markup" in kw:
                return {b["text"]: b["callback_data"] for row in kw["reply_markup"]["inline_keyboard"] for b in row}
        return {}

    def tap(self, label: str) -> str:
        telegram._handle({"update_id": 2, "callback_query": {"id": "q", "data": self.buttons()[label], "message": {
            "chat": {"id": 42}, "message_id": 7}}})
        return [kw["text"] for method, kw in self.api_calls if method == "answerCallbackQuery"][-1]

    def cleared_keyboards(self) -> int:
        return sum(method == "editMessageReplyMarkup" for method, _ in self.api_calls)


class FailureRecovery(PlannerCase):
    def start_batch(self, shorts: bool = False):
        """A batch of chapter 1 whose second step fails the first time it runs."""
        slug = self.project(chapters=2)
        self.ran, failing = [], {"stitch", "shorts_render"}

        def runner(stage):
            def run(b, n):
                self.ran.append(stage)
                if stage in failing:
                    failing.discard(stage)
                    raise RuntimeError("the page could not be read\nsecond line of the traceback")
            return run

        stages = batch.SHORTS_STAGES if shorts else ("download", "stitch", "script")
        env = batching.shorts_env(slug) if shorts else batching.batch_env(slug)
        env = dataclasses.replace(env, runners={stage: runner(stage) for stage in (*batch.LANE_STAGES, *batch.SHORTS_STAGES)},
                                  finished=lambda run: None)
        run = batch.Batch.create(Path(self.tmp) / "batch.json", env, {1: list(stages)})
        self.addCleanup(run.close)
        patcher = mock.patch.dict(batching.SHORTS_BATCHES if shorts else batching.BATCHES, {slug: run})
        patcher.start()
        self.addCleanup(patcher.stop)
        wait_for(self.buttons)
        return slug, run

    def test_a_failed_chapter_is_announced_with_its_stage_error_and_four_ways_to_go_on(self):
        slug, run = self.start_batch()
        self.assertIn("Plan Series: chapter 1 failed at stitch.\nRuntimeError: the page could not be read", self.texts())
        self.assertEqual(list(self.buttons()), ["Retry from failed stage", "Redo all stages", "Skip chapter", "Show full error"])
        self.assertEqual(run.chapters[0]["state"], "failed")

    def test_retry_from_the_failed_stage_does_not_repeat_the_stages_before_it(self):
        _, run = self.start_batch()
        self.assertEqual(self.tap("Retry from failed stage"), "Retrying the stage that failed.")
        wait_for(lambda: run.chapters[0]["state"] == "done")
        self.assertEqual(self.ran, ["download", "stitch", "stitch", "script"])
        self.assertEqual(self.cleared_keyboards(), 1)

    def test_redo_all_stages_starts_the_chapter_over(self):
        _, run = self.start_batch()
        self.assertEqual(self.tap("Redo all stages"), "Starting over.")
        wait_for(lambda: run.chapters[0]["state"] == "done")
        self.assertEqual(self.ran, ["download", "stitch", "download", "stitch", "script"])

    def test_skip_gives_the_chapter_up(self):
        _, run = self.start_batch()
        self.assertEqual(self.tap("Skip chapter"), "Chapter skipped.")
        self.assertEqual(run.chapters[0]["state"], "cancelled")

    def test_show_full_error_sends_the_log_and_keeps_the_buttons(self):
        self.start_batch()
        self.assertEqual(self.tap("Show full error"), "The full error is below.")
        self.assertIn("== stitch failed: RuntimeError: the page could not be read", self.texts()[-1])
        self.assertIn("second line of the traceback", self.texts()[-1])
        self.assertEqual(self.cleared_keyboards(), 0)

    def test_a_card_works_once(self):
        self.start_batch()
        buttons = self.buttons()
        self.tap("Skip chapter")
        telegram._handle({"update_id": 3, "callback_query": {"id": "q2", "data": buttons["Skip chapter"], "message": {
            "chat": {"id": 42}, "message_id": 7}}})
        self.assertEqual([kw["text"] for m, kw in self.api_calls if m == "answerCallbackQuery"][-1], "That card has expired.")

    def test_a_failed_shorts_step_is_recovered_the_same_way(self):
        _, run = self.start_batch(shorts=True)
        self.assertIn("Plan Series: chapter 1 failed at shorts_render.", self.texts()[0])
        self.tap("Retry from failed stage")
        wait_for(lambda: run.chapters[0]["state"] == "done")
        self.assertEqual(self.ran, ["shorts_find", "shorts_render", "shorts_render"])

    def test_the_website_offers_the_same_actions(self):
        slug, run = self.start_batch()
        self.assertEqual(self.client.post(f"/api/projects/{slug}/batch/chapters/1/retry", json={"from_start": True}).status_code, 200)
        wait_for(lambda: run.chapters[0]["state"] == "done")
        self.assertEqual(self.ran, ["download", "stitch", "download", "stitch", "script"])
        self.assertEqual(self.client.post(f"/api/projects/{slug}/batch/chapters/1/retry").status_code, 409)
        self.assertEqual(self.client.post(f"/api/projects/{slug}/batch/chapters/1/skip").status_code, 409)

    def test_the_website_can_skip_a_failed_chapter(self):
        slug, run = self.start_batch()
        self.assertEqual(self.client.post(f"/api/projects/{slug}/batch/chapters/1/skip").status_code, 200)
        self.assertEqual(run.chapters[0]["state"], "cancelled")


class Shorts(PlannerCase):
    def test_shorts_command_starts_the_pipeline_and_names_what_was_skipped(self):
        slug = self.project(finished=(1, 2))
        with mock.patch.object(remote, "start_shorts", return_value=([1, 2], [{"n": 3, "reason": "it has no finished video yet"}])) as start:
            self.say("/shorts Plan Series 1-3")
        start.assert_called_once_with(slug, [1, 2, 3])
        self.assertEqual(self.texts(), ["Making shorts for Plan Series, chapters 1-2.\nSkipped chapter 3: it has no finished video yet."])

    def test_shorts_command_reports_why_nothing_could_start(self):
        self.project()
        self.say("/shorts Plan Series 1-2")
        self.say("/shorts Plan Series 99")
        self.assertIn("None of these chapters can have shorts yet", self.texts()[0])
        self.assertIn("has no chapter 99", self.texts()[1])

    def test_a_rendered_chapter_starts_a_real_shorts_batch(self):
        slug = self.project(finished=(1,))
        with mock.patch.dict(batching.SHORTS_BATCHES, clear=False), \
                mock.patch.object(batching.batch.Batch, "create") as create:
            self.say("/shorts Plan Series 1")
        self.assertEqual(create.call_args.args[2], {1: list(batch.SHORTS_STAGES)})
        self.assertEqual(create.call_args.kwargs["meta"]["find"]["chapters"], [1])
        self.assertIn("Making shorts for Plan Series, chapters 1.", self.texts()[0])
        self.assertTrue(slug)

    def test_shorts_are_made_by_themselves_for_the_chapters_a_pipeline_rendered(self):
        slug = self.project()
        projects.update_settings(slug, {"shorts": {"auto_make": True}})
        finished = mock.Mock(chapters=[{"n": 1, "state": "done", "plan": ["video"]}, {"n": 2, "state": "failed", "plan": ["video"]},
                                       {"n": 3, "state": "done", "plan": ["download"]}])
        with mock.patch.object(batching.threading, "Thread", ImmediateThread), \
                mock.patch.object(batching, "start_shorts") as start, mock.patch.object(batching.cards, "propose_merge"):
            batching._pipeline_finished(slug, finished)
            projects.update_settings(slug, {"shorts": {"auto_make": False}})
            batching._pipeline_finished(slug, finished)
        start.assert_called_once_with(slug, [1])

    def test_a_refused_automatic_start_is_reported(self):
        slug = self.project()
        projects.update_settings(slug, {"shorts": {"auto_make": True}})
        finished = mock.Mock(chapters=[{"n": 1, "state": "done", "plan": ["video"]}])
        with mock.patch.object(batching.threading, "Thread", ImmediateThread), mock.patch.object(batching.cards, "propose_merge"), \
                mock.patch.object(batching, "start_shorts", side_effect=batching.Refused("Shorts are already being made")):
            batching._pipeline_finished(slug, finished)
        self.assertEqual(self.texts(), ["Plan Series: shorts were not started - Shorts are already being made."])

    def test_nothing_is_made_by_itself_unless_the_setting_is_turned_on(self):
        slug = self.project()
        finished = mock.Mock(chapters=[{"n": 1, "state": "done", "plan": ["video"]}])
        with mock.patch.object(batching.threading, "Thread", ImmediateThread),                 mock.patch.object(batching, "start_shorts") as start, mock.patch.object(batching.cards, "propose_merge") as merge:
            batching._pipeline_finished(slug, finished)
        start.assert_not_called()
        merge.assert_called_once_with(slug)

    def test_the_setting_is_off_by_default_and_saved(self):
        slug = self.project()
        self.assertFalse(projects.load(slug)["settings"]["shorts"]["auto_make"])
        saved = self.client.post(f"/api/projects/{slug}/settings", json={"shorts": {"auto_make": True}}).get_json()
        self.assertTrue(saved["settings"]["shorts"]["auto_make"])

    def test_schedule_lists_shorts_with_their_upload_times(self):
        self.short_item(status="scheduled", title="Moment", series="Plan Series", publish_at="2030-01-05T09:30:00+00:00")
        self.say("/schedule")
        text = self.texts()[-1]
        self.assertIn("Shorts (1)", text)
        self.assertIn("Plan Series: Moment", text)
        self.assertIn(f"{telegram._local('2030-01-05T09:30:00+00:00')} - Plan Series: Moment", text)


class UploadPlan(PlannerCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(planning, "date", Saturday)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.slug = self.project(chapters=8, finished=(1, 2, 3, 4), days=[0, 1, 2, 3, 4], min_minutes=30)
        touch(projects.merged_dir(self.slug) / "merged_20260101-000000_ch1-2.mp4", b"video")
        self.waiting_item(project=self.slug, chapters=[3])

    def test_the_plan_counts_queued_merged_and_finished_chapters_as_buffer(self):
        plan = planning.build(self.slug, set())
        self.assertEqual((plan.queued, plan.merged, plan.unmerged), ({3}, {1, 2}, {4}))
        self.assertEqual((plan.avg_minutes, plan.per_upload, plan.buffer_minutes), (10, 3, 40))
        self.assertEqual(plan.buffer_days, 1.4)       # one whole upload of 30 min, uploads on 5 days a week
        self.assertEqual((plan.suggestion, plan.suggestion_minutes), ([5, 6, 7], 30))
        self.assertEqual(plan.next_upload, date(2026, 10, 26))

    def test_uploaded_chapters_are_no_longer_buffer_and_busy_ones_are_not_suggested(self):
        self.waiting_item(project=self.slug, chapters=[4], status="private", video_id="v")
        plan = planning.build(self.slug, {5})
        self.assertEqual(plan.uploaded, {4})
        self.assertEqual(plan.buffer_minutes, 30)
        self.assertEqual(plan.suggestion, [6, 7, 8])

    def test_a_project_without_rendered_chapters_assumes_a_length(self):
        fresh = planning.build(self.project("Fresh", chapters=6), set())
        self.assertEqual((fresh.avg_minutes, fresh.per_upload, fresh.buffer_days, fresh.suggestion), (8, 4, 0, [1, 2, 3, 4]))

    def test_plan_command_shows_every_projects_next_upload_buffer_and_next_chapters(self):
        self.project("Other", channel=False)
        self.say("/plan")
        text = self.texts()[0]
        for expected in ("Plan Series", "Uploads Mon, Tue, Wed, Thu, Fri, at least 30 min each",
                         "10.0 min on average, 3 per upload", "Next upload: Mon 26 Oct",
                         "Buffer: 1.4 days - 40.0 min ready (1 queued, 2 merged, 1 finished)",
                         "Next to make: chapters 5-7, about 30.0 min", "Other\n  no YouTube channel"):
            self.assertIn(expected, text)

    def test_settings_are_validated(self):
        def save(plan):
            return self.client.post(f"/api/projects/{self.slug}/settings", json={"plan": plan}).get_json()["settings"]["plan"]

        self.assertEqual(save({"days": [4, 4, 9, 0], "min_minutes": 5000}), {"days": [0, 4], "min_minutes": 600})
        self.assertEqual(save({"days": []}), {"days": list(range(7)), "min_minutes": 600})
        self.assertEqual(projects.default_settings()["plan"], {"days": list(range(7)), "min_minutes": 30})


class Suggestions(PlannerCase):
    def setUp(self):
        super().setUp()
        self.project("Alpha", chapters=8, finished=(1, 2, 3, 4, 5, 6))
        self.beta = self.project("Beta", chapters=8, finished=(1, 2, 3))
        self.usage = {"five_hour": {"percent": 10, "resets_at": time.time() + 3600},
                      "seven_day": {"percent": 10, "resets_at": time.time() + 86400}, "seen_at": time.time()}
        for target, name, value in ((advisor.claude_usage, "snapshot", lambda: self.usage),
                                    (advisor.claude_cli, "cli_status", lambda: {"ready": True}),
                                    (advisor, "pipeline_running", lambda: False)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_project_with_the_smallest_buffer_is_suggested_with_run_and_later(self):
        advisor.suggest()
        self.assertEqual(self.texts(), ["Beta needs content: 1 day of video ready (30.0 min).\nSuggested: chapters 4-6, about 30.0 min."])
        self.assertEqual(list(self.buttons()), ["Run", "Later"])

    def test_run_starts_the_suggested_chapters(self):
        advisor.suggest()
        with mock.patch.object(remote, "start_batch") as start:
            self.assertEqual(self.tap("Run"), "Started.")
        self.assertEqual((start.call_args.args[0], set(start.call_args.args[1])), (self.beta, {4, 5, 6}))
        self.assertIn("Started Beta, chapters 4-6.", self.texts()[-1])
        self.assertEqual(self.cleared_keyboards(), 1)

    def test_nothing_is_suggested_while_a_pipeline_runs(self):
        with mock.patch.object(advisor, "pipeline_running", return_value=True):
            advisor.suggest()
        self.assertEqual(self.api_calls, [])

    def test_nothing_is_suggested_when_claude_usage_has_reached_the_projects_limit(self):
        self.usage["five_hour"]["percent"] = 75
        advisor.suggest()
        self.assertEqual(self.api_calls, [])
        self.usage["five_hour"]["percent"] = 20
        advisor.suggest()
        self.assertEqual(len(self.sent()), 1)

    def test_a_window_that_has_reset_counts_as_empty(self):
        del self.usage["five_hour"]
        advisor.suggest()
        self.assertEqual(len(self.sent()), 1)

    def test_nothing_is_suggested_when_claude_is_not_ready(self):
        with mock.patch.object(advisor.claude_cli, "cli_status", return_value={"ready": False}):
            advisor.suggest()
        self.assertEqual(self.api_calls, [])

    def test_a_project_with_enough_video_ready_is_left_alone(self):
        with mock.patch.object(advisor, "SUGGEST_BELOW_DAYS", 1):
            advisor.suggest()
        self.assertEqual(self.api_calls, [])

    def test_after_later_the_same_suggestion_waits_until_the_situation_changes_or_hours_pass(self):
        advisor.suggest()
        self.assertEqual(self.tap("Later"), "Not now.")
        advisor.suggest()
        self.assertEqual(len(self.sent()), 1)
        touch(projects.chapter_video_path(self.beta, 4))
        projects.set_status(self.beta, 4, "rendered")           # the situation changed: the next chapters are different
        advisor.suggest()
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("chapters 5-7", self.texts()[-1])
        advisor.suggest()
        self.assertEqual(len(self.sent()), 2)
        with mock.patch.object(advisor.time, "time", return_value=time.time() + advisor.REPEAT_AFTER_S + 60):
            advisor.suggest()
        self.assertEqual(len(self.sent()), 3)

    def test_nothing_is_sent_to_a_chat_that_is_not_paired(self):
        telegram._save(chat_id=None)
        advisor.suggest()
        self.assertEqual(self.api_calls, [])


class MergeApproval(PlannerCase):
    def setUp(self):
        super().setUp()
        self.slug = self.project("Merge Series", chapters=8, finished=range(1, 7), min_minutes=30)
        self.merged, self.queued = [], []
        for target, name, value in (
            (autopublish, "merge_videos", lambda slug, numbers: self.merged.append(numbers) or "merged_x.mp4"),
            (autopublish.enqueue, "full_video", lambda slug, name: self.queued.append(name)),
            (autopublish.threading, "Thread", ImmediateThread), (batching.threading, "Thread", ImmediateThread),
            (batching, "start_shorts", mock.Mock()),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def finish_pipeline(self) -> None:
        batching._pipeline_finished(self.slug, mock.Mock(chapters=[{"n": n, "state": "done", "plan": ["video"]} for n in range(1, 7)]))

    def test_a_finished_pipeline_proposes_the_chapters_that_reach_the_minimum(self):
        self.finish_pipeline()
        self.assertEqual(self.texts(), ["Merge Series: ready to merge into one video\nChapters 1-3 (3)\n"
                                        "Total 30.0 min (at least 30 min per upload)"])
        self.assertEqual(list(self.buttons()), ["Merge", "Change range", "Later"])

    def test_merge_adds_the_video_to_the_upload_approval(self):
        self.finish_pipeline()
        self.assertEqual(self.tap("Merge"), "Merging; the upload approval follows here.")
        self.assertEqual((self.merged, self.queued), ([[1, 2, 3]], ["merged_x.mp4"]))

    def test_later_merges_nothing(self):
        self.finish_pipeline()
        self.assertEqual(self.tap("Later"), "Not now.")
        self.assertEqual(self.merged, [])

    def test_chapters_already_merged_or_queued_are_skipped(self):
        touch(projects.merged_dir(self.slug) / "merged_20260101-000000_ch1-2.mp4", b"video")
        self.waiting_item(project=self.slug, chapters=[3])
        self.finish_pipeline()
        self.assertIn("Chapters 4-6 (3)", self.texts()[0])

    def test_change_range_asks_for_chapters_and_replies_with_their_total_length_before_merging(self):
        self.finish_pipeline()
        self.assertEqual(self.tap("Change range"), "Send the chapters.")
        self.assertIn("Which chapters should be merged?", self.texts()[-1])
        self.say("2-5")
        self.assertEqual(self.texts()[-1], "Merge Series: ready to merge into one video\nChapters 2-5 (4)\n"
                                           "Total 40.0 min (at least 30 min per upload)")
        self.assertEqual(self.merged, [])
        self.tap("Merge")
        self.assertEqual(self.merged, [[2, 3, 4, 5]])

    def test_a_range_that_cannot_be_merged_is_asked_for_again(self):
        self.finish_pipeline()
        self.tap("Change range")
        for wrong, problem in (("abc", "'abc' is not a chapter"), ("99", "has no chapter 99"), ("7", "Chapter 7 has no finished video")):
            self.say(wrong)
            self.assertIn(problem, self.texts()[-1])
            self.assertIn("Which chapters should be merged?", self.texts()[-1])
        self.say("1-2")
        self.assertIn("this is under it", self.texts()[-1])

    def test_a_command_ends_the_wait_for_a_range(self):
        self.finish_pipeline()
        self.tap("Change range")
        self.say("/status")
        self.say("2-5")
        self.assertNotIn("Chapters 2-5", "\n".join(self.texts()))

    def test_chapters_queued_since_the_proposal_are_refused_at_merge_time(self):
        self.finish_pipeline()
        self.waiting_item(project=self.slug, chapters=[2])
        self.assertEqual(self.tap("Merge"), "Chapter 2 is already in an upload.")
        self.assertEqual(self.merged, [])

    def test_too_little_finished_video_is_reported_without_buttons(self):
        slug = self.project("Short Series", chapters=4, finished=(1, 2), min_minutes=30)
        batching._pipeline_finished(slug, mock.Mock(chapters=[]))
        self.assertEqual(self.texts(), ["Short Series: 20.0 min of finished chapters (1-2) is not enough for an upload of 30 min, "
                                        "or a chapter between them is missing."])
        self.assertNotIn("reply_markup", self.sent()[0])

    def test_a_project_without_a_channel_gets_no_proposal(self):
        slug = self.project("Offline", finished=(1, 2, 3), channel=False)
        batching._pipeline_finished(slug, mock.Mock(chapters=[]))
        self.assertEqual(self.api_calls, [])

    def test_run_no_longer_merges_by_itself(self):
        with mock.patch.object(remote, "start_batch"):
            remote.run("Merge Series", [1])
        env = batching.batch_env(self.slug)
        with mock.patch.object(batching.cards, "propose_merge"):
            env.finished(mock.Mock(chapters=[]))
        self.assertEqual(self.merged, [])


if __name__ == "__main__":
    unittest.main()
