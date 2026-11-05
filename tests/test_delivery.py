import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delivery", ROOT / "scripts/publish_delivery.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.source = self.root / "payload"
        self.source.mkdir()
        (self.source / "app.js").write_text("checked build\n")
        self.environment = {"GITHUB_REPOSITORY": "example-owner/project", "GITHUB_REF": "refs/heads/main",
                            "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
        self.metadata = dict(delivery.context(self.environment), component="sample-api-frontend",
                             source_repository="example-owner/project", source_commit="b" * 40)

    def tearDown(self):
        self.folder.cleanup()

    def test_reproducible_bundle_binds_payload_to_source_and_run(self):
        outputs = [self.root / name for name in ("first.zip", "second.zip")]
        for output in outputs:
            delivery.bundle(self.source, output, self.metadata)
        self.assertEqual(delivery.sha256(outputs[0]), delivery.sha256(outputs[1]))
        with ZipFile(outputs[0]) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("delivery.json"))
            self.assertEqual(manifest["factory_commit"], "a" * 40)
            self.assertEqual(manifest["source_commit"], "b" * 40)
            self.assertEqual(manifest["files"][0]["sha256"], delivery.sha256(self.source / "app.js"))
            self.assertEqual(archive.read("payload/app.js"), b"checked build\n")

    def test_empty_missing_hidden_and_symlink_sources_fail(self):
        (self.source / "app.js").unlink()
        with self.assertRaisesRegex(ValueError, "no files"):
            delivery.bundle(self.source, self.root / "empty.zip", self.metadata)
        with self.assertRaises(ValueError):
            delivery.bundle(self.root / "absent", self.root / "missing.zip", self.metadata)
        (self.source / ".env").write_text("not for delivery")
        with self.assertRaisesRegex(ValueError, "no files"):
            delivery.bundle(self.source, self.root / "hidden.zip", self.metadata)
        (self.source / "app.js").write_text("checked build")
        (self.source / ".browserslistrc").write_text("last 2 versions")
        delivery.bundle(self.source, self.root / "visible.zip", self.metadata)
        with ZipFile(self.root / "visible.zip") as archive:
            self.assertEqual(archive.namelist(), ["payload/app.js", "delivery.json"])
        (self.source / ".env").unlink()
        (self.source / "link").symlink_to(ROOT / "README.md")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            delivery.bundle(self.source, self.root / "link.zip", self.metadata)

    def test_only_factory_main_can_publish(self):
        for key, value in (("GITHUB_REPOSITORY", "invalid/repo/extra"), ("GITHUB_REF", "refs/pull/1/merge"),
                           ("GITHUB_SHA", "main"), ("GITHUB_RUN_ID", "untrusted")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                delivery.context(dict(self.environment, **{key: value}))

    def test_upload_must_be_a_draft_with_matching_remote_digest(self):
        archive = self.root / "checked.zip"
        delivery.bundle(self.source, archive, self.metadata)
        calls = []

        class FakeGitHub:
            digest = "sha256:" + delivery.sha256(archive)

            def request(inner, method, path, value=None, upload=None):
                calls.append((method, path, value))
                if value:
                    return {"id": 100, "draft": value["draft"], "tag_name": value["tag_name"],
                            "html_url": "https://github.com/" + "example-owner/project" + "/releases"}
                return {"id": 200, "name": "sample-api-frontend.zip", "state": "uploaded",
                        "size": archive.stat().st_size, "digest": inner.digest}

        api = FakeGitHub()
        receipt = delivery.publish(api, archive, self.metadata)
        self.assertEqual(receipt["sha256"], delivery.sha256(archive))
        self.assertTrue(calls[0][2]["draft"])
        self.assertEqual(calls[0][2]["target_commitish"], "a" * 40)
        self.assertEqual(calls[0][2]["tag_name"], "ci-123-2-sample-api-frontend")
        api.digest = "sha256:" + "0" * 64
        with self.assertRaisesRegex(RuntimeError, "SHA-256 verification"):
            delivery.publish(api, archive, self.metadata)

    def test_bundle_from_another_repository_cannot_be_published(self):
        archive=self.root/"checked.zip"
        delivery.bundle(self.source,archive,self.metadata)
        with self.assertRaises(ValueError):delivery.verify_bundle(archive,dict(self.environment,GITHUB_REPOSITORY="another-owner/project"))

    def test_default_branch_can_have_any_name(self):
        env=dict(self.environment,FACTORY_DEFAULT_BRANCH="trunk",GITHUB_REF="refs/heads/trunk")
        self.assertEqual(delivery.context(env)["factory_repository"],"example-owner/project")
        with self.assertRaises(ValueError):delivery.context(dict(env,GITHUB_REF="refs/heads/main"))


if __name__ == "__main__":unittest.main()
