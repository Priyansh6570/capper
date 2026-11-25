import unittest

from stages import tts_kokoro


class ReadPronunciationTests(unittest.TestCase):
    def test_present_read_is_forced_to_reed(self):
        self.assertEqual(tts_kokoro.fix_pronunciations("fans who read, script"), "fans who [read](/ɹˈid/), script")
        self.assertEqual(tts_kokoro.fix_pronunciations("Read the chapter."), "[Read](/ɹˈid/) the chapter.")

    def test_past_participle_cues_keep_red(self):
        for text in ("He had read it.", "I have already read it.", "It was read aloud."):
            self.assertEqual(tts_kokoro.fix_pronunciations(text), text)

    def test_existing_markup_and_other_words_untouched(self):
        self.assertEqual(tts_kokoro.fix_pronunciations("[read](/ɹˈɛd/) reading reads"), "[read](/ɹˈɛd/) reading reads")


if __name__ == "__main__":
    unittest.main()
