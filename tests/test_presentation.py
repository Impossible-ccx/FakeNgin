"""Keyword highlighting never promotes input text to executable HTML."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp.presentation import highlight_keyword


class KeywordPresentationTests(unittest.TestCase):
    def test_literal_highlights_escape_text_and_query(self):
        rendered = str(highlight_keyword('<script>a.b</script> A.B & "text"', 'a.b'))
        self.assertIn('<mark>a.b</mark>', rendered)
        self.assertIn('<mark>A.B</mark>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&amp;', rendered)
        self.assertEqual(str(highlight_keyword('<img src=x>', '<img')), '<mark>&lt;img</mark> src=x&gt;')
        self.assertEqual(str(highlight_keyword('Straße', 'STRASSE')), '<mark>Straße</mark>')
        self.assertEqual(str(highlight_keyword('ß', 's')), '<mark>ß</mark>')

    def test_empty_or_nonmatching_query_preserves_escaped_original(self):
        self.assertEqual(str(highlight_keyword('正文 <b>&', '')), '正文 &lt;b&gt;&amp;')
        self.assertEqual(str(highlight_keyword('正文 <b>&', '其他')), '正文 &lt;b&gt;&amp;')


if __name__ == '__main__':
    unittest.main()
