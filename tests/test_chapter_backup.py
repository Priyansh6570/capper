from __future__ import annotations

import shutil
from unittest import mock

import chapter_backup
import projects
from tests.support import ApiProjects, touch


class ChapterBackupTest(ApiProjects):
    def setUp(self):
        super().setUp()
        self.use_shared_folder()
        self.slug = self.new_project("Drive Series", chapters=3)["slug"]
        self.local_ch = projects.chapter_dir(self.slug, 1)
        self.drive_ch = projects.stored_chapter_dir(self.slug, 1)
        touch(self.local_ch / "work" / "frames" / "frame_001a.png", b"f" * 100)
        touch(self.local_ch / "work" / "frames" / "frame_002a.png", b"g" * 50)
        touch(self.local_ch / "work" / "audio" / "L001.wav", b"a" * 200)
        touch(self.local_ch / "work" / "framer" / "mapping.json", b"{}")
        touch(self.local_ch / "output" / "recap.mp4", b"v" * 400)

    def test_project_in_shared_folder_is_shared(self):
        self.assertTrue(chapter_backup.is_shared(self.slug))

    def test_backup_copies_files_and_marks_them_backed_up(self):
        self.assertFalse(chapter_backup.kind_status(self.slug, 1, chapter_backup.KINDS["frames"])["backed_up"])
        result = chapter_backup.backup_kind(self.slug, 1, "frames")
        self.assertEqual(result, {"copied": 2, "bytes": 150})
        self.assertEqual((self.drive_ch / "work" / "frames" / "frame_001a.png").read_bytes(), b"f" * 100)
        self.assertTrue(chapter_backup.kind_status(self.slug, 1, chapter_backup.KINDS["frames"])["backed_up"])
        self.assertEqual(chapter_backup.backup_kind(self.slug, 1, "frames")["copied"], 0)  # nothing changed

    def test_a_changed_file_is_no_longer_backed_up(self):
        chapter_backup.backup_kind(self.slug, 1, "frames")
        touch(self.local_ch / "work" / "frames" / "frame_001a.png", b"changed!" * 20)
        self.assertFalse(chapter_backup.kind_status(self.slug, 1, chapter_backup.KINDS["frames"])["backed_up"])
        with self.assertRaises(ValueError):
            chapter_backup.clear_kind(self.slug, 1, "frames")
        self.assertTrue((self.local_ch / "work" / "frames" / "frame_001a.png").exists())

    def test_clear_needs_a_backup_and_frees_the_files(self):
        with self.assertRaises(ValueError):
            chapter_backup.clear_kind(self.slug, 1, "audio")
        chapter_backup.backup_kind(self.slug, 1, "audio")
        self.assertEqual(chapter_backup.clear_kind(self.slug, 1, "audio"), 200)
        self.assertFalse((self.local_ch / "work" / "audio").exists())
        self.assertEqual(chapter_backup.cleared_kinds(self.slug, 1), ["audio"])

    def test_the_script_is_never_cleared(self):
        chapter_backup.backup_kind(self.slug, 1, "script")
        with self.assertRaises(ValueError):
            chapter_backup.clear_kind(self.slug, 1, "script")
        self.assertTrue((self.local_ch / "work" / "framer" / "mapping.json").exists())

    def test_using_a_cleared_chapter_restores_it(self):
        for kind in ("frames", "video"):
            chapter_backup.backup_kind(self.slug, 1, kind)
            chapter_backup.clear_kind(self.slug, 1, kind)
        self.assertFalse((self.local_ch / "output" / "recap.mp4").exists())
        projects.chapter_output_dir(self.slug, 1)               # what any route/job does first
        self.assertEqual((self.local_ch / "output" / "recap.mp4").read_bytes(), b"v" * 400)
        self.assertEqual((self.local_ch / "work" / "frames" / "frame_002a.png").read_bytes(), b"g" * 50)
        self.assertEqual(chapter_backup.cleared_kinds(self.slug, 1), [])

    def test_restore_reports_an_unreachable_drive(self):
        chapter_backup.backup_kind(self.slug, 1, "audio")
        chapter_backup.clear_kind(self.slug, 1, "audio")
        shutil.rmtree(self.drive_ch)
        with self.assertRaises(chapter_backup.BackupUnavailable):
            chapter_backup.restore(self.slug, 1)
        self.assertEqual(chapter_backup.cleared_kinds(self.slug, 1), ["audio"])   # still marked, retry later

    def test_local_project_has_nothing_to_back_up(self):
        with mock.patch.object(chapter_backup, "is_shared", return_value=False):
            self.assertFalse(chapter_backup.is_shared(self.slug))


class BackupApiTest(ChapterBackupTest):
    def test_status_lists_chapters_with_local_files(self):
        data = self.client.get(f"/api/projects/{self.slug}/backup").get_json()
        self.assertTrue(data["shared"])
        [chapter] = data["chapters"]
        self.assertEqual(chapter["n"], 1)
        self.assertEqual(chapter["kinds"]["frames"]["local_bytes"], 150)
        self.assertFalse(chapter["kinds"]["frames"]["backed_up"])

    def test_clear_endpoint_skips_what_is_not_backed_up(self):
        chapter_backup.backup_kind(self.slug, 1, "audio")
        body = {"chapters": [1], "kinds": ["audio", "frames"]}
        data = self.client.post(f"/api/projects/{self.slug}/backup_clear", json=body).get_json()
        self.assertEqual(data["freed"], 200)
        self.assertEqual([s["kind"] for s in data["skipped"]], ["frames"])

    def test_stream_backs_up_the_chosen_kinds(self):
        reply = self.client.get(f"/api/projects/{self.slug}/backup_stream?chapters=1&kinds=frames,audio")
        self.assertIn('"type": "done"', reply.get_data(as_text=True))
        self.assertTrue((self.drive_ch / "work" / "audio" / "L001.wav").exists())
        self.assertFalse((self.drive_ch / "output").exists())
