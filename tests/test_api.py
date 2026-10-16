"""The HTTP API the UI is built on: chapter list, pipeline-modal step data, bulk actions, delete."""
from __future__ import annotations

import projects
from tests.support import ApiProjects, touch


class ApiTest(ApiProjects):
    def get_project(self, **query) -> dict:
        response = self.client.get(f"/api/projects/{self.slug}", query_string=query)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["project"]


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


class ChapterDetailsTest(ApiTest):
    """Bulk actions can cover chapters beyond the loaded page, so their detail is fetched on demand."""

    def details(self, numbers: str):
        return self.client.get(f"/api/projects/{self.slug}/chapter_details", query_string={"numbers": numbers})

    def test_returns_the_detail_of_the_requested_chapters_only(self):
        self.work_file(9, "download", "c.pdf")
        chapters = self.details("9,11").get_json()["chapters"]
        self.assertEqual([c["n"] for c in chapters], [9, 11])
        self.assertEqual(chapters[0]["steps"]["done"], ["download"])
        self.assertEqual(chapters[0]["status"], "downloaded")
        self.assertEqual(chapters[1]["steps"]["next"], "download")

    def test_chapters_beyond_the_listed_page_are_covered(self):
        listed = {c["n"] for c in self.get_project(limit=3)["chapters"] if "steps" in c}
        self.assertEqual(listed, {0, 1, 2})
        self.assertEqual([c["n"] for c in self.details("10").get_json()["chapters"]], [10])

    def test_unknown_chapters_are_left_out_and_bad_input_is_rejected(self):
        self.assertEqual(self.details("99").get_json()["chapters"], [])
        self.assertEqual(self.details("a,b").status_code, 400)
        self.assertEqual(self.client.get("/api/projects/nope/chapter_details").status_code, 404)

    def test_asking_for_details_creates_no_chapter_folders(self):
        self.details(",".join(map(str, range(12))))
        self.assertEqual(self.chapter_folders(self.slug), [])


class PageAssetsTest(ApiTest):
    """The page is index.html plus its stylesheet and scripts served from static/."""

    def test_the_page_references_its_assets_and_has_nothing_inline(self):
        html = self.client.get("/").get_data(as_text=True)
        for reference in ('href="/static/css/app.css"', 'src="/static/js/icons.js"', 'src="/static/js/app.js"'):
            self.assertIn(reference, html)
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"<script>")

    def test_each_asset_is_served_with_its_type(self):
        for path, mimetype in (("/static/css/app.css", "text/css"), ("/static/js/icons.js", "text/javascript"),
                               ("/static/js/app.js", "text/javascript")):
            response = self.client.get(path)
            self.assertEqual((response.status_code, response.mimetype), (200, mimetype), path)
            self.assertGreater(len(response.get_data()), 1000)

    def test_scripts_load_in_dependency_order(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index("/static/js/icons.js"), html.index("/static/js/app.js"))
