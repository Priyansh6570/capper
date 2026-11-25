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
        # one frame holds through the narration; the main edit (several cuts) starts after it
        end_of_words = 1.0 + 0.9
        self.assertGreaterEqual(plan.cuts[0].t1, end_of_words)
        self.assertEqual(plan.cuts[0].t0, 0.0)
        self.assertGreater(len(plan.cuts), 3)
        self.assertNotIn(plan.order[0], plan.order[1:])    # the hold frame is not reused in the edit
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


class AmvBackdrop(unittest.TestCase):
    def test_parse(self):
        from amv import backdrop
        ls = backdrop.parse("mono,rain:0.8@0-12,film@12-", 30)
        self.assertEqual([l.kind for l in ls], ["mono", "rain", "film"])
        self.assertEqual((ls[1].strength, ls[1].t0, ls[1].t1), (0.8, 0.0, 12.0))
        self.assertEqual((ls[2].t0, ls[2].t1), (12.0, 30.0))
        with self.assertRaises(ValueError):
            backdrop.parse("lava")

    def test_weight_fades_and_window(self):
        from amv.backdrop import Layer
        l = Layer("film", 1.0, 5, 10, fade=1.0)
        self.assertEqual(l.weight(4), 0.0)
        self.assertAlmostEqual(l.weight(5.5), 0.5)
        self.assertEqual(l.weight(7), 1.0)
        self.assertEqual(Layer("mono", 0.7).weight(3), 0.7)

    def test_emotion(self):
        from amv import backdrop
        self.assertIn("rain", [l.kind for l in backdrop.for_emotion("grief")])
        self.assertTrue(backdrop.for_emotion("unknown-mood"))

    def test_every_kind_keeps_shape_and_dtype(self):
        import numpy as np
        from amv.backdrop import KINDS, Backdrop, Layer
        img = np.random.default_rng(1).integers(0, 255, (192, 108, 3), dtype=np.uint8)
        for k in KINDS:
            bd = Backdrop([Layer(k, 1.0, fade=0)], 108, 192)
            for i in range(3):
                out = bd.apply(img, i / 30)
            self.assertEqual(out.shape, img.shape, k)
            self.assertEqual(out.dtype, np.uint8, k)


class AmvQuote(unittest.TestCase):
    def test_quote_bank_roundtrip_and_emphasis(self):
        import tempfile
        from pathlib import Path
        from amv import quotes
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "quotes.json"
            q = quotes.upsert(f, {"text": "A blade does not hesitate.  Only the man who holds it does.", "mood": "tense"})
            self.assertEqual(len(quotes.load(f)), 1)
            quotes.upsert(f, {**q, "mood": "sad"})                      # same id: replaced, not duplicated
            self.assertEqual([x["mood"] for x in quotes.load(f)], ["sad"])
            self.assertTrue(quotes.delete(f, q["id"]))
            self.assertEqual(quotes.load(f), [])
        words = quotes.emphasis_words({"text": "Hesitation is the only thing that kills a swordsman", "emphasis": []})
        self.assertTrue(words and not (words & quotes.STOP))
        self.assertEqual(quotes.emphasis_words({"text": "x y z", "emphasis": ["Blade!"]}), {"blade"})
        self.assertEqual(quotes.accent_bgr({"mood": "x", "accent": "#ff8000"}), (0, 128, 255))

    def test_window_ends_the_voice_before_the_drop(self):
        from amv import quote_short
        a = fake_analysis()
        a["drops"] = [4.0, 12.0, 70.0]
        start, end, drop, vat = quote_short.plan_window(a, 8.0)
        self.assertEqual(start, 0.0)                       # 12s drop: room for the voice, the track from its beginning
        self.assertAlmostEqual(drop, 12.0, delta=0.6)
        self.assertGreaterEqual(vat, quote_short.LEAD)
        self.assertLessEqual(vat + 8.0, drop - start - quote_short.END_GAP + 0.01)    # voice ends before the drop
        # a far drop: start on a phrase boundary so the voice is not left waiting through a long silence
        s2, _, d2, v2 = quote_short.plan_window({**a, "drops": [40.0]}, 8.0)
        self.assertGreater(s2, 0.0)
        self.assertLess(d2 - s2 - 8.0 - quote_short.END_GAP, quote_short.MAX_GAP + 4.5)
        self.assertGreaterEqual(v2, quote_short.LEAD)
        with self.assertRaises(ValueError):
            quote_short.plan_window({**a, "drops": [7.0]}, 8.0)

    def test_text_layer_shows_words_as_spoken_and_clears_before_the_drop(self):
        import numpy as np
        from amv import textlayer
        words = [("Never", 0.0, 0.4, 0), ("yield.", 0.4, 0.9, 0)]
        pages = textlayer.pages_from_words(words, {"yield"}, 1.0)
        tl = textlayer.TextLayer(pages)
        base = np.full((1920, 1080, 3), 80, np.uint8)
        before = tl.draw(base.copy(), 0.2)
        mid = tl.draw(base.copy(), 2.0)
        gone = tl.draw(base.copy(), pages[-1].t_out + 1.2)
        self.assertTrue((before == base).all())             # nothing yet
        self.assertGreater(int(np.abs(mid.astype(int) - base).sum()), 0)
        self.assertTrue((gone == base).all())               # the words have left

    def test_quote_recipe_uses_protagonist_frames_and_gives_text_and_voice(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from amv import styles, voice
        d = Path(tempfile.mkdtemp())
        items = []
        for i in range(30):
            p = d / f"f{i}.png"
            Image.new("RGB", (400, 300), (i * 5, 40, 40)).save(p)
            hero = i % 3 != 0
            items.append({"path": str(p), "ch": "1", "id": i, "order": i, "line": 0,
                          "tag": {"has_face": True, "character": "Hero" if hero else "Other", "kind": "face", "intensity": 3 + i % 6,
                                  "expression": "calm" if i % 2 else "angry", "text": False, "visibility": "full", "solo": True}})
        a = fake_analysis()
        narr = voice.Narration(d / "x.wav", 5.0, [voice.Word("Hold", 0.0, 0.4, 0), voice.Word("on.", 0.4, 0.9, 0)], "Hold on.")
        plan = styles.build("quote", items, a, 0.0, 30.0, 10.0, extra={"quote": {"text": "Hold on.", "mood": "calm"},
                            "narration": narr, "voice_at": 1.0, "labels": {"Hero"}})
        used = {plan.frames[i]["path"] for i in plan.order}
        hero_paths = {x["path"] for x in items if x["tag"]["character"] == "Hero"}
        self.assertTrue(used <= hero_paths)
        self.assertIsNotNone(plan.overlay)
        self.assertEqual(plan.voice[1], 1.0)
        self.assertTrue(plan.backdrop)
        self.assertEqual(sum(c.hero for c in plan.cuts), 1)


class AmvLyrics(unittest.TestCase):
    def test_lrc_parse_and_clip_words_stop_before_the_drop(self):
        from amv import lyrics_short
        lines = lyrics_short.parse_lrc("[ti:x]\n[00:01.00] First line here\n[00:04.50][00:20.00] Second line\n[00:09.00] After the drop\n")
        self.assertEqual([t for t, _ in lines], [1.0, 4.5, 9.0, 20.0])
        words = lyrics_short.clip_words(lines, 0.0, 9.5)
        self.assertEqual(words[0][0], "First")
        self.assertLessEqual(max(w[2] for w in words), 9.5 - lyrics_short.END_GAP + 1e-6)   # nothing past the drop's gap
        self.assertNotIn("After", [w[0] for w in words])           # starts too close to the drop
        self.assertEqual(len({w[3] for w in words}), 2)

    def test_pages_one_per_line(self):
        from amv import textlayer
        words = [("a", 0, .2, 0), ("b", .2, .4, 0), ("c", 1, 1.2, 1), ("d", 1.2, 1.4, 1)]
        self.assertEqual(len(textlayer.pages_from_words(words, set(), 0.0, by_sentence=True)), 2)

    def test_voice_mix_is_fully_dipped_before_the_first_word(self):
        from amv import render
        f = render.voice_mix_filter("anull", ("x.wav", 2.0, 8.0))
        self.assertIn("(t-1.450)", f)                  # the dip starts 0.55s before the voice


class S8AmvTests(unittest.TestCase):
    def test_story_lines_in_reading_order_and_spread(self):
        from stages import s8_amv
        items = [{"ch": "1", "order": i, "line": f"Line {i}."} for i in range(20)] + [{"ch": "1", "order": 99, "line": ""}]
        got = s8_amv.story_lines(items, 4)
        self.assertEqual(len(got), 4)
        self.assertEqual(got, sorted(got, key=lambda t: int(t.split()[1].rstrip("."))))

    def test_fallback_title_is_short_and_has_the_series(self):
        from stages import s8_amv
        t = s8_amv.fallback_title("Magic Emperor", "aura", ["He stands. " + "x" * 200])
        self.assertTrue(t.startswith("Magic Emperor: He stands"))
        self.assertLessEqual(len(t), 70)

    def test_index_roundtrip_missing_file(self):
        from pathlib import Path
        from stages import s8_amv
        self.assertEqual(s8_amv.read_index(Path("nope/shorts.json")), [])


class CinemaQuote(unittest.TestCase):
    """The quote short's cinematic words, animated overlay layers and full-screen frames."""

    def test_new_backdrop_layers_draw_something_and_follow_the_voice(self):
        import numpy as np
        from amv import backdrop
        base = np.full((1920, 1080, 3), 60, np.uint8)
        for kind in ("bokeh", "rays", "frame", "grain"):
            bd = backdrop.Backdrop([backdrop.Layer(kind, 1.0, 0.0, 99.0, 0.0, (80, 120, 255))])
            bd.env = lambda t: 1.0
            out = bd.apply(base.copy(), 3.0)
            self.assertEqual(out.shape, base.shape)
            self.assertGreater(int(np.abs(out.astype(int) - base).sum()), 0, kind)

    def test_cinema_text_draws_words_in_the_lower_third(self):
        import numpy as np
        from amv import cinema, textlayer
        words = [textlayer.TWord("Heaven", 1.0, 1.4, True), textlayer.TWord("will", 1.4, 1.7), textlayer.TWord("bow.", 1.7, 2.1)]
        layer = cinema.CinemaText([textlayer.Page(words, 1.0, 2.5)], accent=(60, 150, 255), hold_end=6.0, env=lambda t: 0.5)
        base = np.full((1920, 1080, 3), 40, np.uint8)
        out = layer.draw(base.copy(), 2.2)
        changed = np.where((out > 150).any(axis=(1, 2)))[0]                 # bright letters; the shade only darkens
        self.assertGreater(len(changed), 0)
        self.assertGreater(changed.min(), 1920 * 0.5)            # nothing over the speaker's face (upper half)
        self.assertTrue((layer.draw(base.copy(), 0.2) == base).all())   # nothing before the first word

    def test_voice_envelope_is_flat_without_a_file(self):
        from amv import cinema
        self.assertEqual(cinema.voice_envelope("no-such.wav", 1.0)(2.0), 0.0)

    def test_cover_only_never_letterboxes_a_small_frame(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from amv import render
        p = Path(tempfile.mkdtemp()) / "wide.png"
        Image.new("RGB", (690, 250), (90, 40, 40)).save(p)          # a wide panel: cover = 7.7
        self.assertTrue(render.load_shot(p).fit_mode)
        self.assertFalse(render.load_shot(p, cover_only=True).fit_mode)


class QuoteLooks(unittest.TestCase):
    def test_every_look_is_complete_and_draws(self):
        import numpy as np
        from amv import backdrop, cinema, looks, textlayer
        self.assertEqual(looks.resolve("royal", "x"), "royal")
        self.assertEqual(looks.resolve("auto", "return-of-the-mount-hua-sect"), "scroll")
        self.assertEqual(looks.resolve(None, "some-new-series"), looks.resolve("auto", "some-new-series"))   # stable
        self.assertIn(looks.resolve("auto", "some-new-series"), looks.LOOKS)
        for name, lk in looks.LOOKS.items():
            self.assertIn(lk.font, textlayer.FONTS, name)
            self.assertIn(lk.entrance, cinema.ENTRANCE_S, name)
            self.assertIn(lk.border, ("hairline", "brackets", "bars", "none"), name)
            for kind, strength, scope in lk.layers:
                self.assertIn(kind, backdrop.KINDS, name)
                self.assertIn(scope, ("hold", "all"), name)
            words = [textlayer.TWord("Heaven", 1.0, 1.4, True), textlayer.TWord("will", 1.4, 1.7), textlayer.TWord("bow.", 1.7, 2.1)]
            layer = cinema.CinemaText([textlayer.Page(words, 1.0, 2.5)], accent=(60, 150, 255), hold_end=6.0, env=lambda t: 0.5, look=lk)
            base = np.full((1920, 1080, 3), 40, np.uint8)
            for t in (1.05, 1.5, 2.2, 2.7):                       # arriving, speaking, leaving: none may fail
                self.assertEqual(layer.draw(base.copy(), t).shape, base.shape)

    def test_new_layers_run(self):
        import numpy as np
        from amv import backdrop
        base = np.full((1920, 1080, 3), 60, np.uint8)
        for kind in ("snow", "petals", "scan", "brackets", "bars"):
            bd = backdrop.Backdrop([backdrop.Layer(kind, 1.0, 0.0, 99.0, 0.0, (200, 170, 255))])
            for t in (0.5, 0.6, 2.0):
                out = bd.apply(base.copy(), t)
            self.assertGreater(int(np.abs(out.astype(int) - base).sum()), 0, kind)

    def test_the_edit_after_the_quote_does_not_freeze_on_one_frame(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from amv import styles, voice
        d = Path(tempfile.mkdtemp())
        items = []
        for i in range(30):
            p = d / f"f{i}.png"
            Image.new("RGB", (1080, 1920), (i * 7, 40, 40)).save(p)
            hero = i < 6                                          # only six frames of the speaker
            items.append({"path": str(p), "ch": "1", "id": i, "order": i, "line": 0,
                          "tag": {"has_face": True, "character": "Hero" if hero else "Other", "kind": "face", "intensity": 3 + i % 6,
                                  "expression": "calm" if i % 2 else "angry", "text": False, "visibility": "full", "solo": True}})
        narr = voice.Narration(d / "x.wav", 5.0, [voice.Word("Hold", 0.0, 0.4, 0), voice.Word("on.", 0.4, 0.9, 0)], "Hold on.")
        plan = styles.build("quote", items, fake_analysis(), 0.0, 24.0, 10.0, extra={
            "quote": {"text": "Hold on.", "mood": "calm"}, "narration": narr, "voice_at": 1.0, "labels": {"Hero"}, "look": "neon"})
        tail = plan.order[1:]
        self.assertGreaterEqual(len(set(tail)), 8)
        self.assertTrue(all(a != b for a, b in zip(tail, tail[1:])))      # never the same picture twice in a row


class QuoteOutro(unittest.TestCase):
    def test_closing_message_text_names_the_series(self):
        from amv import quote_short
        text = quote_short.outro_text("Return of the Mount Hua Sect")
        self.assertIn("playlist link for Return of the Mount Hua Sect", text)
        for part in ("description", "pinned comment", "upload daily"):
            self.assertIn(part, text)
        self.assertIn("The playlist link is", quote_short.outro_text(""))

    def test_two_voices_mix_and_both_dip_the_music(self):
        from amv import render
        f = render.voice_mix_filter("anull", ("a.wav", 1.5, 6.0), (("b.wav", 20.0, 6.0),))
        self.assertIn("[2:a]adelay=1500", f)
        self.assertIn("[3:a]adelay=20000", f)
        self.assertIn("amix=inputs=3", f)
        self.assertIn("max(", f)

    def test_the_plan_ends_with_a_scene_for_the_message(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from amv import styles, voice
        d = Path(tempfile.mkdtemp())
        items = []
        for i in range(20):
            p = d / f"f{i}.png"
            Image.new("RGB", (1080, 1920), (i * 9, 40, 40)).save(p)
            items.append({"path": str(p), "ch": "1", "id": i, "order": i, "line": 0,
                          "tag": {"has_face": True, "character": "Hero", "kind": "face", "intensity": 3 + i % 6,
                                  "expression": "calm", "text": False, "visibility": "full", "solo": True}})
        narr = voice.Narration(d / "x.wav", 5.0, [voice.Word("Hold", 0.0, 0.4, 0), voice.Word("on.", 0.4, 0.9, 0)], "Hold on.")
        n2 = voice.Narration(d / "y.wav", 6.0, [voice.Word("Thank", 0.0, 0.4, 0), voice.Word("you.", 0.4, 0.8, 0),
                                                voice.Word("Daily.", 1.4, 1.9, 1)], "Thank you. Daily.")
        plan = styles.build("quote", items, fake_analysis(), 0.0, 27.0, 10.0, extra={
            "quote": {"text": "Hold on.", "mood": "calm"}, "narration": narr, "voice_at": 1.0, "labels": {"Hero"},
            "core_len": 20.0, "outro": {"narration": n2, "at": 20.5, "text": "x"}})
        last = plan.cuts[-1]
        self.assertEqual((last.t0, last.t1, last.mode), (20.0, 27.0, "hold"))
        self.assertNotEqual(plan.frames[plan.order[0]]["path"], plan.frames[plan.order[-1]]["path"])   # a frame of its own
        self.assertEqual(plan.voice_more[0][1], 20.5)
        self.assertEqual(len(plan.overlay.layers), 2)
