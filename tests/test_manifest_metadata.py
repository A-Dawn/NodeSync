from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHOR = {"name": "A-Dawn", "url": "https://github.com/A-Dawn"}
LICENSE = "GPL-3.0-only"
REPOSITORY = "https://github.com/A-Dawn/NodeSync"


class ManifestMetadataTest(unittest.TestCase):
    def test_registry_manifest_contains_market_required_fields(self) -> None:
        manifest = _load_manifest(ROOT / "_manifest.json")

        for field in ("manifest_version", "name", "version", "description", "author", "license", "host_application"):
            self.assertIn(field, manifest)

        self.assertEqual(manifest["author"], AUTHOR)
        self.assertEqual(manifest["license"], LICENSE)
        self.assertEqual(manifest["repository_url"], REPOSITORY)
        self.assertEqual(manifest["homepage_url"], REPOSITORY)
        self.assertEqual(manifest["host_application"]["min_version"], "0.12.2")

    def test_adapter_manifests_share_public_metadata(self) -> None:
        manifest_012 = _load_manifest(ROOT / "nodesync" / "adapters" / "maibot_012" / "_manifest.json")
        manifest_10 = _load_manifest(ROOT / "nodesync" / "adapters" / "maibot_10" / "_manifest.json")

        self.assertEqual(manifest_012["author"], AUTHOR)
        self.assertEqual(manifest_012["license"], LICENSE)
        self.assertEqual(manifest_012["repository_url"], REPOSITORY)
        self.assertEqual(manifest_012["homepage_url"], REPOSITORY)

        self.assertEqual(manifest_10["author"], AUTHOR)
        self.assertEqual(manifest_10["license"], LICENSE)
        self.assertEqual(manifest_10["urls"]["repository"], REPOSITORY)
        self.assertEqual(manifest_10["urls"]["homepage"], REPOSITORY)


def _load_manifest(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return data


if __name__ == "__main__":
    unittest.main()
