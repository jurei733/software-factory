from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import delivery_metrics
import publish_delivery
import verify_promotion


class PromotionAndMetricsTests(unittest.TestCase):
    def test_oversized_manifest_is_rejected_before_decompression(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "oversized.zip"
            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
                package.writestr("delivery.json", b" " * (4 * 1024 ** 2 + 1))
            with patch.object(ZipFile, "open", side_effect=AssertionError("must not decompress")):
                with self.assertRaisesRegex(ValueError, "Oversized delivery manifest"):
                    verify_promotion.verify(archive, "production", repository="example-owner/project")

    def test_production_requires_clean_evidence_and_exact_platform_attestation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);source = root / "source";source.mkdir()
            (source / "app.js").write_text("synthetic")
            env = {"GITHUB_REPOSITORY": "example-owner/project", "GITHUB_REF": "refs/heads/main",
                   "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            metadata = dict(publish_delivery.context(env), component="sample-web-frontend",
                            source_repository="example-owner/project", source_commit="b" * 40)
            unsigned = root / "unsigned.zip";publish_delivery.bundle(source, unsigned, metadata)
            verifier = Mock()
            with self.assertRaisesRegex(ValueError, "security evidence"):
                verify_promotion.verify(unsigned, "production", repository="example-owner/project", verifier=verifier)
            verifier.assert_not_called()
            self.assertEqual(verify_promotion.verify(unsigned, "isolated-staging", repository="example-owner/project")["status"], "verified")
            evidence = root / "evidence";evidence.mkdir()
            (evidence / "security-summary.json").write_text(json.dumps(
                {"schema_version": 1, "status": "passed", "production_eligible": True}))
            (evidence / "sbom.cdx.json").write_text('{"bomFormat":"CycloneDX"}')
            clean = root / "clean.zip";publish_delivery.bundle(source, clean, metadata, evidence)
            result = verify_promotion.verify(clean, "production", repository="example-owner/project", verifier=verifier)
            self.assertFalse(result["deployment_performed"])
            args = verifier.call_args.args[0]
            self.assertIn("--source-ref", args)
            self.assertIn("refs/heads/main", args)
            self.assertIn("--deny-self-hosted-runners", args)
            self.assertIn("--no-public-good", args)
            self.assertIn("example-owner/project" + "/.github/workflows/factory.yml", args)
            verifier.side_effect = RuntimeError("Invalid attestation")
            with self.assertRaisesRegex(RuntimeError, "Invalid attestation"):
                verify_promotion.verify(clean, "production", repository="example-owner/project", verifier=verifier)

    def test_metrics_exclude_staging_other_apps_and_old_events_and_preserve_unknowns(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        unknown = delivery_metrics.calculate([], "sample-web", now=now)
        self.assertIsNone(unknown["deployment_frequency_per_day"])
        self.assertIsNone(unknown["change_fail_rate"])
        events = [
            {"application": "sample-web", "environment": "production", "kind": "deploy", "status": "passed",
             "time": "2026-11-03T12:00:00Z", "committed_at": "2026-11-03T11:00:00Z", "unplanned_rework": False},
            {"application": "sample-web", "environment": "production", "kind": "deploy", "status": "failed",
             "time": "2026-11-03T13:00:00Z", "recovered_at": "2026-11-03T13:10:00Z", "unplanned_rework": True},
            {"application": "sample-web", "environment": "isolated-staging", "kind": "deploy", "status": "passed"},
            {"application": "sample-worker", "environment": "production", "kind": "deploy", "status": "passed"},
            {"application": "sample-web", "environment": "production", "kind": "deploy", "status": "passed",
             "time": "2025-01-01T00:00:00Z"}
        ]
        result = delivery_metrics.calculate(events, "sample-web", now=now)
        self.assertEqual(result["production_deployments"], 2)
        self.assertEqual(result["change_lead_time_seconds_median"], 3600)
        self.assertEqual(result["failed_deployment_recovery_seconds_median"], 600)
        self.assertEqual(result["change_fail_rate"], .5)
        self.assertEqual(result["deployment_rework_rate"], .5)
        self.assertEqual(result["excluded_events_outside_scope"], 3)

    def test_incomplete_metric_samples_remain_unknown_with_visible_coverage(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        shared = {"application": "sample-web", "environment": "production", "kind": "deploy",
                  "time": "2026-11-03T12:00:00Z"}
        events = [dict(shared, status="passed", committed_at="2026-11-03T11:00:00Z", unplanned_rework=False),
                  dict(shared, status="passed"),
                  dict(shared, status="failed", recovered_at="2026-11-03T12:10:00Z", unplanned_rework=True),
                  dict(shared, status="failed")]
        result = delivery_metrics.calculate(events, "sample-web", now=now)
        for field in ("change_lead_time_seconds_median", "failed_deployment_recovery_seconds_median", "deployment_rework_rate"):
            self.assertIsNone(result[field])
        self.assertEqual(result["metric_evidence"]["lead_time"], {"recorded": 1, "expected": 2})
        self.assertEqual(result["metric_evidence"]["recovery_time"], {"recorded": 1, "expected": 2})
        self.assertEqual(result["metric_evidence"]["rework"], {"recorded": 2, "expected": 4})


if __name__ == "__main__":
    unittest.main()
