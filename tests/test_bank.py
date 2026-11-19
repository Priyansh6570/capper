"""The shorts frame bank: choosing, copying, loading and the backup kinds."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from amv import bank


def tag(intensity=6, face=True, text=False, solo=True):
    return {"has_face": face, "face_box": [0.3, 0.2, 0.7, 0.6], "eye": [0.5, 0.35], "character": "x", "kind": "face",
            "expression": "calm", "intensity": intensity, "text": text, "visibility": "full", "solo": solo,
            "mouth": [0.5, 0.5], "hand": [0, 0], "weapon": [0, 0]}


class BankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "chapters" / "7" / "work"
        (self.work / "frames").mkdir(parents=True)
        (self.work / "framer").mkdir()
        lines = []
        for i in range(1, 6):
            Image.new("RGB", (40, 60), (i * 40, 0, 0)).save(self.work / "frames" / f"frame_{i:03d}a.png")
            lines.append({"order": i, "text": f"line {i}", "frames": [{"file": f"frame_{i:03d}a.png"}]})
        (self.work / "framer" / "mapping.json").write_text(json.dumps({"lines": lines}), encoding="utf-8")
        self.tags = {1: tag(9), 2: tag(2, face=False), 3: tag(7, text=True), 4: tag(2, face=True), 5: tag(5, face=False)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_tags(self, paths, **kw):
        return {str(p): self.tags[int(Path(p).stem.split("_")[1][:3])] for p in paths}

    def test_build_keeps_the_strong_and_faces_and_drops_text_and_weak(self):
        with mock.patch.object(bank.tagger, "tag_frames", self.fake_tags):
            data = bank.build(self.work, log=lambda m: None)
        kept = {f["id"] for f in data["frames"] if f["keep"]}
        self.assertEqual(kept, {"frame_001a", "frame_004a", "frame_005a"})
        self.assertEqual(len(data["frames"]), 5)                      # tags of every frame are stored
        self.assertEqual(sorted(p.name for p in bank.frames_dir(self.work).iterdir()),
                         ["frame_001a.png", "frame_004a.png", "frame_005a.png"])

    def test_bank_survives_the_frames_folder_being_deleted(self):
        with mock.patch.object(bank.tagger, "tag_frames", self.fake_tags):
            bank.build(self.work, log=lambda m: None)
        shutil.rmtree(self.work / "frames")
        items = bank.load(self.work)
        self.assertEqual([i["id"] for i in items], ["frame_001a", "frame_004a", "frame_005a"])
        self.assertTrue(all(Path(i["path"]).is_file() for i in items))

    def test_second_build_does_not_tag_again(self):
        with mock.patch.object(bank.tagger, "tag_frames", self.fake_tags) as m:
            bank.build(self.work, log=lambda m_: None)
        with mock.patch.object(bank.tagger, "tag_frames", side_effect=AssertionError("tagged twice")):
            self.assertEqual(bank.build(self.work, log=lambda m_: None)["version"], bank.VERSION)

    def test_no_frames_is_an_error(self):
        shutil.rmtree(self.work / "frames")
        with self.assertRaises(FileNotFoundError):
            bank.build(self.work, log=lambda m: None)


class BackupKindTest(unittest.TestCase):
    def test_shorts_frames_is_ticked_and_clearable_and_the_bank_file_rides_with_the_script(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "framer"))
        import chapter_backup
        k = chapter_backup.KINDS["shorts_frames"]
        self.assertTrue(k.default and k.clearable)
        self.assertIn("work/shorts_bank/bank.json", chapter_backup.KINDS["script"].paths)
        self.assertFalse(chapter_backup.KINDS["script"].clearable)


if __name__ == "__main__":
    unittest.main()


class BankServiceTest(unittest.TestCase):
    """The service, the job and the routes around the bank, with the tagging itself faked."""

    def setUp(self):
        from tests.support import ApiProjects
        self.base = ApiProjects
        self.case = type("C", (ApiProjects,), {"runTest": lambda s: None})()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.slug = self.case.new_project("Bank Series", chapters=2)["slug"]
        import projects
        self.projects = projects

    def _frames(self, n):
        work = self.projects.chapter_work_path(self.slug, n)
        (work / "frames").mkdir(parents=True)
        (work / "framer").mkdir(parents=True)
        Image.new("RGB", (40, 60)).save(work / "frames" / "frame_001a.png")
        (work / "framer" / "mapping.json").write_text(json.dumps(
            {"lines": [{"order": 1, "text": "a", "frames": [{"file": "frame_001a.png"}]}]}), encoding="utf-8")
        return work

    def test_build_chapter_then_skip(self):
        from services import shorts_bank
        work = self._frames(1)
        with mock.patch.object(bank.tagger, "tag_frames", lambda paths, **k: {str(p): tag(8) for p in paths}):
            first = shorts_bank.build_chapter(self.slug, 1, log=lambda m: None)
        self.assertEqual((first["frames"], first["kept"]), (1, 1))
        with mock.patch.object(bank.tagger, "tag_frames", side_effect=AssertionError("again")):
            self.assertEqual(shorts_bank.build_chapter(self.slug, 1, log=lambda m: None)["kept"], 1)
        shorts_bank.invalidate(work)
        self.assertFalse(bank.exists(work))

    def test_project_run_reports_a_chapter_without_frames(self):
        from services import shorts_bank
        self._frames(1)
        with mock.patch.object(bank.tagger, "tag_frames", lambda paths, **k: {str(p): tag(8) for p in paths}):
            out = shorts_bank.build_project(self.slug, log=lambda m: None)
        self.assertEqual(out[0], "no exported frames")
        self.assertEqual(out[1]["kept"], 1)

    def test_tag_job_marks_itself_done_or_failed(self):
        from services import shorts_jobs
        from services.jobs import Job
        self._frames(1)
        job = Job("x~bank", "shorts_tag", self.slug, "1", 0)
        with mock.patch.object(bank.tagger, "tag_frames", lambda paths, **k: {str(p): tag(8) for p in paths}):
            shorts_jobs.run_shorts_tag_job(job)
        self.assertEqual((job.status, job.ok), ("done", True))
        bad = Job("y~bank", "shorts_tag", self.slug, "2", 0)
        shorts_jobs.run_shorts_tag_job(bad)
        self.assertEqual((bad.status, bad.ok), ("failed", False))


class BankRouteTest(unittest.TestCase):
    def setUp(self):
        from tests.support import ApiProjects
        self.case = type("C", (ApiProjects,), {"runTest": lambda s: None})()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.slug = self.case.new_project("Route Series", chapters=2)["slug"]

    def test_status_then_queue_skips_chapters_that_have_a_bank(self):
        import projects
        from services import jobs
        c = self.case.client
        got = c.get(f"/api/projects/{self.slug}/shorts_bank").get_json()
        self.assertEqual([x["built"] for x in got["chapters"]], [False, False])
        work = projects.chapter_work_path(self.slug, 1)
        bank.bank_dir(work).mkdir(parents=True)
        bank.bank_path(work).write_text(json.dumps({"version": 1, "frames": []}), encoding="utf-8")
        with mock.patch.object(jobs.claude_cli, "cli_status", return_value={"ready": True, "message": ""}), \
             mock.patch("routes.shorts._enqueue") as enq:
            res = c.post(f"/api/projects/{self.slug}/shorts_bank", json={}).get_json()
        self.assertEqual((res["queued"], res["skipped"]), ([0], [1]))
        self.assertEqual(enq.call_count, 1)
        self.assertEqual(enq.call_args[0][0].what, "shorts_tag")
