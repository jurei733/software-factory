"""Install the checksum-locked scanner directly from official release assets."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import ssl
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 200 * 1024 * 1024
HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def check_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port not in (None, 443):
        raise ValueError("Scanner download must use an approved HTTPS release host")
    if parsed.username or parsed.password:
        raise ValueError("Scanner download must not contain credentials")


class ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, digest, opener=None, sleep=time.sleep):
    check_url(url)
    opener = opener or build_opener(HTTPSHandler(context=ssl.create_default_context()), ReleaseRedirect())
    for attempt in range(3):
        try:
            with opener.open(Request(url, headers={"User-Agent": "software-factory-scanner"}), timeout=60) as response:
                check_url(response.geturl())
                data = response.read(LIMIT + 1)
            break
        except (URLError, TimeoutError, OSError) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429) and error.code < 500:
                raise RuntimeError("Scanner release download rejected (HTTP %d)" % error.code) from None
            if attempt == 2:
                raise RuntimeError("Scanner release download failed after three attempts") from None
            sleep(2 ** (attempt + 1))
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Scanner release checksum or size mismatch")
    return data


def extract_binary(data, destination):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name == "trivy"]
        if len(members) != 1 or not members[0].isfile() or members[0].size > LIMIT:
            raise ValueError("Release must contain exactly one regular scanner binary")
        binary = archive.extractfile(members[0])
        with binary, destination.open("xb") as output:
            output.write(binary.read(LIMIT + 1))
    destination.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((ROOT / "config/security/tools.json").read_text())["trivy"]
    machine = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    key = platform.system().lower() + "-" + str(machine)
    if key not in config["assets"]:
        parser.error("Unsupported scanner platform: " + key)
    asset = config["assets"][key]
    url = "https://github.com/aquasecurity/trivy/releases/download/v%s/%s" % (config["version"], asset["name"])
    data = download(url, asset["sha256"])
    args.directory.mkdir(parents=True, exist_ok=True)
    extract_binary(data, args.directory / "trivy")
    if os.environ.get("GITHUB_PATH"):
        with Path(os.environ["GITHUB_PATH"]).open("a") as output:
            output.write(str(args.directory.resolve()) + "\n")
    print("Installed checksum-verified Trivy " + config["version"])


if __name__ == "__main__":
    main()
