import unittest

from amv import music, render


def fake_analysis(bpm=120.0, dur=120.0):
    p = 60 / bpm
    beats = [round(i * p, 4) for i in range(int(dur / p))]
    return {"beats": beats, "downbeats": beats[::4], "drops": [30.0], "duration": dur, "bpm": bpm}


class AmvTimeline(unittest.TestCase):
    def test_window_puts_drop_inside_and_on_downbeat_grid(self):
        a = fake_analysis()
        start, end, drop_rel = music.pick_window(a, 30.0)
        self.assertAlmostEqual(end - start, 30.0)
        self.assertTrue(0 < drop_rel < 30)
        self.assertIn(round(start, 4), a["downbeats"])

    def test_cuts_cover_whole_clip_and_hit_the_drop(self):
        a = fake_analysis()
        start, _, drop_rel = music.pick_window(a, 30.0)
        cuts = render.build_cuts(a, start, 30.0, drop_rel)
        self.assertEqual(cuts[0].t0, 0.0)
        self.assertEqual(cuts[-1].t1, 30.0)
        for x, y in zip(cuts, cuts[1:]):
            self.assertAlmostEqual(x.t1, y.t0, places=3)
        self.assertEqual(sum(c.hero for c in cuts), 1)
        self.assertLess(cuts[-1].t1 - cuts[-1].t0, 3.0)   # no endless final hold

    def test_small_pool_merges_cuts_instead_of_repeating(self):
        a = fake_analysis()
        start, _, drop_rel = music.pick_window(a, 30.0)
        cuts = render.build_cuts(a, start, 30.0, drop_rel)
        thin = render.thin_cuts(cuts, 16)
        self.assertLess(len(thin), len(cuts))
        self.assertEqual(sum(c.hero for c in thin), 1)
        self.assertEqual(thin[-1].t1, 30.0)
        for x, y in zip(thin, thin[1:]):
            self.assertAlmostEqual(x.t1, y.t0, places=3)


class AmvPlanner(unittest.TestCase):
    def test_aura_plan_is_mostly_faces_and_ends_with_the_hero(self):
        from amv import planner
        a = fake_analysis()
        start, _, drop_rel = music.pick_window(a, 30.0)
        cuts = render.build_cuts(a, start, 30.0, drop_rel)
        items = []
        for i in range(60):
            face = i % 6 != 0
            items.append({"path": f"f{i}.png", "ch": i // 10, "id": f"P{i}",
                          "tag": {"has_face": face, "face_box": [0.2, 0.2, 0.6, 0.5] if face else [0, 0, 0, 0],
                                  "eye": [0.4, 0.3], "kind": "face" if face else "action",
                                  "expression": f"e{i % 5}", "intensity": 3 + i % 7, "text": False}})
        frames, hero = planner.plan_aura(items, cuts)
        self.assertEqual(hero, len(frames) - 1)
        faces = sum(f["tag"]["has_face"] for f in frames)
        self.assertGreaterEqual(faces / len(frames), 0.88)
        self.assertEqual(len({f["path"] for f in frames}), len(frames))   # no frame twice


if __name__ == "__main__":
    unittest.main()


def bank_items(n=40):
    out = []
    for i in range(n):
        out.append({"path": f"f{i:02d}.png", "id": f"f{i:02d}", "ch": "1", "order": i, "line": "",
                    "tag": {"has_face": i % 3 != 0, "intensity": 3 + (i * 7) % 7, "text": i == 5, "expression": "x",
                            "solo": True, "visibility": "full", "kind": "face", "face_box": [0.3, 0.2, 0.7, 0.6],
                            "eye": [0.5, 0.35]}})
    return out


class AmvStyles(unittest.TestCase):
    def setUp(self):
        from amv import styles
        self.styles = styles
        self.a = fake_analysis()
        self.start, _, self.drop = music.pick_window(self.a, 30.0)
        self.items = bank_items()

    def plan(self, style):
        return self.styles.build(style, self.items, self.a, self.start, 30.0, self.drop)

    def assert_covers(self, plan):
        self.assertEqual(plan.cuts[0].t0, 0.0)
        for x, y in zip(plan.cuts, plan.cuts[1:]):
            self.assertAlmostEqual(x.t1, y.t0, places=3)
        self.assertEqual(plan.cuts[-1].t1, 30.0)
        if plan.order is not None:
            self.assertEqual(len(plan.order), len(plan.cuts))
            self.assertTrue(all(0 <= i < len(plan.frames) for i in plan.order))

    def test_story_holds_two_seconds_in_reading_order_then_bursts_from_the_drop(self):
        plan = self.plan("story")
        self.assert_covers(plan)
        scenes = [c for c in plan.cuts if c.mode == "story"]
        self.assertTrue(scenes and all(c.t1 - c.t0 >= self.styles.STORY_HOLD - 0.01 for c in scenes))
        self.assertAlmostEqual(scenes[-1].t1, self.drop, places=1)
        self.assertEqual(sum(c.hero for c in plan.cuts), 1)
        self.assertEqual(next(c for c in plan.cuts if c.hero).mode, "burst")
        story_ids = [plan.frames[plan.order[i]]["path"] for i, c in enumerate(plan.cuts) if c.mode == "story"]
        self.assertEqual(story_ids, sorted(story_ids))                       # reading order
        self.assertNotIn("f05.png", {f["path"] for f in plan.frames})        # lettering stays out

    def test_twist_is_the_last_story_cut_and_aura_follows_the_drop(self):
        plan = self.plan("twist")
        self.assert_covers(plan)
        scenes = [i for i, c in enumerate(plan.cuts) if c.mode == "story"]
        self.assertEqual(scenes, list(range(len(scenes))))
        late = {f"f{i:02d}.png" for i in range(len(self.items) * 2 // 3, len(self.items))}
        self.assertIn(plan.frames[plan.order[scenes[-1]]]["path"], late)     # the twist comes from the story's late part
        after = [c for c in plan.cuts if c.mode != "story"]
        self.assertTrue(after[0].hero and all(c.mode == "aura" for c in after))

    def test_aura_is_the_old_montage(self):
        plan = self.plan("aura")
        self.assert_covers(plan)
        self.assertIsNone(plan.order)
        self.assertTrue(all(c.mode == "aura" for c in plan.cuts))
        self.assertTrue(any(c.trans == "whip" for c in plan.cuts))

    def test_unknown_style_and_empty_bank_are_refused(self):
        with self.assertRaises(ValueError):
            self.styles.build("nope", self.items, self.a, self.start, 30.0, self.drop)
        with self.assertRaises(ValueError):
            self.styles.build("story", [], self.a, self.start, 30.0, self.drop)


class AmvHitCuts(unittest.TestCase):
    """Cuts at the start follow the track's audible hits, not the beat grid."""

    def analysis(self):
        a = fake_analysis(bpm=96.0)
        # syncopated hits (0.31, 0.62, 0.80, 0.93 ...) that the even grid does not contain
        a["onsets"] = [[round(t, 3), 0.9] for t in (0.31, 0.62, 0.93, 1.23, 1.70, 2.17, 2.80, 3.14)]
        return a

    def test_hook_and_intro_cuts_sit_on_hits(self):
        a = self.analysis()
        cuts = render.build_cuts(a, 0.0, 30.0, 10.0)
        early = [c.t0 for c in cuts if 0 < c.t0 < 3.2 and c.kind in ("hook", "intro")]
        hits = [o for o, _ in a["onsets"]]
        self.assertTrue(early)
        for t in early:
            self.assertLess(min(abs(t - h) for h in hits), 0.04, t)

    def test_no_long_hole_after_the_hook(self):
        cuts = render.build_cuts(self.analysis(), 0.0, 30.0, 10.0)
        self.assertLessEqual(max(c.t1 - c.t0 for c in cuts if c.t0 < 6.0), 1.3)

    def test_start_moves_onto_a_nearby_strong_hit(self):
        a = self.analysis()
        self.assertEqual(music.hit_near(a, 0.28), 0.31)
        self.assertEqual(music.hit_near(a, 0.5, reach=0.05), 0.5)        # nothing near enough: unchanged

    def test_story_changes_land_on_beats_and_hold_two_seconds(self):
        from amv import styles
        beats = [i * 0.627 for i in range(40)]
        cuts = styles._story_cuts(12.0, beats)
        self.assertTrue(all(c.t1 - c.t0 >= styles.STORY_HOLD - 0.01 for c in cuts))
        on_beat = [min(abs(c.t0 - b) for b in beats) < 0.02 for c in cuts[1:]]
        self.assertGreaterEqual(sum(on_beat), len(on_beat) - 1)     # an edge with no legal beat keeps the 2s rule instead


class AmvPivot(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from amv import styles
        self.styles = styles
        self.tmp = tempfile.TemporaryDirectory()
        self.a = fake_analysis()
        self.start, _, self.drop = music.pick_window(self.a, 30.0)
        self.items = []
        for i in range(24):
            p = Path(self.tmp.name) / f"f{i:02d}.png"
            Image.new("RGB", (700, 500) if i != 7 else (300, 150)).save(p)      # f07 is far too small to zoom
            tag = {"has_face": i % 2 == 0, "face_box": [0.3, 0.2, 0.7, 0.6], "eye": [0.5, 0.35] if i % 2 == 0 else [0, 0],
                   "mouth": [0.5, 0.5] if i % 4 == 0 else [0, 0], "hand": [0.4, 0.6] if i % 3 == 0 else [0, 0],
                   "weapon": [0, 0], "intensity": 4 + i % 6, "text": False, "kind": "face", "expression": "x", "solo": True}
            self.items.append({"path": str(p), "id": f"f{i:02d}", "ch": "1", "order": i, "line": "", "tag": tag})

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, point):
        return self.styles.build("pivot", self.items, self.a, self.start, 30.0, self.drop, point)

    def test_only_frames_showing_the_point_are_used_and_no_whips(self):
        plan = self.plan("eye")
        shown = {f["path"] for f in plan.frames}
        for f in plan.frames:
            self.assertTrue(render.pivot_of(f["tag"], "eye"))
        self.assertNotIn(self.items[1]["path"], shown)                    # no face -> no eye
        self.assertNotIn(self.items[7]["path"], shown)                    # too small to pin
        self.assertFalse(any(c.trans == "whip" for c in plan.cuts))
        self.assertEqual(sum(c.hero for c in plan.cuts), 1)

    def test_each_point_selects_its_own_frames_and_too_few_is_refused(self):
        hands = {f["path"] for f in self.plan("hand").frames}
        self.assertTrue(hands <= {x["path"] for x in self.items if x["tag"]["hand"][0] > 0})
        with self.assertRaises(ValueError):
            self.plan("weapon")                                           # none of the frames shows one
        with self.assertRaises(ValueError):
            self.plan("elbow")

    def test_pivot_of_needs_a_face_for_eye_and_mouth(self):
        tag = {"has_face": False, "face_box": [0.3, 0.2, 0.7, 0.6], "eye": [0.5, 0.3], "mouth": [0.5, 0.5], "hand": [0.2, 0.2]}
        self.assertIsNone(render.pivot_of(tag, "eye"))
        self.assertIsNone(render.pivot_of(tag, "mouth"))
        self.assertEqual(render.pivot_of(tag, "hand"), (0.2, 0.2, None))
