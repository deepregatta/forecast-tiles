#!/usr/bin/env bash
# Validate workflow syntax, expressions and job dependencies with verified actionlint.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

actionlint_version=1.7.12
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)
    actionlint_arch=amd64
    actionlint_digest=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
    ;;
  Linux/aarch64)
    actionlint_arch=arm64
    actionlint_digest=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
    ;;
  *) echo "This wrapper supports Linux amd64/arm64." >&2; exit 2 ;;
esac

actionlint_scratch=$(mktemp -d "${TMPDIR:-/tmp}/forecast-actionlint.XXXXXX")
trap 'rm -rf "$actionlint_scratch"' EXIT
actionlint_archive="actionlint_${actionlint_version}_linux_${actionlint_arch}.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --retry 3 \
  "https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/${actionlint_archive}" \
  -o "$actionlint_scratch/$actionlint_archive"
printf '%s  %s\n' "$actionlint_digest" "$actionlint_scratch/$actionlint_archive" | sha256sum --check
tar -xzf "$actionlint_scratch/$actionlint_archive" -C "$actionlint_scratch" actionlint
"$actionlint_scratch/actionlint" -version
# Keep optional shellcheck/pyflakes availability from changing this workflow gate.
"$actionlint_scratch/actionlint" -shellcheck= -pyflakes= "$@"
