import unittest

from scripts import trigger_signals as ts


class TriggerSignalsTest(unittest.TestCase):
    def test_scores_agent_tooling_language(self):
        score, hits = ts.score_text("Own our internal AI platform: MCP servers, CLAUDE.md standards, everyone uses Claude Code.")
        self.assertGreaterEqual(score, ts.MIN_SCORE)
        self.assertIn("claude-code", hits)
        self.assertIn("mcp", hits)
        self.assertIn("agents-md", hits)

    def test_ignores_unrelated_postings(self):
        score, hits = ts.score_text("Account Executive: manage a pipeline of enterprise customers and exceed quota.")
        self.assertEqual(score, 0)
        self.assertEqual(hits, [])

    def test_cursor_position_is_not_cursor_editor(self):
        self.assertEqual(ts.score_text("update the cursor position in the canvas")[0], 0)

    def test_detects_ats_from_links_or_html(self):
        self.assertEqual(ts.detect_ats(["https://jobs.ashbyhq.com/vendelux"], ""), ("ashby", "vendelux"))
        self.assertEqual(ts.detect_ats([], '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">'), ("greenhouse", "acme"))
        self.assertEqual(ts.detect_ats(["https://jobs.lever.co/acme/123"], ""), ("lever", "acme"))
        self.assertIsNone(ts.detect_ats(["https://acme.example/careers"], ""))

    def test_parses_rss_and_atom(self):
        rss = b'<rss><channel><item><title>How we use Claude Code</title><link>https://a.example/p</link><pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate><description>MCP everywhere</description></item></channel></rss>'
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Hello</title><link href="https://a.example/h"/><published>2026-09-01T10:00:00Z</published><summary>x</summary></entry></feed>'
        self.assertEqual(ts.parse_feed(rss)[0]["url"], "https://a.example/p")
        self.assertEqual(ts.parse_feed(atom)[0]["url"], "https://a.example/h")
        self.assertIsNotNone(ts.parse_when(ts.parse_feed(rss)[0]["publishedAt"]))


if __name__ == "__main__":
    unittest.main()
