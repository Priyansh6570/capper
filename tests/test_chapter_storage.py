"""Each chapter's state lives in its own file; project.json holds project-level data and the chapter index."""
from __future__ import annotations

import json
import threading

import projects
from tests.support import IsolatedProjects


class ChapterFilesTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.use_shared_folder()
        self.slug = self.new_project(chapters=6)["slug"]
        self.root = self.shared / self.slug

    def state_files(self) -> list[str]:
        return sorted(p.parent.name for p in (self.root / "chapters").glob("*/chapter.json"))

    def project_document(self) -> dict:
        return json.loads((self.root / "project.json").read_text(encoding="utf-8"))

    def test_untouched_chapters_have_no_file_or_folder(self):
        self.assertEqual(self.state_files(), [])
        self.assertFalse((self.root / "chapters").exists())

    def test_project_json_holds_the_index_not_the_chapters(self):
        document = self.project_document()
        self.assertNotIn("chapters", document)
        self.assertEqual(document["chapter_count"], 6)
        self.assertEqual(document["chapter_ranges"], [[0, 5]])

    def test_a_chapter_gets_its_file_when_it_has_progress(self):
        projects.set_status(self.slug, 2, "downloaded")
        self.assertEqual(self.state_files(), ["2"])
        self.assertEqual(json.loads((self.root / "chapters" / "2" / "chapter.json").read_text())["status"], "downloaded")

    def test_saving_different_chapters_writes_different_files(self):
        projects.set_status(self.slug, 1, "downloaded")
        before = (self.root / "chapters" / "1" / "chapter.json").stat().st_mtime_ns
        projects.set_status(self.slug, 4, "boxed")
        self.assertEqual(self.state_files(), ["1", "4"])
        self.assertEqual((self.root / "chapters" / "1" / "chapter.json").stat().st_mtime_ns, before)

    def test_a_chapter_status_change_does_not_rewrite_project_json(self):
        before = (self.root / "project.json").stat().st_mtime_ns
        projects.set_status(self.slug, 1, "boxed")
        projects.set_chapter_tone(self.slug, 1, None)
        self.assertEqual((self.root / "project.json").stat().st_mtime_ns, before)

    def test_state_survives_a_reload(self):
        projects.set_status(self.slug, 3, "boxed")
        chapter = projects.get_chapter(projects.load(self.slug), 3)
        self.assertEqual((chapter["status"], chapter["title"]), ("boxed", "Chapter 3"))

    def test_derive_all_writes_only_the_chapters_that_changed(self):
        projects.chapter_dir(self.slug, 2).joinpath("work", "download").mkdir(parents=True)
        projects.chapter_dir(self.slug, 2).joinpath("work", "download", "c.pdf").write_bytes(b"x")
        projects.derive_all(self.slug)
        self.assertEqual(self.state_files(), ["2"])

    def test_chapter_numbers_with_gaps_and_custom_titles_round_trip(self):
        project = projects.load(self.slug)
        projects.fill_chapters(project, [{"n": 0, "title": "Prologue"}, {"n": 1}, {"n": 2}, {"n": 4}, {"n": 6}])
        projects.save(project)
        self.assertEqual(self.project_document()["chapter_ranges"], [[0, 2], [4, 4], [6, 6]])
        loaded = projects.load(self.slug)
        self.assertEqual([c["n"] for c in loaded["chapters"]], [0, 1, 2, 4, 6])
        self.assertEqual(projects.get_chapter(loaded, 0)["title"], "Prologue")
        self.assertNotIn("6", self.project_document()["chapter_titles"])

    def test_tone_is_stored_with_the_chapter(self):
        projects.set_chapter_tone(self.slug, 2, "serious")
        self.assertEqual(self.state_files(), ["2"])
        self.assertEqual(projects.get_chapter(projects.load(self.slug), 2)["tone"], "serious")


LEGACY_CHAPTERS = [
    {"n": 0, "title": "Prologue", "status": "complete", "pdf": None, "video": "v.mp4", "updated_at": "2026-01-01T00:00:00+00:00", "tone": "serious"},
    {"n": 1, "title": "Episode 1", "status": "boxed", "pdf": "a.pdf", "video": None, "updated_at": "2026-01-02T00:00:00+00:00", "tone": None},
    {"n": 2, "title": "Episode 2", "status": "not_started", "pdf": None, "video": None, "updated_at": None, "tone": None},
    {"n": 4, "title": "Fourth", "status": "not_started", "pdf": None, "video": None, "updated_at": None, "tone": None},
]


class MigrationTest(IsolatedProjects):
    def legacy_project(self) -> str:
        project = projects.new_project("Old Series", "https://example.test/old")
        project["schema"] = 1
        project["chapters"] = [dict(c) for c in LEGACY_CHAPTERS]
        folder = projects.project_dir(project["slug"])
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project), encoding="utf-8")
        return project["slug"]

    def test_loading_a_legacy_project_loses_no_chapter_state(self):
        slug = self.legacy_project()
        loaded = projects.load(slug)
        self.assertEqual([{k: c[k] for k in LEGACY_CHAPTERS[0]} for c in loaded["chapters"]], LEGACY_CHAPTERS)

    def test_migration_moves_state_into_chapter_files_and_rewrites_project_json(self):
        slug = self.legacy_project()
        projects.load(slug)
        root = projects.project_dir(slug)
        self.assertEqual(sorted(p.parent.name for p in root.glob("chapters/*/chapter.json")), ["0", "1"])
        document = json.loads((root / "project.json").read_text(encoding="utf-8"))
        self.assertNotIn("chapters", document)
        self.assertEqual((document["schema"], document["chapter_count"], document["chapter_ranges"]),
                         (projects.SCHEMA_VERSION, 4, [[0, 2], [4, 4]]))

    def test_the_original_project_json_is_kept_once(self):
        slug = self.legacy_project()
        original = (projects.project_dir(slug) / "project.json").read_text(encoding="utf-8")
        projects.load(slug)
        projects.load(slug)
        self.assertEqual((projects.project_dir(slug) / "project.json.v1").read_text(encoding="utf-8"), original)

    def test_migrating_twice_changes_nothing(self):
        slug = self.legacy_project()
        first = projects.load(slug)
        second = projects.load(slug)
        self.assertEqual(first["chapters"], second["chapters"])

    def test_other_project_data_is_untouched(self):
        slug = self.legacy_project()
        loaded = projects.load(slug)
        self.assertEqual((loaded["name"], loaded["url"], loaded["site"]), ("Old Series", "https://example.test/old", None))
        self.assertIn("music", loaded["settings"])


class MigrationRaceTest(MigrationTest):
    """Several requests can load a schema 1 project at the same moment."""

    def test_concurrent_loads_keep_the_original_and_migrate_once(self):
        slug = self.legacy_project()
        folder = projects.project_dir(slug)
        original = (folder / "project.json").read_text(encoding="utf-8")
        results, barrier = [], threading.Barrier(8)

        def load():
            barrier.wait()
            results.append(projects.load(slug)["chapters"])

        threads = [threading.Thread(target=load) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(chapters == results[0] for chapters in results))
        self.assertEqual((folder / "project.json.v1").read_text(encoding="utf-8"), original)

    def test_migration_never_overwrites_a_chapter_file_that_already_exists(self):
        slug = self.legacy_project()
        newer = {"n": 1, "status": "complete", "pdf": "a.pdf", "video": "v.mp4", "updated_at": "2026-02-02T00:00:00+00:00", "tone": None}
        path = projects.stored_chapter_dir(slug, 1) / "chapter.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(newer), encoding="utf-8")
        projects.load(slug)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "complete")
        self.assertEqual(projects.get_chapter(projects.load(slug), 1)["status"], "complete")

    def test_an_existing_original_copy_is_never_replaced(self):
        slug = self.legacy_project()
        folder = projects.project_dir(slug)
        (folder / "project.json.v1").write_text("the real original", encoding="utf-8")
        projects.load(slug)
        self.assertEqual((folder / "project.json.v1").read_text(encoding="utf-8"), "the real original")
