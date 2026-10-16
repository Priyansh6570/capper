"""The shared `gemini` settings group became `script`; older projects migrate on first load."""
from __future__ import annotations

import json
import threading

import project_defaults
import projects
from tests.support import IsolatedProjects

LEGACY_VOICE = {"tone": "serious", "narration_type": "third_person", "style_reference": "Dry and short.",
                "model": "gemini-3.8-flash"}


class SettingsMigrationTest(IsolatedProjects):
    def legacy_project(self, schema: int = 2) -> str:
        """A project as schema 2 wrote it: the voice under settings.gemini next to script_engine."""
        project = projects.new_project("Old Voice", "https://example.test/old")
        document = {k: v for k, v in project.items() if k != "chapters"}
        document.update(schema=schema, chapter_count=0, chapter_ranges=[], chapter_titles={})
        settings = document["settings"]
        del settings["script"]
        settings["gemini"] = dict(LEGACY_VOICE)
        settings["script_engine"] = "claude"
        folder = projects.project_dir(project["slug"])
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(document), encoding="utf-8")
        return project["slug"]

    def stored(self, slug: str, name: str = "project.json") -> dict:
        return json.loads((projects.project_dir(slug) / name).read_text(encoding="utf-8"))

    def test_the_voice_moves_to_the_script_group(self):
        slug = self.legacy_project()
        script = projects.load(slug)["settings"]["script"]
        self.assertEqual(script, {"tone": "serious", "narration_type": "third_person", "style_reference": "Dry and short."})

    def test_the_model_and_engine_choice_are_dropped(self):
        settings = projects.load(self.legacy_project())["settings"]
        self.assertNotIn("gemini", settings)
        self.assertNotIn("script_engine", settings)

    def test_project_json_is_rewritten_and_the_original_kept_once(self):
        slug = self.legacy_project()
        original = (projects.project_dir(slug) / "project.json").read_text(encoding="utf-8")
        projects.load(slug)
        projects.load(slug)
        document = self.stored(slug)
        self.assertEqual(document["schema"], projects.SCHEMA_VERSION)
        self.assertNotIn("gemini", document["settings"])
        self.assertNotIn("script_engine", document["settings"])
        self.assertEqual((projects.project_dir(slug) / "project.json.v2").read_text(encoding="utf-8"), original)

    def test_migrating_twice_changes_nothing(self):
        slug = self.legacy_project()
        first = projects.load(slug)["settings"]
        self.assertEqual(projects.load(slug)["settings"], first)

    def test_an_existing_original_copy_is_never_replaced(self):
        slug = self.legacy_project()
        (projects.project_dir(slug) / "project.json.v2").write_text("the real original", encoding="utf-8")
        projects.load(slug)
        self.assertEqual((projects.project_dir(slug) / "project.json.v2").read_text(encoding="utf-8"), "the real original")

    def test_concurrent_loads_keep_the_original_and_migrate_once(self):
        slug = self.legacy_project()
        original = (projects.project_dir(slug) / "project.json").read_text(encoding="utf-8")
        results, barrier = [], threading.Barrier(8)

        def load():
            barrier.wait()
            results.append(projects.load(slug)["settings"]["script"])

        threads = [threading.Thread(target=load) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(script == results[0] for script in results))
        self.assertEqual((projects.project_dir(slug) / "project.json.v2").read_text(encoding="utf-8"), original)

    def test_a_schema_1_project_goes_through_both_migrations(self):
        project = projects.new_project("Very Old", "")
        project["schema"] = 1
        project["chapters"] = [{"n": 0, "title": "Episode 0", "status": "boxed", "pdf": None, "video": None,
                                "updated_at": None, "tone": None}]
        del project["settings"]["script"]
        project["settings"]["gemini"] = dict(LEGACY_VOICE)
        folder = projects.project_dir(project["slug"])
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project), encoding="utf-8")
        loaded = projects.load(project["slug"])
        self.assertEqual(loaded["settings"]["script"]["tone"], "serious")
        self.assertEqual(projects.get_chapter(loaded, 0)["status"], "boxed")
        self.assertIn("chapters", self.stored(project["slug"], "project.json.v1"))
        self.assertNotIn("chapters", self.stored(project["slug"], "project.json.v2"))
        self.assertIn("gemini", self.stored(project["slug"], "project.json.v2")["settings"])

    def test_a_chapter_without_its_own_tone_uses_the_script_tone(self):
        project = projects.load(self.legacy_project())
        projects.fill_chapters(project, [{"n": 0}])
        self.assertEqual(projects.chapter_tone(project, 0), "serious")


class ScriptSettingsTest(IsolatedProjects):
    def setUp(self):
        super().setUp()
        self.slug = self.new_project()["slug"]

    def update(self, **groups) -> dict:
        """Save the project's settings with `groups` merged in, the way the settings page does."""
        settings = projects.load(self.slug)["settings"]
        settings.update(groups)
        projects.update_settings(self.slug, settings)
        return projects.load(self.slug)["settings"]

    def test_the_script_group_can_be_updated(self):
        script = self.update(script={"tone": "comedic", "narration_type": "third_person", "style_reference": "Short."})["script"]
        self.assertEqual(script, {"tone": "comedic", "narration_type": "third_person", "style_reference": "Short."})

    def test_invalid_values_fall_back_to_defaults(self):
        script = self.update(script={"tone": "nonsense", "narration_type": "second_person", "style_reference": ""})["script"]
        self.assertEqual((script["tone"], script["narration_type"]), (projects.tones.DEFAULT_TONE, "first_person"))

    def test_the_retired_gemini_group_is_ignored(self):
        settings = self.update(gemini={"tone": "serious", "model": "gemini-3.8-flash"}, script_engine="gemini")
        self.assertNotIn("gemini", settings)
        self.assertNotIn("script_engine", settings)

    def test_new_projects_start_from_the_script_defaults(self):
        defaults = project_defaults.load()
        self.assertEqual(set(defaults["script"]), {"tone", "narration_type", "style_reference"})
        self.assertNotIn("gemini", defaults)
        self.assertNotIn("script_engine", defaults)
