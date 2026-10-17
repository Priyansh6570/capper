from __future__ import annotations

from pathlib import Path
from unittest import mock

import app_settings
import projects
from tests.support import IsolatedProjects, touch


class ProjectLoadingTest(IsolatedProjects):
    def test_create_then_load_has_defaults_and_chapters(self):
        slug = self.new_project("My Series", chapters=3)["slug"]
        loaded = projects.load(slug)
        self.assertEqual(loaded["name"], "My Series")
        self.assertEqual([c["n"] for c in loaded["chapters"]], [0, 1, 2])
        self.assertEqual(loaded["chapters"][0]["status"], "not_started")
        self.assertIn("music", loaded["settings"])

    def test_refilling_chapters_keeps_progress(self):
        project = self.new_project(chapters=3)
        projects.set_status(project["slug"], 1, "boxed")
        project = projects.load(project["slug"])
        projects.fill_chapters(project, [{"n": n, "title": f"Chapter {n}"} for n in range(6)])
        projects.save(project)
        loaded = projects.load(project["slug"])
        self.assertEqual(len(loaded["chapters"]), 6)
        self.assertEqual(projects.get_chapter(loaded, 1)["status"], "boxed")

    def test_list_projects_counts_statuses(self):
        slug = self.new_project(chapters=4)["slug"]
        projects.set_status(slug, 0, "downloaded")
        [listed] = projects.list_projects()
        self.assertEqual((listed["slug"], listed["chapters"]), (slug, 4))
        self.assertEqual(listed["counts"], {"downloaded": 1, "not_started": 3})

    def test_slug_is_unique_across_local_and_shared_folders(self):
        self.use_shared_folder()
        first = projects.create("Same Name", "")["slug"]
        self.assertEqual(projects.unique_slug("Same Name"), f"{first}-2")

    def test_new_projects_go_to_the_shared_folder_when_set(self):
        shared = self.use_shared_folder()
        slug = self.new_project()["slug"]
        self.assertTrue((shared / slug / "project.json").is_file())
        self.assertFalse((self.local / slug / "project.json").exists())
        self.assertEqual([p["slug"] for p in projects.list_projects()], [slug])

    def test_projects_in_both_folders_are_listed_once(self):
        local_slug = self.new_project("Local One")["slug"]
        self.use_shared_folder()
        shared_slug = self.new_project("Shared One")["slug"]
        self.assertEqual(sorted(p["slug"] for p in projects.list_projects()), sorted([local_slug, shared_slug]))

    def test_reading_chapter_paths_creates_no_folders(self):
        slug = self.new_project(chapters=3)["slug"]
        projects.derive_all(slug)
        projects.chapter_work_path(slug, 2)
        projects.chapter_video_path(slug, 2)
        self.assertEqual(self.chapter_folders(slug), [])

    def test_writing_creates_only_that_chapters_folders(self):
        slug = self.new_project(chapters=3)["slug"]
        projects.chapter_work_dir(slug, 1)
        self.assertEqual(self.chapter_folders(slug), ["1"])

    def test_delete_removes_every_location(self):
        shared = self.use_shared_folder()
        slug = self.new_project()["slug"]
        touch(projects.chapter_dir(slug, 1) / "work" / "x.txt")
        touch(self.work / f"{slug}_ch1" / "old.txt")
        touch(self.output / f"{slug}_ch1" / "old.mp4")
        projects.delete(slug)
        self.assertFalse((shared / slug).exists())
        self.assertFalse((self.local / slug).exists())
        self.assertFalse((self.work / f"{slug}_ch1").exists())
        self.assertFalse((self.output / f"{slug}_ch1").exists())

    def test_delete_works_when_the_project_exists_in_only_one_location(self):
        self.use_shared_folder()
        slug = projects.new_project("Local Only", "")["slug"]
        folder = self.local / slug
        folder.mkdir()
        (folder / "project.json").write_text("{}", encoding="utf-8")
        projects.delete(slug)
        self.assertFalse(folder.exists())

    def test_delete_of_a_project_that_is_nowhere_is_not_an_error(self):
        self.use_shared_folder()
        projects.delete("never-existed")

    def test_delete_names_what_it_could_not_remove(self):
        slug = self.new_project()["slug"]

        def stuck(path, onexc):
            onexc(OSError, Path(path) / "locked.mp4", PermissionError())

        with mock.patch.object(projects.shutil, "rmtree", side_effect=stuck):
            with self.assertRaisesRegex(OSError, "Couldn't delete: .*locked.mp4"):
                projects.delete(slug)

    def test_delete_leaves_other_projects_alone(self):
        keep = self.new_project("Keep Me")["slug"]
        drop = self.new_project("Drop Me")["slug"]
        projects.delete(drop)
        self.assertTrue(projects.exists(keep))
        self.assertFalse(projects.exists(drop))


class AppSettingsTest(IsolatedProjects):
    def test_projects_folder_is_none_until_set_and_can_be_cleared(self):
        self.assertIsNone(app_settings.projects_folder())
        self.use_shared_folder()
        self.assertEqual(app_settings.projects_folder(), self.shared)
        app_settings.set_projects_folder("")
        self.assertIsNone(app_settings.projects_folder())


class RelocateTest(IsolatedProjects):
    def test_move_to_another_folder_keeps_the_project(self):
        self.use_shared_folder()
        slug = self.new_project("Angry Nymph")["slug"]
        target = self.shared / "bebe"
        projects.relocate(slug, str(target))
        self.assertEqual(projects.project_dir(slug), target)
        self.assertTrue((target / "project.json").is_file())
        self.assertFalse((self.shared / slug).exists())
        self.assertEqual([p["slug"] for p in projects.list_projects()], [slug])
        projects.relocate(slug, "")
        self.assertEqual(projects.project_dir(slug), self.shared / slug)
        self.assertEqual(app_settings.project_folders(), {})

    def test_point_at_a_folder_without_project_is_refused(self):
        slug = self.new_project()["slug"]
        with self.assertRaises(ValueError):
            projects.relocate(slug, str(self.tmp / "nothing"), move=False)
