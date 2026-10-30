"""YouTube publishing and the Telegram remote, against a fake YouTube and a fake Telegram."""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

import branding
import projects
from publishing import (channels, enqueue, labels, oauth, pacific, quota, slots, store, sync, telegram, uploads,
                        videos, worker, youtube_api)
from services import autopublish, batching, merging, remote
from tests.support import ApiProjects, touch

CLIENT = {"id": "client-1", "secret": "s", "token_uri": "https://token.test", "auth_uri": "https://auth.test"}


class FakeYouTube:
    """Stands in for youtube_api.Client: records calls, answers like YouTube."""

    def __init__(self, privacy: str = "private"):
        self.client, self.calls, self.privacy_result = CLIENT, [], privacy
        self.fail_upload: Exception | None = None
        self.can_publish = True
        self.public: set[str] = set()

    def upload(self, path, snippet, status, *, session_url, on_session, progress):
        if self.fail_upload:
            raise self.fail_upload
        self.calls.append(("upload", snippet, status))
        on_session("https://session.test")
        progress(1.0)
        return {"id": "vid1", "status": {"privacyStatus": self.privacy_result}}

    def set_thumbnail(self, video_id, jpeg):
        self.calls.append(("thumbnail", video_id, len(jpeg)))

    def playlist_id(self, title):
        self.calls.append(("playlist", title))
        return "PL1"

    def add_to_playlist(self, playlist, video_id):
        self.calls.append(("add", playlist, video_id))

    def set_privacy(self, video_id, privacy):
        self.calls.append(("privacy", video_id, privacy))
        return privacy if self.can_publish else "private"

    def videos(self, ids, part="snippet,status,statistics"):
        return [{"id": i, "status": {"privacyStatus": "public" if i in self.public else "private"},
                 "statistics": {"viewCount": "7"}, "snippet": {"title": "T"}} for i in ids]


class Publishing(ApiProjects):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(store, "PUBLISH_DIR", self.tmp / "publishing")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notices: list[str] = []
        for name, fake in (("notify", lambda text, rows=None: self.notices.append(text)), ("announce_approval", mock.Mock())):
            patcher = mock.patch.object(telegram, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.youtube = FakeYouTube()
        patcher = mock.patch.object(youtube_api.Client, "for_channel", return_value=self.youtube)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, channel_id: str = "UC1", title: str = "Recapper") -> dict:
        channel = channels.new({"id": channel_id, "title": title, "avatar": "a.png"}, CLIENT,
                               {"access": "a", "refresh": "r", "expires_at": time.time() + 3600})
        channels.save(channel)
        return channel

    def configure_project(self, **youtube) -> None:
        projects.update_settings(self.slug, {"youtube": {"channel": "UC1", "title": "{series} Ch {chapters} Part {part}",
                                                         "description": "About {series} ({chapters})", "tags": ["recap", "manhwa"],
                                                         **youtube}})

    def merged(self, name: str = "merged_20240101-000000_ch22-23-24.mp4", duration: float = 600) -> str:
        touch(projects.merged_dir(self.slug) / name, b"video")
        patcher = mock.patch.object(branding, "duration_of", return_value=duration)
        patcher.start()
        self.addCleanup(patcher.stop)
        return name

    def waiting_item(self, **fields) -> dict:
        return uploads.add({"kind": uploads.FULL, "status": "waiting_approval", "project": self.slug, "channel": "UC1",
                            "chapters": [1, 2], "file": str(touch(self.tmp / "v.mp4")), "duration_s": 60, "title": "Title",
                            "description": "Desc", "tags": ["a"], "playlist": "Api Series", "part": 1, "parts": 1, **fields})

    def short_item(self, **fields) -> dict:
        return uploads.add({"kind": uploads.SHORT, "status": "queued", "project": self.slug, "channel": "UC1", "chapters": [1],
                            "file": str(touch(self.tmp / "s.mp4")), "title": "Short", "description": "Desc", "tags": [],
                            "series": "Api Series", "script": [{"text": "line"}], "model": "sonnet", "metadata": "plan",
                            "publish_at": uploads.now_iso(), **fields})


class TestLabels(unittest.TestCase):
    def test_chapter_label_joins_runs(self):
        self.assertEqual(labels.chapter_label([24, 22, 23, 30]), "22-24, 30")
        self.assertEqual(labels.chapter_label([5]), "5")

    def test_parse_chapters(self):
        self.assertEqual(labels.parse_chapters("1, 3 5-7"), [1, 3, 5, 6, 7])
        for bad in ("", "x", "7-5"):
            with self.assertRaises(ValueError):
                labels.parse_chapters(bad)

    def test_placeholders_are_filled_and_unknown_braces_kept(self):
        self.assertEqual(labels.fill("{series} {chapters} #{part} {other}", series="S", chapters="1-2", part=3), "S 1-2 #3 {other}")

    def test_a_split_video_without_part_in_its_title_gets_the_number(self):
        self.assertEqual(labels.part_title("Show 1-9", "{series} {chapters}", 2, 3), "Show 1-9 - Part 2")
        self.assertEqual(labels.part_title("Show P2", "{series} P{part}", 2, 3), "Show P2")
        self.assertEqual(labels.part_title("Show 1-9", "{series} {chapters}", 1, 1), "Show 1-9")

    def test_titles_lose_angle_brackets_and_stop_at_100_characters(self):
        self.assertEqual(labels.clean_title("<b>" + "x" * 200), "b" + "x" * 99)

    def test_tags_are_unique_and_bounded(self):
        self.assertEqual(labels.clean_tags(["A", "a", " b ", "", "c,d"]), ["A", "b", "cd"])
        self.assertLessEqual(sum(len(t) + 3 for t in labels.clean_tags([f"tag{i:04d}" * 5 for i in range(100)])), labels.MAX_TAGS_CHARS)


class TestPacific(unittest.TestCase):
    def test_offset_follows_daylight_saving(self):
        self.assertEqual(pacific.offset(datetime(2025, 7, 1, 12, tzinfo=timezone.utc)), timedelta(hours=-7))
        self.assertEqual(pacific.offset(datetime(2025, 1, 1, 12, tzinfo=timezone.utc)), timedelta(hours=-8))
        self.assertEqual(pacific.offset(datetime(2025, 3, 9, 9, 59, tzinfo=timezone.utc)), timedelta(hours=-8))
        self.assertEqual(pacific.offset(datetime(2025, 3, 9, 10, 0, tzinfo=timezone.utc)), timedelta(hours=-7))
        self.assertEqual(pacific.offset(datetime(2025, 11, 2, 9, 0, tzinfo=timezone.utc)), timedelta(hours=-8))


class TestQuota(Publishing):
    def test_usage_adds_up_and_starts_over_the_next_day(self):
        quota.spend("c", 1600)
        quota.spend("c", 50)
        self.assertEqual(quota.used("c"), 1650)
        self.assertEqual(quota.remaining("c"), quota.DEFAULT_LIMIT - 1650)
        with mock.patch.object(pacific, "today", return_value="2999-01-01"):
            self.assertEqual(quota.used("c"), 0)

    def test_exhausting_leaves_nothing(self):
        quota.exhaust("c")
        self.assertEqual(quota.remaining("c"), 0)
        quota.set_limit(500)
        self.assertEqual(quota.limit(), 500)

    def test_low_threshold(self):
        quota.spend("c", int(quota.limit() * quota.LOW_FRACTION))
        self.assertTrue(quota.is_low("c"))
        self.assertFalse(quota.is_low("other"))


class TestSlots(Publishing):
    def test_slots_spread_through_the_day(self):
        schedule = {"per_day": 3, "from_hour": 9, "to_hour": 21}
        local = [s.astimezone() for s in slots.slots_of(schedule, datetime(2030, 5, 1).date())]
        self.assertEqual([(s.hour, s.minute) for s in local], [(11, 0), (15, 0), (19, 0)])

    def test_each_short_gets_the_next_free_slot(self):
        self.connect()
        first = self.short_item(status="scheduled", publish_at=slots.next_slot("UC1", set()).isoformat())
        used = slots.taken("UC1")
        second = slots.next_slot("UC1", used)
        self.assertGreater(second, datetime.fromisoformat(first["publish_at"]))
        self.assertNotIn(second, used)

    def test_schedule_is_clamped(self):
        self.assertEqual(slots.clamp_schedule({"per_day": 0, "from_hour": 30, "to_hour": 2}), {"per_day": 1, "from_hour": 23, "to_hour": 24})

    def test_changing_the_schedule_moves_waiting_shorts(self):
        self.connect()
        for _ in range(3):
            slot = slots.next_slot("UC1", slots.taken("UC1"))
            self.short_item(status="scheduled", publish_at=slot.isoformat())
        channels.update("UC1", shorts={"per_day": 1, "from_hour": 6, "to_hour": 7})
        slots.reschedule("UC1")
        days = {datetime.fromisoformat(i["publish_at"]).astimezone().date() for i in uploads.all_items()}
        self.assertEqual(len(days), 3)


class TestEnqueue(Publishing):
    def test_a_merged_video_is_queued_with_the_project_defaults_and_waits_for_approval(self):
        self.connect()
        touch(projects.assets_dir(self.slug) / "thumbnail.png")
        Image.new("RGB", (40, 30), "red").save(projects.assets_dir(self.slug) / "thumbnail.png")
        self.configure_project(thumbnail="assets/thumbnail.png")
        items = enqueue.full_video(self.slug, self.merged())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["status"], item["channel"], item["parts"]), ("waiting_approval", "UC1", 1))
        self.assertEqual(item["title"], "Api Series Ch 22-24 Part 1")
        self.assertEqual(item["description"], "About Api Series (22-24)")
        self.assertEqual((item["tags"], item["playlist"], item["chapters"]), (["recap", "manhwa"], "Api Series", [22, 23, 24]))
        self.assertTrue(item["thumbnail"].endswith("thumbnail.png"))
        telegram.announce_approval.assert_called_once()

    def test_the_branded_version_is_uploaded_when_there_is_one(self):
        self.connect()
        self.configure_project()
        name = self.merged()
        touch(projects.merged_dir(self.slug) / "merged_20240101-000000_ch22-23-24_branded.mp4", b"branded")
        item = enqueue.full_video(self.slug, name)[0]
        self.assertTrue(item["file"].endswith("_branded.mp4"))

    def test_the_same_video_is_not_queued_twice(self):
        self.connect()
        self.configure_project()
        name = self.merged()
        enqueue.full_video(self.slug, name)
        with self.assertRaises(enqueue.Refused):
            enqueue.full_video(self.slug, name)

    def test_a_project_without_a_channel_is_refused(self):
        with self.assertRaises(enqueue.Refused):
            enqueue.full_video(self.slug, self.merged())

    def test_a_video_longer_than_11h55_is_split_into_parts_each_with_its_number(self):
        self.connect()
        self.configure_project()
        name = self.merged(duration=3 * 11 * 3600)
        parts = [touch(self.tmp / f"part_{n}.mp4") for n in (1, 2, 3)]
        with mock.patch.object(videos, "split", return_value=parts) as split:
            items = enqueue.full_video(self.slug, name)
        self.assertEqual(split.call_args.args[2], 3)
        self.assertEqual([i["title"] for i in items], [f"Api Series Ch 22-24 Part {n}" for n in (1, 2, 3)])
        self.assertEqual([(i["part"], i["parts"]) for i in items], [(1, 3), (2, 3), (3, 3)])

    def test_part_count_respects_the_limit(self):
        self.assertEqual(videos.part_count(videos.MAX_PART_S), 1)
        self.assertEqual(videos.part_count(videos.MAX_PART_S + 1), 2)

    def write_shorts(self, n: int, count: int) -> None:
        folder = projects.chapter_output_path(self.slug, n) / "shorts"
        folder.mkdir(parents=True, exist_ok=True)
        made = []
        for i in range(1, count + 1):
            touch(folder / f"short_{i:02d}.mp4")
            made.append({"id": i, "file": f"short_{i:02d}.mp4", "title": f"Moment {i}", "description": "d",
                         "script": [{"text": "hello"}]})
        (folder / "shorts.json").write_text(json.dumps({"shorts": made}), encoding="utf-8")

    def test_rendered_shorts_are_scheduled_in_separate_slots_without_approval(self):
        self.connect()
        self.configure_project()
        self.write_shorts(1, 3)
        queued = enqueue.shorts_of_chapter(self.slug, 1)
        self.assertEqual([i["status"] for i in queued], ["scheduled"] * 3)
        self.assertEqual(len({i["publish_at"] for i in queued}), 3)
        self.assertEqual({i["metadata"] for i in queued}, {"pending"})

    def test_rendering_a_chapter_again_replaces_its_unsent_shorts_but_keeps_uploaded_ones(self):
        self.connect()
        self.configure_project()
        self.write_shorts(1, 2)
        first = enqueue.shorts_of_chapter(self.slug, 1)
        uploads.update(first[0]["id"], status="live", video_id="v")
        enqueue.shorts_of_chapter(self.slug, 1)
        statuses = sorted(i["status"] for i in uploads.all_items())
        self.assertEqual(statuses, ["live", "scheduled", "scheduled"])

    def test_shorts_are_not_queued_when_switched_off_or_without_a_channel(self):
        self.write_shorts(1, 1)
        self.assertEqual(enqueue.shorts_of_chapter(self.slug, 1), [])
        self.connect()
        self.configure_project(auto_shorts=False)
        self.assertEqual(enqueue.shorts_of_chapter(self.slug, 1), [])


class TestShortsSwitchedOff(Publishing):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_switching_shorts_uploads_off_deletes_the_unsent_shorts_but_not_uploaded_ones(self):
        scheduled = self.short_item(status="scheduled")
        queued = self.short_item()
        live = self.short_item(status="live")
        full = self.waiting_item()
        self.client.post(f"/api/projects/{self.slug}/settings", json={"youtube": {"auto_shorts": False}})
        left = {i["id"] for i in uploads.all_items()}
        self.assertEqual(left, {live["id"], full["id"]})
        self.assertFalse({scheduled["id"], queued["id"]} & left)

    def test_the_worker_never_sends_a_short_of_a_project_with_shorts_uploads_off(self):
        item = self.short_item(status="scheduled", publish_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        projects.update_settings(self.slug, {"youtube": {"auto_shorts": False}})
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "scheduled")
        queued = self.short_item()
        worker.tick()
        self.assertEqual(uploads.get(queued["id"])["status"], "queued")


class TestWorker(Publishing):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_an_approved_full_video_uploads_private_with_its_metadata_thumbnail_and_playlist(self):
        thumb = self.tmp / "t.png"
        Image.new("RGB", (40, 30), "blue").save(thumb)
        item = self.waiting_item(thumbnail=str(thumb))
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "waiting_approval")   # nothing uploads unapproved
        self.assertEqual(self.youtube.calls, [])
        uploads.decide(item["id"], True)
        worker.tick()
        done = uploads.get(item["id"])
        self.assertEqual((done["status"], done["video_id"]), ("private", "vid1"))
        kinds = [c[0] for c in self.youtube.calls]
        self.assertEqual(kinds, ["upload", "thumbnail", "playlist", "add"])
        _, snippet, status = self.youtube.calls[0]
        self.assertEqual((snippet["title"], snippet["description"], snippet["tags"]), ("Title", "Desc", ["a"]))
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(channels.get("UC1")["playlists"], {"Api Series": "PL1"})
        self.assertIn("Uploaded as private", self.notices[0])

    def test_the_playlist_is_created_once_and_reused(self):
        for _ in range(2):
            uploads.decide(self.waiting_item()["id"], True)
            worker.tick()
        self.assertEqual([c[0] for c in self.youtube.calls].count("playlist"), 1)

    def test_a_skipped_video_never_uploads(self):
        item = self.waiting_item()
        uploads.decide(item["id"], False)
        worker.tick()
        self.assertEqual((uploads.get(item["id"])["status"], self.youtube.calls), ("skipped", []))

    def test_a_short_uploads_public_with_the_shorts_hashtag(self):
        self.youtube.privacy_result = "public"
        item = self.short_item()
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "live")
        _, snippet, status = self.youtube.calls[0]
        self.assertEqual(status["privacyStatus"], "public")
        self.assertTrue(snippet["description"].endswith("#Shorts"))
        self.assertIn("Short published", self.notices[-1])

    def test_a_short_waits_for_its_slot(self):
        item = self.short_item(status="scheduled", publish_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds"))
        worker.tick()
        self.assertEqual((uploads.get(item["id"])["status"], self.youtube.calls), ("scheduled", []))
        uploads.update(item["id"], publish_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"))
        worker.tick()
        self.assertNotEqual(uploads.get(item["id"])["status"], "scheduled")

    def test_a_short_that_youtube_forces_private_is_flagged_and_published_when_the_lock_lifts(self):
        item = self.short_item()
        worker.tick()
        locked = uploads.get(item["id"])
        self.assertEqual((locked["status"], locked["locked"]), ("private", True))
        self.assertTrue(channels.get("UC1")["public_locked"])
        self.assertIn("Studio", self.notices[-1])

        self.youtube.can_publish = False
        self.assertEqual(sync.lift_lock("UC1"), [])
        self.assertEqual(uploads.get(item["id"])["status"], "private")

        self.youtube.can_publish = True
        self.assertEqual([i["id"] for i in sync.lift_lock("UC1")], [item["id"]])
        self.assertEqual((uploads.get(item["id"])["status"], channels.get("UC1")["public_locked"]), ("live", False))

    def test_a_locked_channel_is_probed_only_now_and_then(self):
        self.short_item(status="private", locked=True, video_id="v", uploaded_at=uploads.now_iso())
        channels.update("UC1", public_locked=True, lock_checked_at=time.time())
        self.assertFalse(sync.lock_check_due(channels.get("UC1")))
        channels.update("UC1", lock_checked_at=time.time() - sync.LOCK_CHECK_EVERY_S - 1)
        self.assertTrue(sync.lock_check_due(channels.get("UC1")))

    def test_videos_made_public_in_studio_become_live(self):
        item = self.waiting_item(status="private", video_id="vid9", uploaded_at=uploads.now_iso())
        self.assertEqual(sync.refresh_visibility("UC1"), [])
        self.youtube.public.add("vid9")
        self.assertEqual([i["id"] for i in sync.refresh_visibility("UC1")], [item["id"]])
        self.assertEqual(uploads.get(item["id"])["status"], "live")

    def test_nothing_uploads_when_the_quota_cannot_cover_it_and_it_goes_ahead_after_the_reset(self):
        item = self.waiting_item(status="queued")
        quota.spend(CLIENT["id"], quota.limit() - worker.UPLOAD_NEEDS + 1)
        worker.tick()
        self.assertEqual((uploads.get(item["id"])["status"], self.youtube.calls), ("queued", []))
        with mock.patch.object(pacific, "today", return_value="2999-01-01"):
            worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "private")

    def test_a_quota_error_waits_for_the_reset_without_counting_as_a_failed_attempt(self):
        item = self.waiting_item(status="queued")
        self.youtube.fail_upload = youtube_api.QuotaExceeded(403, "quotaExceeded", "out")
        worker.tick()
        waiting = uploads.get(item["id"])
        self.assertEqual((waiting["status"], waiting["attempts"]), ("queued", 0))
        self.assertGreater(datetime.fromisoformat(waiting["next_attempt_at"]), datetime.now(timezone.utc))
        self.youtube.fail_upload = None
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "queued")   # still waiting for the reset

    def test_a_failed_upload_is_retried_with_a_delay_and_fails_for_good_on_a_bad_request(self):
        item = self.waiting_item(status="queued")
        self.youtube.fail_upload = youtube_api.ApiError(503, "backendError", "try later")
        worker.tick()
        retry = uploads.get(item["id"])
        self.assertEqual((retry["status"], retry["attempts"], retry["error"]), ("queued", 1, "try later"))
        self.assertGreater(datetime.fromisoformat(retry["next_attempt_at"]), datetime.now(timezone.utc))
        worker.tick()                                   # the delay has not passed
        self.assertEqual(uploads.get(item["id"])["attempts"], 1)

        self.youtube.fail_upload = youtube_api.ApiError(400, "invalidTitle", "bad title")
        uploads.update(item["id"], next_attempt_at=None)
        worker.tick()
        failed = uploads.get(item["id"])
        self.assertEqual((failed["status"], failed["error"]), ("failed", "bad title"))
        self.assertIn("Upload failed", self.notices[-1])
        uploads.retry(item["id"])
        self.assertEqual((uploads.get(item["id"])["status"], uploads.get(item["id"])["attempts"]), ("queued", 0))

    def test_a_missing_file_fails_without_retrying(self):
        item = self.waiting_item(status="queued", file=str(self.tmp / "gone.mp4"))
        self.youtube.fail_upload = FileNotFoundError()
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "failed")

    def test_a_refused_token_asks_for_a_reconnect_and_stops_uploading(self):
        item = self.waiting_item(status="queued")
        self.youtube.fail_upload = youtube_api.AuthError(400, "invalid_grant", "revoked")
        worker.tick()
        self.assertTrue(channels.get("UC1")["needs_reconnect"])
        self.assertEqual(uploads.get(item["id"])["status"], "failed")

    def test_an_item_of_a_disconnected_channel_fails(self):
        item = self.waiting_item(status="queued", channel="UC-gone")
        worker.tick()
        self.assertEqual(uploads.get(item["id"])["status"], "failed")

    def test_a_thumbnail_problem_does_not_undo_the_upload(self):
        thumb = self.tmp / "t.png"
        Image.new("RGB", (40, 30)).save(thumb)
        item = self.waiting_item(status="queued", thumbnail=str(thumb))
        self.youtube.set_thumbnail = mock.Mock(side_effect=youtube_api.ApiError(403, "forbidden", "needs verification"))
        worker.tick()
        done = uploads.get(item["id"])
        self.assertEqual(done["status"], "private")
        self.assertIn("needs verification", done["warnings"][0])

    def test_a_short_gets_claudes_title_description_and_tags_before_it_uploads(self):
        item = self.short_item(metadata="pending", status="scheduled",
                               publish_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))
        written = {"title": "Claude title", "description": "Claude text", "tags": ["x"]}
        with mock.patch.object(worker.claude_cli, "cli_status", return_value={"ready": True}), \
                mock.patch.object(worker.metadata, "write", return_value=written):
            worker.tick()
        done = uploads.get(item["id"])
        self.assertEqual((done["title"], done["description"], done["tags"], done["metadata"]), ("Claude title", "Claude text", ["x"], "claude"))

    def test_the_plans_text_stays_when_claude_cannot_answer(self):
        item = self.short_item(metadata="pending", status="scheduled",
                               publish_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))
        with mock.patch.object(worker.claude_cli, "cli_status", return_value={"ready": False}):
            worker.tick()
        self.assertEqual((uploads.get(item["id"])["title"], uploads.get(item["id"])["metadata"]), ("Short", "plan"))

    def test_the_quota_warning_is_sent_once_a_day(self):
        quota.set_limit(100_000)
        quota.spend(CLIENT["id"], 80_000)
        uploads.decide(self.waiting_item()["id"], True)
        worker.tick()
        uploads.decide(self.waiting_item(title="Two")["id"], True)
        worker.tick()
        self.assertEqual(sum("running low" in n for n in self.notices), 1)

    def test_the_evening_summary_is_sent_once(self):
        self.youtube.report = mock.Mock(return_value=[{"views": 5, "estimatedMinutesWatched": 120, "subscribersGained": 2}])
        with mock.patch.object(telegram, "state", return_value={"paired": True}), \
                mock.patch.object(worker, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2030, 5, 5, 21, 0)
            worker._daily_summary()
            worker._daily_summary()
        summaries = [n for n in self.notices if n.startswith("Daily summary")]
        self.assertEqual(len(summaries), 1)
        self.assertIn("Views: 5", summaries[0])

    def test_uploads_in_progress_when_the_app_stopped_are_queued_again(self):
        item = self.waiting_item(status="uploading")
        with mock.patch.object(worker.threading, "Thread"):
            worker.start()
        self.assertEqual(uploads.get(item["id"])["status"], "queued")


class TestResumableUpload(unittest.TestCase):
    def test_the_file_is_sent_in_chunks_and_resumes_after_an_interruption(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "v.bin"
        path.write_bytes(b"x" * 10)
        sent = []

        def put(url, data=None, headers=None, timeout=None):
            sent.append(headers["Content-Range"])
            if headers["Content-Range"] == "bytes */10":
                return mock.Mock(status_code=308, headers={"Range": "bytes=0-3"})
            if headers["Content-Range"] == "bytes 4-9/10":
                return mock.Mock(status_code=200, json=lambda: {"id": "done"})
            return mock.Mock(status_code=308, headers={"Range": "bytes=0-3"})

        client = youtube_api.Client(CLIENT, {"access": "a", "refresh": "r", "expires_at": time.time() + 999}, lambda t: None)
        progress = []
        with mock.patch.object(youtube_api.requests, "put", side_effect=put):
            self.assertEqual(client._send_file("https://session", path, progress.append), {"id": "done"})
        self.assertEqual(sent, ["bytes */10", "bytes 4-9/10"])

    def test_an_expired_session_is_reported(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "v.bin"
        path.write_bytes(b"x")
        client = youtube_api.Client(CLIENT, {"access": "a", "refresh": "r", "expires_at": time.time() + 999}, lambda t: None)
        with mock.patch.object(youtube_api.requests, "put", return_value=mock.Mock(status_code=404)):
            with self.assertRaises(youtube_api.SessionExpired):
                client._send_file("https://session", path, lambda f: None)

    def test_an_expiring_token_is_refreshed_and_saved(self):
        saved = []
        client = youtube_api.Client(CLIENT, {"access": "old", "refresh": "r", "expires_at": time.time() + 5}, saved.append)
        with mock.patch.object(youtube_api.requests, "post", return_value=mock.Mock(
                content=b"x", status_code=200, json=lambda: {"access_token": "new", "expires_in": 3600})):
            self.assertEqual(client._access(), "new")
        self.assertEqual(saved[0]["refresh"], "r")

    def test_a_refused_refresh_token_is_an_auth_error(self):
        with mock.patch.object(youtube_api.requests, "post", return_value=mock.Mock(
                content=b"x", status_code=400, json=lambda: {"error": "invalid_grant"})):
            with self.assertRaises(youtube_api.AuthError):
                youtube_api.refreshed(CLIENT, "r")

    def test_quota_errors_are_recognised_and_use_up_the_quota(self):
        resp = mock.Mock(ok=False, status_code=403)
        resp.json.return_value = {"error": {"message": "m", "errors": [{"reason": "quotaExceeded"}]}}
        self.assertIsInstance(youtube_api._error(resp), youtube_api.QuotaExceeded)


class TestOAuth(unittest.TestCase):
    def test_the_client_file_must_be_a_desktop_client(self):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        good = folder / "good.json"
        good.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "sec"}}), encoding="utf-8")
        self.assertEqual(oauth.load_client(f'"{good}"')["id"], "id")
        web = folder / "web.json"
        web.write_text(json.dumps({"web": {"client_id": "id", "client_secret": "sec"}}), encoding="utf-8")
        for bad in (str(web), str(folder / "missing.json")):
            with self.assertRaises(ValueError):
                oauth.load_client(bad)

    def test_the_sign_in_asks_for_the_three_scopes_with_pkce_and_offline_access(self):
        sign_in = oauth.SignIn({**CLIENT, "auth_uri": oauth.AUTH_URI})
        sign_in._server.server_close()
        for scope in ("auth/youtube ", "youtube.upload", "yt-analytics.readonly"):
            self.assertIn(scope.strip(), sign_in.url.replace("%2F", "/"))
        for part in ("code_challenge_method=S256", "access_type=offline", "response_type=code"):
            self.assertIn(part, sign_in.url)
        self.assertTrue(sign_in.redirect_uri.startswith("http://127.0.0.1:"))

    def test_a_single_channel_is_saved_and_several_need_a_choice(self):
        with mock.patch.object(store, "PUBLISH_DIR", Path(__import__("tempfile").mkdtemp())):
            sign_in = oauth.SignIn(CLIENT)
            sign_in._server.server_close()
            sign_in._code = "code"
            token = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            with mock.patch.object(oauth.requests, "post", return_value=mock.Mock(json=lambda: token)), \
                    mock.patch.object(youtube_api.Client, "my_channels", return_value=[
                        {"id": "A", "title": "A", "avatar": ""}, {"id": "B", "title": "B", "avatar": ""}]):
                sign_in._finish()
                self.assertEqual(sign_in.state, "choose")
                sign_in.choose("B")
            self.assertEqual(sign_in.state, "done")
            saved = channels.get("B")
            self.assertEqual(saved["token"]["refresh"], "r")
            self.assertNotIn("token", channels.public(saved))
            self.assertNotIn("secret", json.dumps(channels.public(saved)))


class TestTelegram(Publishing):
    def setUp(self):
        super().setUp()
        self.api_calls: list[tuple] = []
        self.runs: list[tuple] = []
        patcher = mock.patch.object(telegram, "_api", side_effect=lambda method, **kw: self.api_calls.append((method, kw)) or {})
        patcher.start()
        self.addCleanup(patcher.stop)
        telegram._controls = telegram.Controls(
            run=lambda p, n: self.runs.append((p, n)) or "started", shorts=lambda p, n: self.runs.append(("shorts", p, n)) or "making",
            plan=lambda: "the plan", status=lambda: "idle", projects=lambda: ["Iron-Blooded Hound", "My Series"],
            act=lambda action, card: telegram.Reaction(f"did {action}", keep_buttons=action == "error"))
        self.addCleanup(setattr, telegram, "_controls", None)
        self.connect()

    def say(self, text: str, chat: int = 42) -> None:
        telegram._handle({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})

    def replies(self) -> list[str]:
        return [kw["text"] for method, kw in self.api_calls if method == "sendMessage"]

    def pair(self) -> None:
        telegram._save(token="tok", chat_id=42)

    def test_the_first_start_pairs_the_chat_and_only_that_chat_is_answered(self):
        telegram._save(token="tok")
        self.say("/queue")
        self.assertEqual(self.api_calls, [])            # not paired: ignored
        self.say("/start", chat=42)
        self.assertEqual(telegram._chat(), 42)
        self.say("/start", chat=99)
        self.say("/queue", chat=99)
        self.assertEqual(telegram._chat(), 42)
        self.assertTrue(all(kw["chat_id"] == 42 for method, kw in self.api_calls))

    def test_approve_skip_and_edit_title_buttons(self):
        self.pair()
        approve, skip, edit = (self.waiting_item(title=t) for t in ("A", "B", "C"))

        def press(action, item):
            telegram._handle({"update_id": 2, "callback_query": {"id": "q", "data": f"{action}:{item['id']}", "message": {
                "chat": {"id": 42}, "message_id": 7}}})

        press("approve", approve)
        press("skip", skip)
        press("title", edit)
        self.assertEqual(uploads.get(approve["id"])["status"], "queued")
        self.assertEqual(uploads.get(skip["id"])["status"], "skipped")
        self.say("A better title")
        self.assertEqual(uploads.get(edit["id"])["title"], "A better title")
        self.assertEqual(uploads.get(edit["id"])["status"], "waiting_approval")
        press("approve", approve)                         # a second press does nothing
        self.assertIn("Already handled.", [kw.get("text") for m, kw in self.api_calls if m == "answerCallbackQuery"])

    def test_queue_sends_a_card_for_each_waiting_item(self):
        self.pair()
        self.waiting_item()
        self.waiting_item()
        self.say("/queue")
        self.assertEqual(telegram.announce_approval.call_count, 2)

    def test_the_card_names_title_channel_length_and_chapters(self):
        card = telegram.card_text(self.waiting_item(duration_s=3725, chapters=[22, 23, 24], part=2, parts=3))
        for expected in ("Title", "Recapper", "1:02:05", "22-24", "part 2 of 3"):
            self.assertIn(expected, card)

    def test_schedule_private_uploaded_and_help(self):
        self.pair()
        self.waiting_item(status="queued", title="Next full")
        self.short_item(status="scheduled", title="Later short")
        done = self.waiting_item(status="private", title="Hidden", video_id="abc", uploaded_at=uploads.now_iso())
        self.youtube.public.add("abc")
        self.short_item(status="private", locked=True, title="Locked", video_id="def", uploaded_at=uploads.now_iso())
        self.say("/schedule")
        self.say("/private")
        self.say("/uploaded")
        text = "\n".join(self.replies())
        for expected in ("Videos (1)", "Shorts (1)", "Next full", "Later short", "Quota today", "https://youtu.be/abc",
                         "publish it in Studio"):
            self.assertIn(expected, text)
        self.assertEqual(uploads.get(done["id"])["status"], "live")   # /private checked YouTube, which says it is public

    def test_stats_report_each_channel_and_recent_views(self):
        self.pair()
        self.waiting_item(status="live", title="Vid", video_id="v1", uploaded_at=uploads.now_iso())
        self.youtube.report = mock.Mock(side_effect=lambda *a, **k: [{"video": "v1", "views": 9}] if k.get("dimensions") else
                                        [{"views": 120, "estimatedMinutesWatched": 600, "subscribersGained": 4}])
        self.say("/stats")
        text = "\n".join(self.replies())
        for expected in ("Recapper - last 7 days", "Views: 120", "Watch time: 10.0 h", "+4", "Top videos", "Vid: 7 views"):
            self.assertIn(expected, text)

    def test_run_starts_the_pipeline_with_the_chapter_range_and_status_reports(self):
        self.pair()
        self.say("/run My Series 22-24")
        self.assertEqual(self.runs, [("My Series", [22, 23, 24])])
        self.say("/status")
        self.say("/run")
        self.assertIn("started", self.replies())
        self.assertIn("idle", self.replies())
        self.assertTrue(any("Usage" in r for r in self.replies()))

    def test_shorts_and_plan_commands(self):
        self.pair()
        self.say("/shorts My Series 22-24")
        self.say("/shorts")
        self.say("/plan")
        self.assertEqual(self.runs, [("shorts", "My Series", [22, 23, 24])])
        self.assertEqual(self.replies()[0], "making")
        self.assertIn("Usage: /shorts", self.replies()[1])
        self.assertEqual(self.replies()[2], "the plan")

    def test_a_planner_button_is_answered_and_its_buttons_removed_unless_it_keeps_them(self):
        self.pair()
        for action in ("later", "error"):
            telegram._handle({"update_id": 2, "callback_query": {"id": "q", "data": f"{action}:card", "message": {
                "chat": {"id": 42}, "message_id": 7}}})
        answers = [kw["text"] for m, kw in self.api_calls if m == "answerCallbackQuery"]
        cleared = [kw["message_id"] for m, kw in self.api_calls if m == "editMessageReplyMarkup"]
        self.assertEqual((answers, len(cleared)), (["did later", "did error"], 1))

    def test_projects_lists_the_names_run_accepts(self):
        self.pair()
        self.say("/projects")
        self.assertEqual(self.replies(), ["Iron-Blooded Hound\nMy Series"])

    def test_run_keeps_a_project_name_with_spaces_and_hyphens_whole(self):
        self.pair()
        self.say("/run Revenge of the Iron-Blooded Sword Hound 11-12")
        self.assertEqual(self.runs, [("Revenge of the Iron-Blooded Sword Hound", [11, 12])])

    def test_an_unknown_command_lists_the_commands(self):
        self.pair()
        self.say("/nope")
        self.assertIn("/queue", self.replies()[0])


class TestRoutes(Publishing):
    def test_state_hides_secrets_and_lists_items(self):
        self.connect()
        self.waiting_item()
        data = self.client.get("/api/publishing").get_json()
        self.assertEqual([c["title"] for c in data["channels"]], ["Recapper"])
        self.assertNotIn("token", json.dumps(data))
        self.assertNotIn("file", data["items"][0])

    def test_items_are_edited_approved_and_removed_through_the_api(self):
        self.connect()
        item = self.waiting_item()
        edited = self.client.patch(f"/api/publishing/items/{item['id']}", json={"title": "New <b>", "tags": ["x", "x", "y"]})
        self.assertEqual((edited.get_json()["item"]["title"], edited.get_json()["item"]["tags"]), ("New b", ["x", "y"]))
        self.assertEqual(self.client.patch(f"/api/publishing/items/{item['id']}", json={"title": " "}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/publishing/items/{item['id']}/approve").status_code, 200)
        self.assertEqual(self.client.post(f"/api/publishing/items/{item['id']}/approve").status_code, 409)
        uploads.update(item["id"], status="private")
        self.assertEqual(self.client.patch(f"/api/publishing/items/{item['id']}", json={"title": "Late"}).status_code, 409)

    def test_a_custom_thumbnail_is_stored_for_the_item(self):
        import io
        self.connect()
        item = self.waiting_item()
        buffer = io.BytesIO()
        Image.new("RGB", (2000, 1200), "green").save(buffer, "PNG")
        buffer.seek(0)
        sent = self.client.post(f"/api/publishing/items/{item['id']}/thumbnail", data={"file": (buffer, "cover.png")})
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(self.client.get(f"/api/publishing/items/{item['id']}/thumbnail").mimetype, "image/jpeg")
        self.assertEqual(self.client.post(f"/api/publishing/items/{item['id']}/thumbnail", data={"file": (io.BytesIO(b"x"), "a.exe")}).status_code, 400)

    def test_the_schedule_and_quota_limit_are_saved(self):
        self.connect()
        self.assertEqual(self.client.post("/api/publishing/channels/UC1/schedule", json={"per_day": 5, "from_hour": 8, "to_hour": 20}).status_code, 200)
        self.assertEqual(channels.get("UC1")["shorts"], {"per_day": 5, "from_hour": 8, "to_hour": 20})
        self.client.post("/api/publishing/quota", json={"limit": 20000})
        self.assertEqual(quota.limit(), 20000)

    def test_a_merged_video_is_queued_from_the_api(self):
        self.connect()
        self.configure_project()
        name = self.merged()
        sent = self.client.post(f"/api/projects/{self.slug}/youtube/queue", json={"merge": name})
        self.assertEqual(sent.get_json()["items"][0]["status"], "waiting_approval")
        self.assertEqual(self.client.post(f"/api/projects/{self.slug}/youtube/queue", json={"merge": name}).status_code, 409)

    def test_project_upload_defaults_are_saved_and_validated(self):
        saved = self.client.post(f"/api/projects/{self.slug}/settings", json={"youtube": {
            "channel": "UC1", "title": "  ", "tags": ["a", "a", "<b>"], "auto_shorts": False}}).get_json()["settings"]["youtube"]
        self.assertEqual((saved["channel"], saved["tags"], saved["auto_shorts"]), ("UC1", ["a", "b"], False))
        self.assertEqual(saved["title"], projects.default_settings()["youtube"]["title"])

    def test_the_thumbnail_asset_is_stored_in_the_project_settings(self):
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (30, 20)).save(buffer, "PNG")
        buffer.seek(0)
        self.client.post(f"/api/projects/{self.slug}/asset", data={"kind": "thumbnail", "file": (buffer, "t.png")})
        self.assertEqual(projects.load(self.slug)["settings"]["youtube"]["thumbnail"], "assets/thumbnail.png")
        self.client.delete(f"/api/projects/{self.slug}/asset/thumbnail")
        self.assertIsNone(projects.load(self.slug)["settings"]["youtube"]["thumbnail"])

    def test_disconnecting_forgets_the_channel(self):
        self.connect()
        with mock.patch("routes.publishing.requests.post"):
            self.assertEqual(self.client.delete("/api/publishing/channels/UC1").status_code, 200)
        self.assertEqual(channels.all_channels(), [])


class TestMerging(Publishing):
    def finish_chapters(self, *numbers: int) -> None:
        for n in numbers:
            touch(projects.chapter_video_path(self.slug, n), b"video")

    def info(self, width: int = 1920) -> dict:
        return {"video": {"width": width, "height": 1080, "codec_name": "h264"}, "audio": {"codec_name": "aac", "sample_rate": "48000"}}

    def run_merge(self, chapters: list[int], infos: list[dict], returncode: int = 0) -> str:
        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"merged")
            self.commands.append(cmd)
            return mock.Mock(returncode=returncode, stderr="boom")

        self.commands: list[list[str]] = []
        with mock.patch.object(merging.branding, "probe", side_effect=infos), \
                mock.patch.object(merging.subprocess, "run", side_effect=fake_run):
            return merging.merge_videos(self.slug, chapters)

    def test_chapters_with_matching_videos_are_joined_by_copying(self):
        self.finish_chapters(3, 4)
        name = self.run_merge([3, 4], [self.info(), self.info()])
        self.assertRegex(name, r"^merged_\d{8}-\d{6}_ch3-4\.mp4$")
        self.assertIn("copy", self.commands[0])
        self.assertTrue((projects.merged_dir(self.slug) / name).is_file())

    def test_videos_that_differ_are_normalised_first(self):
        self.finish_chapters(3, 4)
        self.run_merge([3, 4], [self.info(), self.info(1280)])
        self.assertIn("-filter_complex", self.commands[0])

    def test_a_chapter_without_a_video_is_refused(self):
        self.finish_chapters(3)
        with self.assertRaises(merging.Refused):
            self.run_merge([3, 4], [self.info()])
        with self.assertRaises(merging.Refused):
            self.run_merge([3, 99], [self.info()])

    def test_a_failing_ffmpeg_is_reported(self):
        self.finish_chapters(3, 4)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.run_merge([3, 4], [self.info(), self.info()], returncode=1)

    def test_two_merges_of_a_project_do_not_run_at_once(self):
        with merging.merge_slot(self.slug):
            with self.assertRaises(merging.Refused):
                merging.merge_videos(self.slug, [1, 2])


class TestAutoPublish(Publishing):
    def setUp(self):
        super().setUp()
        self.connect()
        self.configure_project()
        patchers = {"merge": mock.patch.object(autopublish, "merge_videos", return_value="merged_x_ch1-2.mp4"),
                    "queue": mock.patch.object(autopublish.enqueue, "full_video")}
        self.mocks = {name: patcher.start() for name, patcher in patchers.items()}
        for patcher in patchers.values():
            self.addCleanup(patcher.stop)

    def disable_intro_outro(self) -> None:
        projects.update_settings(self.slug, {"branding": {"intro": {"enabled": False}, "outro": {"enabled": False}}})

    def test_finished_chapters_are_merged_and_queued_for_approval(self):
        self.disable_intro_outro()
        autopublish.publish(self.slug, [1, 2])
        self.mocks["merge"].assert_called_once_with(self.slug, [1, 2])
        self.mocks["queue"].assert_called_once_with(self.slug, "merged_x_ch1-2.mp4")
        self.assertEqual(self.notices, [])

    def test_the_intro_and_outro_are_added_before_queueing_when_the_project_wants_them(self):
        def finish(job):
            job.status = "done"

        with mock.patch.object(autopublish, "_enqueue", side_effect=finish) as enqueued:
            autopublish.publish(self.slug, [1, 2])
        self.assertEqual(enqueued.call_args.args[0].what, "brand")
        self.mocks["queue"].assert_called_once()

    def test_a_failed_intro_and_outro_render_stops_the_queueing_and_says_so(self):
        def fail(job):
            job.status, job.message = "failed", "no clip"

        with mock.patch.object(autopublish, "_enqueue", side_effect=fail):
            autopublish.publish(self.slug, [1, 2])
        self.mocks["queue"].assert_not_called()
        self.assertIn("no clip", self.notices[0])

    def test_a_project_without_a_channel_is_told_about_not_merged(self):
        projects.update_settings(self.slug, {"youtube": {"channel": None}})
        autopublish.publish(self.slug, [1, 2])
        self.mocks["merge"].assert_not_called()
        self.assertIn("no YouTube channel", self.notices[0])

    def test_a_merge_that_fails_is_reported(self):
        self.mocks["merge"].side_effect = RuntimeError("ffmpeg exited 1")
        self.disable_intro_outro()
        autopublish.publish(self.slug, [1, 2])
        self.assertIn("ffmpeg exited 1", self.notices[0])


class TestRun(Publishing):
    def test_run_starts_the_chapters_and_leaves_merging_to_the_approval_step(self):
        self.connect()
        self.configure_project()
        with mock.patch.object(remote, "start_batch") as start:
            reply = remote.run("api series", [3, 4])
        self.assertEqual(set(start.call_args.args[1]), {3, 4})
        self.assertEqual(start.call_args.kwargs, {})
        self.assertIn("asked whether to merge", reply)

    def test_run_says_when_the_project_has_no_channel(self):
        with mock.patch.object(remote, "start_batch"):
            self.assertIn("will not be queued", remote.run("Api Series", [3]))

    def test_a_project_name_with_spaces_and_hyphens_is_found(self):
        hound = self.new_project("Revenge of the Iron-Blooded Sword Hound")
        with mock.patch.object(remote, "start_batch") as start:
            remote.run("Revenge of the Iron-Blooded Sword Hound", [1])
            remote.run("iron-blooded sword", [1])
        self.assertEqual([c.args[0] for c in start.call_args_list], [hound["slug"]] * 2)
        self.assertEqual(remote.project_names().count("Revenge of the Iron-Blooded Sword Hound"), 1)

    def test_run_refuses_unknown_projects_and_chapters(self):
        with self.assertRaisesRegex(ValueError, "no project"):
            remote.run("Nothing", [1])
        with self.assertRaisesRegex(ValueError, "no chapter"):
            remote.run("Api Series", [99])


if __name__ == "__main__":
    unittest.main()
