import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import discover_company_sources as dcs


class ClassifyLinksTest(unittest.TestCase):
    def test_picks_index_pages_not_articles(self):
        links = [
            "https://acme.example/",
            "https://acme.example/blog/why-we-raised",
            "https://acme.example/blog",
            "https://www.acme.example/careers/",
            "https://acme.example/careers/senior-engineer",
            "https://acme.example/news/2026-launch",
            "https://acme.example/newsroom",
            "https://docs.acme.example/changelog",
            "https://acme.example/changelog?page=2",
            "https://other.example/blog",
        ]
        chosen = dcs.classify_links("https://acme.example/", links, dcs.DEFAULT_SOURCE_TYPES)
        self.assertEqual(
            [(s.source_type, s.url) for s in chosen],
            [
                ("careers", "https://www.acme.example/careers"),
                ("news", "https://acme.example/newsroom"),
                ("blog", "https://acme.example/blog"),
                ("changelog", "https://docs.acme.example/changelog"),
            ],
        )

    def test_prefers_external_ats_board_for_careers(self):
        links = ["https://acme.example/careers", "https://jobs.ashbyhq.com/acme"]
        chosen = dcs.classify_links("https://acme.example/", links, ("careers",))
        self.assertEqual(chosen[0].url, "https://jobs.ashbyhq.com/acme")

    def test_ignores_homepage_and_unrelated_types(self):
        links = ["https://acme.example/", "https://acme.example/pricing"]
        self.assertEqual(dcs.classify_links("https://acme.example/", links, ("careers", "news")), [])
        self.assertEqual(
            dcs.classify_links("https://acme.example/", links, ("pricing",))[0].url,
            "https://acme.example/pricing",
        )

    def test_localized_prefix_matches(self):
        links = ["https://acme.example/en/careers"]
        self.assertEqual(dcs.classify_links("https://acme.example/", links, ("careers",))[0].url, "https://acme.example/en/careers")


class DiscoverTest(unittest.TestCase):
    def test_writes_registry_from_cached_maps_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roster = root / "roster.csv"
            roster.write_text(
                "Company,Website,Stage\nAcme,https://acme.example,Series A\nNo URL,,Seed\n",
                encoding="utf-8",
            )
            targets, _ = dcs.read_targets(roster, url_column="Website", name_column="Company", id_column=None, metadata_columns=["Stage"])
            cache = root / "cache"
            cache.mkdir()
            (cache / f"{targets[0].company_id}.json").write_text(
                json.dumps({"url": "https://acme.example/", "links": ["https://acme.example/careers", "https://acme.example/blog"]}),
                encoding="utf-8",
            )
            key = root / "key"
            key.write_text("test-key\n", encoding="utf-8")
            output = root / "registry.csv"
            errors = root / "errors.json"
            code = dcs.main(
                [
                    "--input", str(roster),
                    "--output", str(output),
                    "--cache-dir", str(cache),
                    "--errors-file", str(errors),
                    "--url-column", "Website",
                    "--name-column", "Company",
                    "--metadata-column", "Stage",
                    "--api-key-file", str(key),
                    "--include-homepage",
                ]
            )
            self.assertEqual(code, 0)
            rows = list(csv.DictReader(output.open(encoding="utf-8")))
            self.assertEqual([r["Source Type"] for r in rows], ["homepage", "careers", "blog"])
            self.assertEqual(rows[1]["Website URL"], "https://acme.example/careers")
            self.assertEqual(rows[1]["Company ID"], targets[0].company_id)
            self.assertEqual(rows[1]["Stage"], "Series A")
            self.assertEqual(json.loads(errors.read_text()), {})


if __name__ == "__main__":
    unittest.main()
