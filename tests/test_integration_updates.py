import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import integration_updates as updates
import repository_factory


class IntegrationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.repo = self.root / "consumer"; self.repo.mkdir()
        (self.repo / "README.md").write_text("Synthetic independent consumer")
        config = repository_factory.draft_profile(self.repo, "example-owner/consumer", "custom")
        repository_factory.export_repository(config, self.root / "overlay")
        shutil.copytree(self.root / "overlay", self.repo, dirs_exist_ok=True)

    def test_old_owned_files_upgrade_with_preview_backup_and_custom_files_preserved(self):
        owned = self.repo / ".factory/factory.py"
        owned.write_text(owned.read_text() + "\n# Previous version\n")
        manifest = updates.read_manifest(self.repo)
        manifest["files"][".factory/factory.py"] = updates.digest(owned)
        manifest["factory"]["version"] = "0.0.1"
        (self.repo / updates.MANIFEST).write_text(json.dumps(manifest))
        config = self.repo / ".factory/config.json"; config.write_text(config.read_text() + "\n")
        task = self.repo / ".factory/tasks/team.json"; task.write_text('{"custom":true}')
        config_before, task_before = config.read_bytes(), task.read_bytes()
        preview = updates.upgrade(self.repo, self.root / "preview")
        self.assertFalse(preview["applied"])
        self.assertIn("Previous version", owned.read_text())
        receipt = updates.upgrade(self.repo, self.root / "applied", apply=True)
        self.assertTrue(receipt["applied"])
        self.assertEqual(owned.read_bytes(), (ROOT / "factory.py").read_bytes())
        self.assertEqual(config.read_bytes(), config_before); self.assertEqual(task.read_bytes(), task_before)
        self.assertIn("Previous version", (self.root / "applied/backup/.factory/factory.py").read_text())
        self.assertFalse(updates.drift(self.repo, updates.read_manifest(self.repo)))

    def test_local_edits_missing_files_links_and_collisions_fail_without_writes(self):
        owned = self.repo / ".factory/factory.py"; original = owned.read_bytes()
        owned.write_text("local edit")
        with self.assertRaisesRegex(factory.FactoryError, "drift"):
            updates.upgrade(self.repo, self.root / "conflict", apply=True)
        self.assertEqual(owned.read_text(), "local edit")
        owned.unlink(); owned.symlink_to(ROOT / "factory.py")
        with self.assertRaisesRegex(factory.FactoryError, "links"):
            updates.upgrade(self.repo, self.root / "link", apply=True)
        owned.unlink(); owned.write_bytes(original)
        manifest = updates.read_manifest(self.repo); del manifest["files"][".factory/factory.py"]
        (self.repo / updates.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(factory.FactoryError, "collides"):
            updates.upgrade(self.repo, self.root / "collision", apply=True)
        self.assertEqual(owned.read_bytes(), original)

    def test_classic_yarn_gets_its_own_immutable_install_option(self):
        (self.repo / "package.json").write_text(json.dumps({"packageManager": "yarn@1.22.22", "scripts": {"test": "true"}}))
        (self.repo / "yarn.lock").write_text("# yarn lockfile v1\n")
        profile = repository_factory.draft_profile(self.repo, "example-owner/consumer")
        self.assertEqual(profile["ci"]["install"][0]["argv"], ["corepack", "yarn", "install", "--frozen-lockfile"])


if __name__ == "__main__":
    unittest.main()
