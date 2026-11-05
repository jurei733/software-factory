import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import URLError, HTTPError

spec = importlib.util.spec_from_file_location("install_scanner", Path(__file__).resolve().parents[1] / "scripts/install_scanner.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)
URL = "https://github.com/aquasecurity/trivy/releases/download/v0.76.1/synthetic.tar.gz"


class ScannerInstallTests(unittest.TestCase):
    def response(self, data):
        response = io.BytesIO(data)
        response.geturl = lambda: URL
        return response

    def test_retries_network_failure_then_verifies_checksum(self):
        opener = Mock()
        opener.open.side_effect = [URLError("temporary"), self.response(b"synthetic")]
        sleep = Mock()
        self.assertEqual(scanner.download(URL, hashlib.sha256(b"synthetic").hexdigest(), opener, sleep), b"synthetic")
        sleep.assert_called_once_with(2)
        self.assertEqual(opener.open.call_count, 2)

    def test_checksum_and_permanent_http_failures_are_not_retried(self):
        for response in (self.response(b"modified"), HTTPError(URL, 404, "missing", {}, None)):
            opener = Mock()
            opener.open.side_effect = [response] if isinstance(response, Exception) else None
            opener.open.return_value = response
            with self.assertRaises((ValueError, RuntimeError)):
                scanner.download(URL, "0" * 64, opener, Mock())
            self.assertEqual(opener.open.call_count, 1)
        opener.open.side_effect = URLError("offline")
        with self.assertRaises(RuntimeError):
            scanner.download(URL, "0" * 64, opener, Mock())
        self.assertEqual(opener.open.call_count, 4)

    def test_unapproved_download_hosts_and_redirects_fail(self):
        for url in ("http://github.com/file", "https://github.com.evil.invalid/file", "https://user@github.com/file"):
            with self.assertRaises(ValueError):
                scanner.check_url(url)
        with self.assertRaises(ValueError):
            scanner.ReleaseRedirect().redirect_request(None, None, 302, "redirect", {}, "http://github.com/file")

    def archive(self, symbolic=False):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("trivy")
            if symbolic:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/outside"
                archive.addfile(entry)
            else:
                entry.size = 6
                archive.addfile(entry, io.BytesIO(b"binary"))
            unrelated = tarfile.TarInfo("../outside")
            unrelated.size = 4
            archive.addfile(unrelated, io.BytesIO(b"data"))
        return buffer.getvalue()

    def test_extracts_only_regular_binary_without_unpacking_archive_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "trivy"
            scanner.extract_binary(self.archive(), target)
            self.assertEqual(target.read_bytes(), b"binary")
            self.assertEqual(list(Path(folder).iterdir()), [target])
            with self.assertRaises(FileExistsError):
                scanner.extract_binary(self.archive(), target)
            target.unlink()
            with self.assertRaises(ValueError):
                scanner.extract_binary(self.archive(symbolic=True), target)


if __name__ == "__main__":
    unittest.main()
