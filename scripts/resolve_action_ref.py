#!/usr/bin/env python3
"""Read-only action update helper: verify upstream identity and peel tags to commits."""

import argparse
import json
import re
import subprocess


def api(endpoint, fields):
    # Request only public identity/ref/verification fields; never print auth state.
    result = subprocess.run(
        ["gh", "api", endpoint, "--jq", fields], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def git_ref(repository, tag):
    result = subprocess.run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{repository}.git",
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    refs = dict(line.split()[::-1] for line in result.stdout.splitlines())
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))


def resolve(repository, tag, expected_sha=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("expected an upstream owner/repository")
    if not re.fullmatch(r"v\d+(?:\.\d+)*", tag):
        raise ValueError("expected a stable version tag such as v4.4.0")
    if expected_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("expected a full 40-character commit SHA")
    identity = api(f"repos/{repository}", "{full_name,archived}")
    if identity["full_name"].lower() != repository.lower() or identity["archived"]:
        raise ValueError(
            "upstream moved, was archived, or has a different identity; review required"
        )
    ref = api(
        f"repos/{repository}/git/ref/tags/{tag}", "{ref,object:{type:.object.type,sha:.object.sha}}"
    )
    if ref["ref"] != f"refs/tags/{tag}":
        raise ValueError("unexpected upstream tag reference")
    obj = ref["object"]
    seen = set()
    while obj["type"] == "tag":
        if obj["sha"] in seen or len(seen) >= 8:
            raise ValueError("cyclic or excessively nested annotated tag")
        seen.add(obj["sha"])
        obj = api(
            f"repos/{repository}/git/tags/{obj['sha']}",
            "{object:{type:.object.type,sha:.object.sha}}",
        )["object"]
    if obj["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
        raise ValueError("action tag did not resolve to a full commit SHA")
    sha = obj["sha"]
    if git_ref(repository, tag) != sha:
        raise ValueError("Git and REST disagree; tag may have moved during verification")
    commit = api(
        f"repos/{repository}/commits/{sha}",
        "{sha,verification:{verified:.commit.verification.verified,reason:.commit.verification.reason}}",
    )
    if commit["sha"] != sha:
        raise ValueError("unexpected upstream commit")
    if expected_sha is not None and sha != expected_sha:
        raise ValueError("upstream tag differs from the reviewed pin; do not update automatically")
    return {"repository": identity["full_name"], "tag": tag, **commit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("tag")
    parser.add_argument("--expect", help="refuse a tag that differs from this reviewed commit")
    args = parser.parse_args()
    try:
        result = resolve(args.repository, args.tag, args.expect)
    except (ValueError, subprocess.CalledProcessError, KeyError) as exc:
        # Subprocess stdout/stderr may contain local auth diagnostics; do not echo them.
        parser.exit(
            1,
            f"Verification refused: {str(exc) if isinstance(exc, ValueError) else type(exc).__name__}\n",
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
