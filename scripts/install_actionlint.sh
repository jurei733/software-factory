#!/usr/bin/env bash
set -euo pipefail

version=1.7.12
archive="actionlint_${version}_linux_amd64.tar.gz"
expected=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
destination="${RUNNER_TEMP:?RUNNER_TEMP fehlt}/factory-actionlint"
mkdir -p "$destination"
curl --fail --silent --show-error --location \
  "https://github.com/rhysd/actionlint/releases/download/v${version}/${archive}" \
  --output "$destination/$archive"
echo "$expected  $destination/$archive" | sha256sum --check -
tar -xzf "$destination/$archive" -C "$destination" actionlint
echo "$destination" >> "${GITHUB_PATH:?GITHUB_PATH fehlt}"
