"""The HTTP API the UI is built on: chapter list, pipeline-modal step data, bulk actions, delete."""
from __future__ import annotations

from unittest import mock

import app
import projects
from tests.support import IsolatedProjects, touch


class ApiTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        for name, value in (("PROJECTS_DIR", self.local), ("WORK_DIR", self.work), ("OUTPUT_DIR", self.output)):
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.app.test_client()
        self.slug = self.new_project("Api Series", chapters=12)["slug"]

    def get_project(self, **query) -> dict:
        response = self.client.get(f"/api/projects/{self.slug}", query_string=query)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["project"]

    def work_file(self, n: int, *parts: str, **kwargs):
        return touch(projects.chapter_dir(self.slug, n).joinpath("work", *parts), **kwargs)


class ChapterListTest(ApiTest):
    def test_every_chapter_is_listed_but_only_the_first_page_has_detail(self):
        project = self.get_project(limit=5)
        self.assertEqual(len(project["chapters"]), 12)
        detailed = [c["n"] for c in project["chapters"] if "steps" in c]
        self.assertEqual(detailed, [0, 1, 2, 3, 4])
        self.assertTrue(all("status" in c for c in project["chapters"]))

    def test_without_a_limit_every_chapter_has_detail(self):
        self.assertTrue(all("steps" in c for c in self.get_project()["chapters"]))

    def test_listing_creates_no_chapter_folders(self):
        self.get_project(limit=5)
        self.assertEqual(self.chapter_folders(self.slug), [])

    def test_listing_derives_status_from_disk(self):
        self.work_file(2, "download", "c.pdf")
        statuses = {c["n"]: c["status"] for c in self.get_project(limit=3)["chapters"]}
        self.assertEqual(statuses[2], "downloaded")
        self.assertEqual(statuses[3], "not_started")

    def test_unknown_project_is_a_404(self):
        self.assertEqual(self.client.get("/api/projects/nope").status_code, 404)


class PipelineStepDataTest(ApiTest):
    """What the Run pipeline modal reads: each chapter's done / stale steps and the next one to run."""

    def steps(self, n: int) -> dict:
        return next(c for c in self.get_project()["chapters"] if c["n"] == n)["steps"]

    def test_a_chapter_with_nothing_starts_at_download(self):
        self.assertEqual(self.steps(0), {"done": [], "stale": [], "next": "download"})

    def test_a_downloaded_chapter_is_done_up_to_download(self):
        self.work_file(1, "download", "c.pdf")
        self.assertEqual(self.steps(1), {"done": ["download"], "stale": [], "next": "split"})

    def test_a_step_older_than_an_earlier_one_is_stale(self):
        self.work_file(2, "gemini_parts", "p.pdf", mtime=1_000)
        self.work_file(2, "download", "c.pdf", mtime=2_000)
        steps = self.steps(2)
        self.assertEqual(steps["done"], ["download", "split"])
        self.assertEqual(steps["stale"], ["split"])
        self.assertEqual(steps["next"], "split")

    def test_a_rendered_chapter_counts_every_step_as_done(self):
        touch(projects.chapter_dir(self.slug, 3) / "output" / "recap.mp4")
        self.assertEqual(self.steps(3)["next"], None)

    def test_missing_chapters_endpoint_counts_unstored_downloads(self):
        self.assertEqual(self.client.get(f"/api/projects/{self.slug}/missing_chapters").get_json(),
                         {"count": 12, "total": 12})
        projects.store_pdf(self.slug, 0, touch(self.tmp / "dl" / "c0.pdf"))
        self.assertEqual(self.client.get(f"/api/projects/{self.slug}/missing_chapters").get_json()["count"], 11)


class BulkActionTest(ApiTest):
    def delete_kind(self, chapters: list[int], kind: str, **extra):
        return self.client.post(f"/api/projects/{self.slug}/library/delete",
                                json={"chapters": chapters, "kind": kind, **extra})

    def test_library_delete_dry_run_reports_without_deleting(self):
        for n in (0, 1):
            self.work_file(n, "audio", "a.wav", content=b"12345")
        plan = self.delete_kind([0, 1], "audio", dry_run=True).get_json()
        self.assertEqual((plan["count"], plan["bytes"]), (2, 10))
        self.assertTrue(projects.chapter_dir(self.slug, 0).joinpath("work", "audio", "a.wav").exists())

    def test_library_delete_removes_one_type_across_chapters_and_keeps_the_rest(self):
        for n in (0, 1, 7):
            self.work_file(n, "audio", "a.wav")
            self.work_file(n, "framer", "mapping.json")
        self.assertEqual(self.delete_kind([0, 1, 7], "audio").status_code, 200)
        for n in (0, 1, 7):
            work = projects.chapter_dir(self.slug, n) / "work"
            self.assertFalse((work / "audio" / "a.wav").exists())
            self.assertTrue((work / "framer" / "mapping.json").exists())

    def test_library_delete_rejects_chapters_outside_the_project(self):
        self.assertEqual(self.delete_kind([99], "audio").status_code, 400)

    def test_library_delete_rejects_unknown_types(self):
        self.assertEqual(self.delete_kind([0], "script").status_code, 400)


class DeleteProjectTest(ApiTest):
    def delete(self, **body):
        return self.client.delete(f"/api/projects/{self.slug}", json=body)

    def test_needs_the_exact_project_name(self):
        self.assertEqual(self.delete().status_code, 400)
        self.assertEqual(self.delete(confirm="api series").status_code, 400)
        self.assertTrue(projects.exists(self.slug))

    def test_exact_name_removes_the_project(self):
        self.work_file(1, "download", "c.pdf")
        self.assertEqual(self.delete(confirm="Api Series").status_code, 200)
        self.assertFalse(projects.exists(self.slug))
        self.assertFalse(projects.local_dir(self.slug).exists())
