import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.website_change_events import (
    StateStore,
    Target,
    acknowledge,
    build_events,
    canonicalize_url,
    main,
    read_targets,
)


class WebsiteChangeEventsTest(unittest.TestCase):
    def test_canonicalizes_urls_without_fragments_or_default_ports(self):
        self.assertEqual(canonicalize_url("Example.COM:443/jobs/#open"), "https://example.com/jobs")
        self.assertEqual(canonicalize_url("http://Example.COM:80/"), "http://example.com/")
        self.assertIsNone(canonicalize_url("ftp://example.com/file"))

    def test_reads_targets_and_uses_stable_derived_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.csv"
            path.write_text(
                "Company,Website,Stage\nAcme,https://acme.example,Series A\nNo URL,,Seed\n",
                encoding="utf-8",
            )
            first, missing = read_targets(
                path,
                url_column="Website",
                name_column="Company",
                id_column=None,
                metadata_columns=["Stage"],
            )
            second, _ = read_targets(
                path,
                url_column="Website",
                name_column="Company",
                id_column=None,
                metadata_columns=["Stage"],
            )

        self.assertEqual(first, second)
        self.assertEqual(missing, 1)
        self.assertEqual(first[0].metadata, {"Stage": "Series A"})

    def test_silences_new_and_same_pages(self):
        target = Target("acme", "Acme", "https://acme.example/", {})
        pages = [
            {
                "metadata": {"sourceURL": "https://acme.example"},
                "changeTracking": {"changeStatus": "new"},
            },
            {
                "metadata": {"sourceURL": "https://acme.example"},
                "changeTracking": {"changeStatus": "same"},
            },
        ]

        events, unmapped = build_events(
            [target], pages, batch_id="batch", observed_at="now", max_diff_chars=100
        )

        self.assertEqual(events, [])
        self.assertEqual(unmapped, 0)

    def test_changed_page_has_stable_event_id_and_bounded_diff(self):
        target = Target("acme", "Acme", "https://acme.example/jobs", {"Stage": "A"})
        page = {
            "markdown": "new content",
            "metadata": {
                "sourceURL": "https://acme.example/jobs/",
                "title": "Jobs",
                "statusCode": 200,
            },
            "changeTracking": {
                "changeStatus": "changed",
                "previousScrapeAt": "2026-09-01T00:00:00Z",
                "visibility": "visible",
                "diff": {"text": "+" + "x" * 20},
            },
        }

        first, _ = build_events(
            [target], [page], batch_id="one", observed_at="first", max_diff_chars=10
        )
        second, _ = build_events(
            [target], [page], batch_id="two", observed_at="second", max_diff_chars=10
        )

        self.assertEqual(first[0]["eventId"], second[0]["eventId"])
        self.assertEqual(first[0]["company"]["metadata"], {"Stage": "A"})
        self.assertTrue(first[0]["change"]["diffTruncated"])
        self.assertEqual(len(first[0]["change"]["diff"]), 10)

    def test_removed_page_with_string_diff_is_emitted(self):
        target = Target("acme", "Acme", "https://acme.example/jobs", {})
        page = {
            "metadata": {"sourceURL": "https://acme.example/jobs"},
            "changeTracking": {
                "changeStatus": "removed",
                "previousScrapeAt": "2026-09-01T00:00:00Z",
                "diff": "-job listing",
            },
        }

        events, _ = build_events(
            [target], [page], batch_id="batch", observed_at="now", max_diff_chars=100
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"]["changeStatus"], "removed")
        self.assertEqual(events[0]["change"]["diff"], "-job listing")

    def test_prepare_replays_pending_until_exact_batch_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "targets.csv"
            fixture_path = root / "fixture.json"
            state_dir = root / "state"
            csv_path.write_text("name,url\nAcme,https://acme.example\n", encoding="utf-8")
            fixture_path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "markdown": "new",
                                "metadata": {"sourceURL": "https://acme.example"},
                                "changeTracking": {
                                    "changeStatus": "changed",
                                    "previousScrapeAt": "2026-09-01T00:00:00Z",
                                    "diff": {"text": "+new"},
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            command = [
                "prepare",
                "--input",
                str(csv_path),
                "--state-dir",
                str(state_dir),
                "--fixture",
                str(fixture_path),
            ]

            first_stdout = io.StringIO()
            with contextlib.redirect_stdout(first_stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 0)
            event = json.loads(first_stdout.getvalue())

            second_stdout = io.StringIO()
            with contextlib.redirect_stdout(second_stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 0)
            self.assertEqual(first_stdout.getvalue(), second_stdout.getvalue())

            wrong_ack = io.StringIO()
            with contextlib.redirect_stderr(wrong_ack):
                self.assertEqual(
                    main(["ack", "--state-dir", str(state_dir), "--batch-id", "wrong"]), 1
                )
            self.assertIn("does not match", wrong_ack.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "ack",
                            "--state-dir",
                            str(state_dir),
                            "--batch-id",
                            event["batchId"],
                        ]
                    ),
                    0,
                )
            state = StateStore(state_dir).load()
            self.assertIsNone(state["pending"])
            self.assertEqual(state["lastAcknowledgedBatchId"], event["batchId"])


if __name__ == "__main__":
    unittest.main()
