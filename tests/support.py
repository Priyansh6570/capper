"""Shared fixtures: every test runs against a throwaway projects folder, never the real one."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_settings
import config
import projects


def touch(path: Path, content: bytes = b"x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class IsolatedProjects(unittest.TestCase):
    """A temporary projects/ folder, work/ and output/ trees and settings file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recapper-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.local = self.tmp / "projects"
        self.local.mkdir()
        self.work, self.output, self.shared = self.tmp / "work", self.tmp / "output", self.tmp / "shared"
        for target, name, value in (
            (projects, "PROJECTS_DIR", self.local), (projects, "WORK_DIR", self.work),
            (projects, "OUTPUT_DIR", self.output), (config, "PROJECTS_DIR", self.local),
            (app_settings, "SETTINGS_FILE", self.tmp / "app_settings.json"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_shared_folder(self) -> Path:
        app_settings.set_projects_folder(str(self.shared))
        return self.shared

    def new_project(self, name: str = "Demo Series", chapters: int = 5, url: str = "https://example.test/s") -> dict:
        project = projects.create(name, url)
        projects.fill_chapters(project, [{"n": n, "title": f"Chapter {n}"} for n in range(chapters)])
        projects.save(project)
        return project

    def chapter_folders(self, slug: str) -> list[str]:
        chapters = projects.local_dir(slug) / "chapters"
        return sorted(p.name for p in chapters.iterdir()) if chapters.is_dir() else []
