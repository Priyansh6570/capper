"""The app on waitress: event streams must arrive as they are produced, and many can be open at once."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from unittest import mock

import app
import bulk_download
import projects
from tests.support import ApiProjects

FRAME_GAP_S = 0.5


def slow_run(slug, count, download_one, progress, should_stop):
    progress(0, 1, 2)
    time.sleep(FRAME_GAP_S)
    progress(1, 2, 2)
    return {"requested": 2, "failed": []}


class WaitressServerTest(ApiProjects):
    def setUp(self):
        super().setUp()
        project = projects.load(self.slug)
        project["site"] = "example.test"
        projects.save(project)
        patcher = mock.patch.object(bulk_download, "run", slow_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = app.make_server("127.0.0.1", 0)
        self.addCleanup(self.server.close)
        threading.Thread(target=self.server.run, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.effective_port}"

    def read_stream(self) -> list[tuple[float, dict]]:
        """Every event of one download stream with the time it arrived."""
        started = time.monotonic()
        events = []
        with urllib.request.urlopen(f"{self.base}/api/projects/{self.slug}/download_stream?count=2", timeout=20) as response:
            for line in response:
                if line.startswith(b"data: "):
                    events.append((time.monotonic() - started, json.loads(line[6:])))
        return events

    def test_serves_normal_requests(self):
        with urllib.request.urlopen(f"{self.base}/api/projects/{self.slug}?limit=3", timeout=10) as response:
            self.assertEqual(json.load(response)["project"]["name"], "Api Series")

    def test_events_arrive_as_they_are_produced(self):
        events = self.read_stream()
        self.assertEqual([e["type"] for _, e in events], ["progress", "progress", "done"])
        first, second = events[0][0], events[1][0]
        self.assertLess(first, FRAME_GAP_S / 2)
        self.assertGreaterEqual(second - first, FRAME_GAP_S * 0.8)

    def test_more_streams_than_a_default_thread_pool_run_at_once(self):
        results, started = [], time.monotonic()
        threads = [threading.Thread(target=lambda: results.append(self.read_stream())) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 12)
        self.assertLess(time.monotonic() - started, FRAME_GAP_S * 4)
