from __future__ import annotations

from unittest import mock

import bulk_download
import projects
from tests.support import IsolatedProjects, touch


class BulkDownloadTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(bulk_download, "PAUSE_BETWEEN_CHAPTERS_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use_shared_folder()
        self.slug = self.new_project(chapters=8)["slug"]
        self.fetched: list[int] = []
        self.progress: list[tuple[int, int, int]] = []

    def download(self, n: int) -> None:
        self.fetched.append(n)
        projects.store_pdf(self.slug, n, touch(self.tmp / "dl" / f"c{n}.pdf", b"%PDF"))

    def run_bulk(self, count: int, stop=lambda: False, download=None) -> dict:
        return bulk_download.run(self.slug, count, download or self.download,
                                 lambda *args: self.progress.append(args), stop)

    def test_downloads_the_first_missing_chapters_in_order(self):
        result = self.run_bulk(3)
        self.assertEqual(self.fetched, [0, 1, 2])
        self.assertEqual(result, {"requested": 3, "failed": []})
        self.assertEqual(self.progress, [(0, 1, 3), (1, 2, 3), (2, 3, 3)])

    def test_stored_chapters_are_skipped_so_a_stopped_run_resumes(self):
        stops = iter([False, True])
        self.run_bulk(3, stop=lambda: next(stops, True))
        self.assertEqual(self.fetched, [0])
        self.run_bulk(2)
        self.assertEqual(self.fetched, [0, 1, 2])

    def test_failures_are_listed_and_the_run_continues(self):
        def flaky(n: int) -> None:
            if n == 1:
                raise RuntimeError("site said no")
            self.download(n)

        result = self.run_bulk(3, download=flaky)
        self.assertEqual(result["failed"], [{"chapter": 1, "error": "site said no"}])
        self.assertEqual(self.fetched, [0, 2])

    def test_a_failed_chapter_is_retried_by_the_next_run(self):
        def always_fails(n: int) -> None:
            raise RuntimeError("boom")

        self.run_bulk(2, download=always_fails)
        self.run_bulk(2)
        self.assertEqual(self.fetched, [0, 1])

    def test_count_larger_than_what_is_left_downloads_the_rest(self):
        self.run_bulk(5)
        result = self.run_bulk(50)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(bulk_download.missing_chapters(self.slug), [])


class StoredChapterTest(IsolatedProjects):
    def test_store_pdf_leaves_no_partial_file_and_is_found(self):
        self.use_shared_folder()
        slug = self.new_project(chapters=2)["slug"]
        stored = projects.store_pdf(slug, 0, touch(self.tmp / "dl" / "c0.pdf", b"%PDF-data"))
        self.assertEqual(projects.stored_pdf(slug, 0), stored)
        self.assertEqual(stored.read_bytes(), b"%PDF-data")
        self.assertEqual([p.name for p in stored.parent.iterdir()], [stored.name])
        self.assertTrue(str(stored).startswith(str(self.shared)))

    def test_work_files_stay_local_while_downloads_go_to_the_project_folder(self):
        self.use_shared_folder()
        slug = self.new_project(chapters=2)["slug"]
        self.assertTrue(str(projects.chapter_work_dir(slug, 0)).startswith(str(self.local)))
        self.assertTrue(str(projects.stored_chapter_dir(slug, 0)).startswith(str(self.shared)))
