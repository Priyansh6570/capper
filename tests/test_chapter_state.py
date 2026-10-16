from __future__ import annotations

import projects
from tests.support import IsolatedProjects, touch


class ChapterStateTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.slug = self.new_project(chapters=4)["slug"]

    def status(self, n: int) -> str:
        return projects.get_chapter(projects.load(self.slug), n)["status"]

    def test_status_only_moves_forward_unless_forced(self):
        projects.set_status(self.slug, 1, "boxed")
        projects.set_status(self.slug, 1, "downloaded")
        self.assertEqual(self.status(1), "boxed")
        projects.set_status(self.slug, 1, "not_started", force=True)
        self.assertEqual(self.status(1), "not_started")

    def test_unknown_status_and_chapter_are_rejected(self):
        with self.assertRaises(ValueError):
            projects.set_status(self.slug, 1, "nonsense")
        with self.assertRaises(KeyError):
            projects.set_status(self.slug, 99, "boxed")

    def test_derive_all_reads_progress_from_disk(self):
        touch(projects.chapter_dir(self.slug, 0) / "work" / "download" / "c.pdf")
        touch(projects.chapter_dir(self.slug, 1) / "work" / "framer" / "mapping.json")
        touch(projects.chapter_dir(self.slug, 2) / "output" / "recap.mp4")
        derived = {c["n"]: c["status"] for c in projects.derive_all(self.slug)["chapters"]}
        self.assertEqual(derived, {0: "downloaded", 1: "boxed", 2: "rendered", 3: "not_started"})

    def test_derived_status_is_stored(self):
        touch(projects.chapter_dir(self.slug, 0) / "work" / "download" / "c.pdf")
        projects.derive_all(self.slug)
        self.assertEqual(self.status(0), "downloaded")

    def test_derive_never_downgrades_a_chapter_with_its_video(self):
        touch(projects.chapter_dir(self.slug, 0) / "output" / "recap.mp4")
        projects.set_status(self.slug, 0, "complete")
        projects.derive_all(self.slug)
        self.assertEqual(self.status(0), "complete")

    def test_deleted_video_drops_complete_back_to_what_disk_supports(self):
        touch(projects.chapter_dir(self.slug, 0) / "work" / "download" / "c.pdf")
        projects.set_status(self.slug, 0, "complete", force=True)
        projects.derive_all(self.slug)
        self.assertEqual(self.status(0), "downloaded")

    def test_derive_status_matches_derive_all_for_one_chapter(self):
        touch(projects.chapter_dir(self.slug, 3) / "work" / "framer" / "mapping.json")
        self.assertEqual(projects.derive_status(self.slug, 3)["status"], "boxed")
