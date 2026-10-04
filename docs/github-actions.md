# GitHub Actions and main history safety

## Reviewed pins (2026-10-04)

All 29 external action uses in the 12 workflows are full upstream commit
SHAs. There are no local actions, Docker actions or reusable workflows in this
inventory. The previously selected major tags resolved to these releases; no
major version was upgraded:

| Upstream action | Original ref | Reviewed release | Commit |
|---|---|---|---|
| [actions/checkout](https://github.com/actions/checkout/releases/tag/v4.4.0) | `v4` | `v4.4.0` | `11d5960a326750d5838078e36cf38b85af677262` |
| [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv/releases/tag/v6.8.0) | `v6` | `v6.8.0` | `d0cc045d04ccac9d8b7881df0226f9e82c39688e` |
| [actions/setup-node](https://github.com/actions/setup-node/releases/tag/v4.4.0) | `v4` | `v4.4.0` | `49933ea5288caeca8642d1e84afbd3f7d6820020` |
| [actions/upload-artifact](https://github.com/actions/upload-artifact/releases/tag/v4.6.2) | `v4` | `v4.6.2` | `ea165f8d65b6e75b540449e92b4886f43607fa02` |

REST refs and `git ls-remote` agreed on the major and exact release tags.
The `setup-uv` major ref is an unsigned annotated tag: its tag-object SHA is
`d0d8abe699bfb85fec6de9f7adb5ae17292296ff`, which is **not** the action commit.
It was peeled to the commit above. GitHub reported all four commits' signatures
as verified (`valid`). Upstream identity was checked in the named owner/repo,
alongside release notes and `action.yml`; all four run on the action's Node 20
runtime. These are dated observations, not permanent assurances of upstream
trust. [GitHub's pinning guidance](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions)
requires checking the action's own repository rather than a fork.

Every checkout sets `persist-credentials: false`: the jobs do not push Git
changes, and subsequent steps need no credential in Git configuration. Existing
`contents: read` permissions are retained. CI uses `push` to `main` and ordinary
`pull_request`, with no production secrets. Secret-bearing workflows use manual
dispatch or scheduled events; there is no `pull_request_target`, `workflow_run`
or `workflow_call` path. Manual dispatch can still select another repository
ref for a writer; only trusted writers should have repository access. Pinning
does not prevent an authorized writer from changing a workflow or exposing its
secrets. Credential/IAM changes require their own reviewed scope.

Provider schedules, dispatch inputs, job limits, per-layer concurrency with
`cancel-in-progress: false`, regional `max-parallel: 1`, artifact retention,
production gates and budgets are unchanged. No ingestion is started by CI.

## Workflow validation and manual updates

CI's Python job runs `bash scripts/check_workflows.sh` before dependency setup.
The same command works locally on Linux amd64/arm64. It downloads actionlint
**1.7.12** from [its upstream release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)
into a disposable directory, checks the reviewed SHA-256 before extracting or
executing, then validates every workflow's grammar, expressions and job
dependencies. Its digests were cross-checked against upstream release-asset
metadata and `actionlint_1.7.12_checksums.txt`. Missing network/downloads or a
digest mismatch fail the gate. Optional shellcheck/pyflakes are disabled so
their installation does not change this gate; Python/Ruff and dispatcher tests
remain the substantive repository checks. The existing `scripts/verify.sh`
stays offline and also tests the read-only action-ref helper.

At the owner's monthly maintenance review, and promptly for an upstream
security advisory, inspect the four upstream releases/advisories. No recurring
job, automatic merge, required PR, new subscription or paid service is enabled.
Use this procedure for each proposed update:

1. Review upstream release notes, `action.yml`, distributed source changes,
   runner requirements and advisories. Keep the current major unless a reviewed
   security/compatibility reason warrants an upgrade. The action runtime and
   the installed Python/Node/uv versions are separate dependencies; a commit
   pin does not freeze downloaded toolchains or transitive runtime downloads.
2. Resolve the exact candidate version from its canonical upstream, e.g.:

   ```sh
   .venv/bin/python scripts/resolve_action_ref.py actions/checkout v4.4.0 \
     --expect 11d5960a326750d5838078e36cf38b85af677262
   ```

   The helper is read-only. It checks repository identity/archive status, peels
   annotated tags, compares REST with Git refs, verifies the commit endpoint,
   and prints only public ref/signature fields. `--expect` rejects moved tags.
   For a new release, first resolve without `--expect`, review that commit, then
   rerun with the reviewed SHA. An unsigned commit, owner transfer, ref mismatch
   or unexplained tag movement needs investigation, not an automatic repin.
3. Replace **every** use of that action, including job-level reusable workflows
   if any are added, with the full verified commit plus same-line `# vX.Y.Z`.
   Update the table above and the dated review. Inspect all `.yml` and `.yaml`
   workflows; new local/composite actions must have their nested dependencies
   reviewed too. Do not change permissions, triggers, credentials, concurrency
   or scientific fixtures merely to update an action.
4. Run `bash scripts/check_workflows.sh`, then `bash scripts/verify.sh` with
   lockfile-managed dependencies installed. For an actionlint update, verify
   the exact release asset digest in both upstream metadata and checksum file
   before changing the wrapper version/digests; rerun valid and deliberately
   invalid scratch workflow checks.
5. Stage only scoped paths, commit and push directly to `main`. Read back the
   pushed SHA and its hosted `ci` run/jobs. An invalid workflow, unavailable
   runner, quota or payment block is unresolved hosted proof. Observe ordinary
   scheduled writers separately when needed; CI success is not R2 delivery.

Rollback code with a scoped revert/new commit pushed to `main`, not a force
push. Restore a last verified immutable pin when an action update fails; do
not restore mutable tags as routine recovery. Do not run production writes
or the isolated live R2 test merely to validate this task.

## Proposed main ruleset — activation requires owner approval

The 2026-10-04 read-back found `main.protected=false`, branch protection GET
404, zero repository/inherited rulesets and zero effective branch rules.
Actions were enabled with `allowed_actions=all`, `sha_pinning_required=false`;
default token permission was `read`, and workflow PR approval was disabled.
These provider settings have not been changed by this preparation.

The exact [proposed REST payload](../.github/main-ruleset.proposed.json) is named
`main-history-safety`, targets only `refs/heads/main`, has enforcement `active`,
and contains just `deletion` and `non_fast_forward`. Its bypass actor list is
empty: owner, administrators, apps, deploy keys and other writers are subject
to both rules. Ordinary fast-forward direct pushes and existing dispatches
remain allowed; no actor needs a bypass for the authorized delivery flow.
No restriction on branch updates/creation, PR, review, signatures, linear
history, deployments or status checks is proposed.

[GitHub documents](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
that these rules block deletion and force-push, including default-branch
rename without bypass. The public repository can use branch rulesets on Free.
An administrator can disable this ruleset for an explicitly approved emergency;
no standing admin bypass is granted. The baseline direct-main push CI passed
at `036dc0bd406844fea4ca0c02ef34de1f64370e79` in
[run 37227257499](https://github.com/deepregatta/forecast-tiles/actions/runs/37227257499).
`python` and `dispatcher` run **after** the main push. Requiring their success
before that push would need a separately demonstrated bootstrap/contribution
flow. Their passing status alone is insufficient to propose mandatory checks.

After explicit approval of this payload, recheck main HEAD and all current
rules/protection with the whitelisted reads below. If they have changed,
reconcile rather than replacing someone else's settings. Use existing scoped
GitHub administration access; do not create a credential or alter plans:

```sh
gh api repos/deepregatta/forecast-tiles/branches/main --jq '{name,sha:.commit.sha,protected}'
gh api repos/deepregatta/forecast-tiles/rulesets --jq 'map({id,name,target,enforcement,source_type,source})'
gh api repos/deepregatta/forecast-tiles/rules/branches/main --jq 'map({type,ruleset_id,ruleset_source_type,ruleset_source})'
# Only after approval; save the returned ruleset ID privately.
gh api --method POST repos/deepregatta/forecast-tiles/rulesets \
  --input .github/main-ruleset.proposed.json \
  --jq '{id,name,target,enforcement,bypass_actors,conditions,rules}'
```

Read back that ID using `GET /repos/deepregatta/forecast-tiles/rulesets/ID`
(including `name,target,enforcement,bypass_actors,conditions,rules`), and the
effective rules endpoint above. Compare every field with the proposal. Verify
the next authorized ordinary fast-forward main commit and its CI/deployment
flow; no empty test commit, deletion, force-push or synthetic production write
is required. Settings read-back does not prove an attempted dangerous push was
rejected, and code CI does not prove provider activation.

Rollback, only for the **newly created ID**, is:

```sh
gh api --method PUT repos/deepregatta/forecast-tiles/rulesets/ID \
  --input .github/main-ruleset.rollback.json \
  --jq '{id,name,enforcement}'
```

The [rollback payload](../.github/main-ruleset.rollback.json) sets enforcement
to `disabled`; it deletes no rule, branch, commit or data. Read the ruleset and
effective branch rules again; before approval the observed baseline had no
effective rules. Other provider protections must be preserved if present at
activation. Never delete unrelated rulesets. Risk: administrators can still
change settings, and these two rules do not prevent faulty fast-forward code,
secret misuse, account compromise or spending. This proposal does not modify
the EUR 20 monthly budget, free plans, storage admission or `MAX_BUCKET_BYTES`.
Activation, provider read-back and post-activation contribution proof remain
open until actually performed.

REST schema: [create/update repository rulesets](https://docs.github.com/en/rest/repos/rules).
