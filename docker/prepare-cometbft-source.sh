#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

checksum() {
  local actual
  actual=$(sha256sum "$2")
  if [[ ${actual%% *} != "$1" ]]; then
    fail "CometBFT input checksum failed: $2"
  fi
}

[[ $# == 3 ]] || fail 'Usage: prepare-cometbft-source.sh ARCHIVE DESTINATION PATCH'
archive=$1
destination=$2
history_patch=$3
: "${COMETBFT_VERSION:?}" "${COMETBFT_SOURCE_COMMIT:?}" "${COMETBFT_SOURCE_URL:?}"
: "${COMETBFT_SOURCE_SHA256:?}" "${COMETBFT_PATCH_SHA256:?}" "${COMETBFT_HISTORY_REVISION:?}"
[[ ${COMETBFT_VERSION} == 0.40.0 ]] || fail 'History revision 1 requires COMETBFT_VERSION=0.40.0'
[[ ${COMETBFT_SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ ]] || fail 'Invalid COMETBFT_SOURCE_COMMIT'
[[ ${COMETBFT_SOURCE_SHA256} =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid COMETBFT_SOURCE_SHA256'
[[ ${COMETBFT_PATCH_SHA256} =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid COMETBFT_PATCH_SHA256'
[[ ${COMETBFT_HISTORY_REVISION} == 1 ]] || fail 'Unsupported COMETBFT_HISTORY_REVISION'
[[ ${COMETBFT_SOURCE_URL} == "https://codeload.github.com/cometbft/cometbft/tar.gz/${COMETBFT_SOURCE_COMMIT}" ]] || fail 'Source URL must identify the pinned CometBFT commit'
[[ ! -e ${destination} && ! -L ${destination} ]] || fail 'Prepared source destination already exists'
checksum "$COMETBFT_SOURCE_SHA256" "$archive"
checksum "$COMETBFT_PATCH_SHA256" "$history_patch"

staging=$(mktemp -d "${destination}.stage.XXXXXX")
trap 'if [[ -n ${staging} ]]; then rm -rf -- "$staging"; fi' EXIT
tar -xzf "$archive" --strip-components=1 -C "$staging"
git -C "$staging" apply --check --whitespace=error "$history_patch"
git -C "$staging" apply --whitespace=error "$history_patch"
version_file="$staging/version/version.go"
sed "s/TMCoreSemVer = \"[^\"]*\"/TMCoreSemVer = \"${COMETBFT_VERSION}\"/" "$version_file" > "$version_file.tmp"
mv "$version_file.tmp" "$version_file"
grep -F "TMCoreSemVer = \"${COMETBFT_VERSION}\"" "$version_file" >/dev/null
printf '{"version":"%s","source_url":"%s","source_commit":"%s","source_sha256":"%s","patch_sha256":"%s","history_revision":"%s"}\n' \
  "$COMETBFT_VERSION" "$COMETBFT_SOURCE_URL" "$COMETBFT_SOURCE_COMMIT" \
  "$COMETBFT_SOURCE_SHA256" "$COMETBFT_PATCH_SHA256" "$COMETBFT_HISTORY_REVISION" \
  > "$staging/xian-cometbft-provenance.json"
mv "$staging" "$destination"
staging=''
