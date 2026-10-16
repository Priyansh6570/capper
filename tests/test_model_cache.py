"""Where the Hugging Face model cache lives: a per-PC setting, applied as HF_HOME."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import app_settings
import choose_model_cache
import config
from tests.support import ApiProjects, IsolatedProjects


class ModelCacheSettingTest(IsolatedProjects):
    def test_there_is_no_model_cache_until_one_is_set(self):
        self.assertIsNone(app_settings.model_cache())

    def test_a_saved_folder_is_created_and_remembered(self):
        folder = self.tmp / "hf_cache"
        self.assertEqual(app_settings.set_model_cache(str(folder)), folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(app_settings.model_cache(), folder)

    def test_an_empty_value_goes_back_to_the_default(self):
        app_settings.set_model_cache(str(self.tmp / "hf_cache"))
        app_settings.set_model_cache("")
        self.assertIsNone(app_settings.model_cache())

    def test_it_is_stored_beside_the_projects_folder_without_touching_it(self):
        app_settings.set_projects_folder(str(self.tmp / "shared"))
        app_settings.set_model_cache(str(self.tmp / "hf_cache"))
        self.assertEqual(app_settings.projects_folder(), self.tmp / "shared")
        saved = json.loads((self.tmp / "app_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["hf_home"], str(self.tmp / "hf_cache"))


class HfHomeTest(IsolatedProjects):
    """config hands the saved folder to huggingface_hub as HF_HOME when it is imported."""

    def apply(self, saved: str | None = None, env: dict | None = None) -> str | None:
        settings_file = self.tmp / "settings.json"
        if saved is not None:
            settings_file.write_text(saved, encoding="utf-8")
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(config, "APP_SETTINGS_FILE", settings_file):
            if not env:
                os.environ.pop("HF_HOME", None)
            config.apply_model_cache_setting()
            return os.environ.get("HF_HOME")

    def test_the_saved_folder_becomes_hf_home(self):
        self.assertEqual(self.apply(json.dumps({"hf_home": "D:/hf_cache"})), "D:/hf_cache")

    def test_an_hf_home_already_in_the_environment_wins(self):
        self.assertEqual(self.apply(json.dumps({"hf_home": "D:/hf_cache"}), env={"HF_HOME": "E:/mine"}), "E:/mine")

    def test_nothing_is_set_without_a_usable_setting(self):
        for saved in (None, "{not json", json.dumps({"hf_home": None}), json.dumps({"projects_folder": "x"})):
            with self.subTest(saved=saved):
                self.assertIsNone(self.apply(saved))


class ChooseDriveTest(IsolatedProjects):
    SYSTEM = Path("C:/")

    def choose(self, free: dict[str, float], drives: list[str] | None = None):
        paths = [Path(f"{d}:/") for d in (drives or free)]
        return choose_model_cache.choose(paths, self.SYSTEM, lambda drive: free[drive.drive[0]])

    def test_the_first_other_drive_with_room_is_used(self):
        self.assertEqual(self.choose({"C": 500, "D": 80}), Path("D:/hf_cache"))

    def test_the_system_drive_is_never_chosen(self):
        self.assertIsNone(self.choose({"C": 500}))

    def test_a_drive_without_enough_room_is_skipped(self):
        self.assertEqual(self.choose({"C": 500, "D": choose_model_cache.MIN_FREE_GB - 1, "E": 200}), Path("E:/hf_cache"))
        self.assertIsNone(self.choose({"C": 500, "D": 5}))

    def test_drives_are_tried_in_letter_order(self):
        self.assertEqual(self.choose({"E": 100, "D": 100}, drives=["E", "D"]), Path("D:/hf_cache"))

    def run_setup_step(self, free: dict[str, float], env: dict | None = None) -> int:
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(choose_model_cache, "fixed_drives", return_value=[Path(f"{d}:/") for d in free]), \
                mock.patch.object(choose_model_cache.shutil, "disk_usage",
                                  side_effect=lambda drive: mock.Mock(free=free[Path(drive).drive[0]] * 1e9)):
            if not env:
                os.environ.pop("HF_HOME", None)
            os.environ["SystemDrive"] = "C:"
            return choose_model_cache.main()

    def test_setup_saves_the_chosen_folder(self):
        with mock.patch.object(app_settings.Path, "mkdir"):
            self.assertEqual(self.run_setup_step({"C": 500, "D": 80}), 0)
        self.assertEqual(app_settings.model_cache(), Path("D:/hf_cache"))

    def test_a_pc_with_only_one_drive_keeps_the_default(self):
        self.run_setup_step({"C": 500})
        self.assertIsNone(app_settings.model_cache())

    def test_an_existing_setting_is_not_replaced(self):
        app_settings.set_model_cache(str(self.tmp / "mine"))
        self.run_setup_step({"C": 500, "D": 80})
        self.assertEqual(app_settings.model_cache(), self.tmp / "mine")

    def test_an_hf_home_in_the_environment_is_not_overridden(self):
        self.run_setup_step({"C": 500, "D": 80}, env={"HF_HOME": "E:/mine"})
        self.assertIsNone(app_settings.model_cache())


class ModelCacheApiTest(ApiProjects):
    def post(self, **body) -> dict:
        response = self.client.post("/api/app_settings", json=body)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_the_settings_report_the_cache_in_use(self):
        data = self.client.get("/api/app_settings").get_json()
        self.assertEqual(data["model_cache"], "")
        self.assertTrue(data["model_cache_in_use"])

    def test_saving_a_folder_creates_it_and_returns_it(self):
        folder = self.tmp / "cache"
        self.assertEqual(self.post(model_cache=str(folder))["model_cache"], str(folder))
        self.assertTrue(folder.is_dir())

    def test_saving_an_empty_value_clears_it(self):
        self.post(model_cache=str(self.tmp / "cache"))
        self.assertEqual(self.post(model_cache="")["model_cache"], "")

    def test_saving_the_projects_folder_leaves_the_cache_alone(self):
        self.post(model_cache=str(self.tmp / "cache"))
        data = self.post(projects_folder=str(self.tmp / "shared"))
        self.assertEqual((data["projects_folder"], data["model_cache"]), (str(self.tmp / "shared"), str(self.tmp / "cache")))
