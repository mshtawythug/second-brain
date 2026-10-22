# CI: pull the AGE test image first in eval.yml and benchmark.yml

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal.** `eval.yml` and `benchmark.yml` start the Apache AGE test Postgres with
`docker compose -f docker-compose.age-test.yml up -d --build`, which compiles AGE
from C source on every run. Each carries `# TODO flip to pull-first once
ghcr.io/mshtawythug/second-brain-age is public`. `ci.yml` already pulls first.
This change does the same in the other two workflows. It is the follow-up that
`docs/specs/2026-07-25-sections/F11-F12-ci-gate-repository-hygiene-documentation.md`
(item 3, and open question 4) deferred.

## Evidence that the image is public (2026-10-22, anonymous, no `docker login`)

Image ref, read from `docker-compose.age-test.yml` `image:`:
`ghcr.io/mshtawythug/second-brain-age:pg16-v1.5.0-rc0-pgv0.8.6`.

- `GET https://ghcr.io/token?scope=repository:mshtawythug/second-brain-age:pull&service=ghcr.io`
  with no credentials returns an anonymous bearer token.
- `HEAD /v2/mshtawythug/second-brain-age/manifests/pg16-v1.5.0-rc0-pgv0.8.6`
  with that token returns **HTTP 200**, `application/vnd.oci.image.index.v1+json`,
  digest `sha256:b3dbf56b95528739008bca644820b744eb2473bb65de985273e3beb04d1868ea`.
  The same request without a token returns 401, so the 200 comes from the
  anonymous token. Nothing was cached.
- The index lists `linux/amd64` (`sha256:ea59e29e…bab00`) and `linux/arm64`
  (`sha256:8df43eec…e5271`). `HEAD` on the amd64 manifest returns **HTTP 200**.
  GitHub-hosted runners are amd64.
- The labels on the amd64 config agree with the Dockerfile pins:
  `brain.pgvector.version=0.8.6`, `brain.age.ref=PG16/v1.5.0-rc0`,
  `brain.age.commit=0048900f…`, base `pgvector/pgvector:0.8.6-pg16`. The image
  was created on 2026-08-08 from revision `63e1c92`.
  `git log 63e1c92..HEAD -- src/brain/templates/docker/age/` is **empty**, so the
  published image was built from the Dockerfile that is on master today.
- The pull path already works in CI: the `ci.yml` run on master 39665492729
  (2026-10-21) logs `postgres-age-test Pulling` and then `Pulled` in about 14 s.
  The `up -d` that follows starts the container in under 1 s and builds nothing.

## Change

Use the idiom `ci.yml` already uses, so all three workflows share one shape:

```yaml
- name: Pull the pinned AGE image (falls back to a local build)
  run: docker compose -f docker-compose.age-test.yml pull --quiet || true
- name: Start the AGE test instance
  run: docker compose -f docker-compose.age-test.yml up -d
```

- `pull || true`: when the registry misses, the pull does not fail the job.
- `up -d` without `--build`: `--build` would rebuild over the pulled image and
  undo the change. With no `pull_policy` set, `up` reuses a local copy of the
  tag when one exists. When none exists, it tries the registry and builds from
  `build:` only if that pull fails. That is the fallback. This was checked on
  Compose v5.1.4 with a throwaway project that had both `image:` and `build:`:
  an absent, pullable image was pulled and not built; an unpullable ref
  printed "must be built from source" and was then built. The Compose source
  (`ensureImagesExists`: pull the required images, then build what is still
  missing) agrees. The old `ci.yml` header said a bare `up` "builds rather
  than pulls"; that was wrong and has been corrected. The explicit pull step
  does not depend on this `up`-time behaviour. It also refreshes a cached copy
  of the mutable tag, which a bare `up` would reuse.
- The compose service has both `image:` and `build:`. A pull fetches the
  `image:` ref, and a fallback build tags its result with that same ref. One
  string names both paths, so the two cannot drift apart in the workflows.
- **The PR-time Dockerfile build moves to `publish-age-image.yml` (review
  round 1).** Before this change, eval and benchmark (`up -d --build`) were
  the only workflows that built the Dockerfile on a PR, and the publish
  workflow had no `pull_request` trigger. Pull-first removed the last PR-time
  build. The publish workflow now has a `pull_request` leg, with `paths:` set
  to `src/brain/templates/docker/age/**` and the workflow file itself. On that
  leg it builds `linux/amd64` only, skips QEMU and the GHCR login, and does not
  push. The concurrency group is per ref for PRs (`publish-age-image-<ref>`)
  and stays a single group for publishes (`publish-age-image-publish`), so a PR
  run cannot cancel a master publish. The master leg is unchanged: same push
  `paths:`, multi-arch, pushed, `type=gha` cache.

## Files

| File | Change |
|---|---|
| `tests/test_age_image_pull_first_contract.py` (new, `nodb`) | Written first, and fails first. It finds every workflow that names `docker-compose.age-test.yml`, and the set must include ci/eval/benchmark. Each must run a compose `pull … \|\| true` step before an `up -d` step. Neither `--build` nor a compose `build` step is allowed. The image ref may not appear anywhere in the parsed workflow (`run:`, `env:`, `with:`; comments do not count). Neither the TODO nor the text `up -d --build` may appear, comments included. The compose service must keep both `image:` and `build:`. |
| `tests/test_publish_age_image_workflow_contract.py` (new, `nodb`) | The PR leg: the trigger and both paths; no push, amd64 only; QEMU and login skipped; a non-static concurrency group. Also that the master leg is unchanged: push paths, cache, context, tags, labels. |
| `tests/test_packaging_templates.py` | New `test_every_tracked_age_image_tag_matches_the_dockerfile_pins`: every `second-brain-age:pg16-…` tag and every `pgvector X.Y.Z +` phrase in a tracked file outside `docs/`, `CHANGELOG.md` and the migrations must match the Dockerfile pins. |
| `.github/workflows/eval.yml`, `benchmark.yml` | Two steps in place of one. Remove the TODO. Correct header prose (`up -d --build`, the stale `pgv0.8.2` / `pgvector 0.8.2`). |
| `.github/workflows/ci.yml` | Header now describes all three AGE workflows as pulling first, gives the accurate `up` behaviour, and says "becomes private again". |
| `.github/workflows/publish-age-image.yml` | The PR build-check leg described above. |
| `bin/brain-graphrag-gif`, `src/brain/templates/docker-compose.yml.j2`, `src/brain/templates/env.example`, `.env.example` | Stale `pgv0.8.2` tag and `pgvector 0.8.2` prose → `0.8.6`. The 0.8.2 → 0.8.6 bump (`63e1c92`) missed these. None was meant to stay old: the "production stays on 0.8.2" note in `63e1c92` is about the machine-local, gitignored `docker-compose.override.yml`, which none of these files feeds. |
| `CONTRIBUTING.md` | Contributors run `pull`, then `up -d`. `--build` is described as forcing a local compile. |
| `CLAUDE.md`, `AGENTS.md` | "Eval gate (CI)" step 1 now describes pull-first with a build fallback. The two files hold the same paragraph. |

Out of scope: `tests/conftest.py`, `tests/test_maintenance.py`, the compose
header, and the `env.example` "Start it" lines keep `up -d --build`. It is
still a correct local command. The historical specs are not edited.

## Risks

1. **A pulled image older than the Dockerfile.** The tag encodes the pins
   (`pg16-v<AGE ref>-pgv<pgvector>`), and
   `test_age_image_version_pins_agree_everywhere` fails when the Dockerfile and
   the compose tag disagree. So a version bump without a tag bump cannot pass
   CI. A Dockerfile edit that changes no version (an apt package, a build flag)
   keeps the same tag. Its PR therefore pulls the previously published image in
   ci, eval and benchmark.
   **The first draft of this plan said "`ci.yml` already had this exposure, so
   nothing new." That was false.** Before this change, eval and benchmark built
   the Dockerfile on every PR, and they were the only PR-time build. Removing
   them would have opened the gap. The `pull_request` leg of
   `publish-age-image.yml` closes it: any PR touching the Dockerfile directory
   builds it (amd64).
   **Left over:** that build checks that the image *builds*. Tests still run
   against the pulled image, so a Dockerfile edit that builds but breaks AGE at
   runtime is caught only after merge, when `publish-age-image.yml` re-pushes
   the tag and the next ci run pulls it. An arm64-only build failure is also
   caught only on the master publish. Bumping the tag on such edits makes the
   PR's ci fall back to building that Dockerfile and test it.
2. **The image goes private again, or GHCR is down.** `|| true` falls through
   to the `build:` fallback. CI is slower but still correct.
3. **The tag is mutable.** A digest pin would make it immutable, but the build
   fallback cannot produce a digest ref. The trade-off is accepted, the same as
   in `ci.yml`.
4. **Re-running `brain setup --profile full` moves an existing install to
   0.8.6.** `run_setup` re-renders `$BRAIN_HOME/docker-compose.yml` on every
   run that passes preflight (`_write_compose_yml`, an unconditional
   `atomic_write_text`). It then runs `docker compose pull` and `up -d` itself
   (`_compose_up`). So the re-run pulls `pgv0.8.6` and `up -d` recreates the
   Postgres container, in the same run. Preflight refuses an occupied Postgres
   port, so in practice this happens on a re-run with the stack stopped.
   Checked: binding a port Docker publishes fails with `EADDRINUSE` on macOS.
   With the stack up, setup aborts before it writes anything. The major stays
   PostgreSQL 16, so the data directory is unchanged. The `vector` extension
   stays at 0.8.2 until `ALTER EXTENSION vector UPDATE;`, because migration
   001's `CREATE EXTENSION IF NOT EXISTS` never updates it (pgvector README,
   "Upgrading"). Fresh installs were getting the old image before this change.
   Their pull got the 0.8.2 image, and the build fallback built 0.8.6 content
   under the 0.8.2 tag, which mislabelled it. The maintainer's own prod
   container is not rendered from this template. This is recorded in
   `CHANGELOG.md` under `[Unreleased]`, and supersedes the 0.3.0 note "keep
   running their current image until you rebuild deliberately".

## Verification

Run only the targeted tests, one after another:
`pytest tests/test_age_image_pull_first_contract.py tests/test_eval_workflow_contract.py tests/test_ci_workflow.py tests/test_packaging_templates.py --no-cov`.
On the PR, check the eval and benchmark job logs for `Pulling`/`Pulled` and no
AGE compile.

**Results (2026-10-22).** Red first: before the workflow edit, 4 of 11 failed
(eval and benchmark, the pull-first test and the TODO test), and ci.yml passed.
After the edit, the four targeted modules passed (49 tests).
`tests/test_reading_surface_pointers.py` passed (48). `ruff check` and
`mypy src/` are clean. Mutation check, each mutation applied in a scratch copy
and checked to have landed: dropping `|| true` (eval), adding `--build`
(benchmark, then ci), hard-coding the image ref (eval), deleting the compose
`build:` stanza, restoring the TODO (benchmark), and swapping the pull/up order
(eval). Each one turns exactly one test red. The unmutated copy passes 11/11.

**Review round 1 results (2026-10-22).**
- Red first: 7 failed before the fixes:
  - `up -d --build` in ci.yml's header (the old line 66);
  - all five PR-leg rules in `publish-age-image.yml`;
  - the tag scan, which named all five stale sites.
- After the fixes these pass, run one after another:
  - the pull-first contract, the publish contract, `test_packaging_templates`, `test_eval_workflow_contract` and `test_ci_workflow` (57);
  - the reading-surface, docs-asset, `brain-ci` and repo-hygiene tests (116);
  - the setup, dotenv, config and log-rotation tests (121), on a sibling test DB.
- actionlint 1.7.7 is clean on all four workflows. `ruff check` and `mypy src/` are clean.
- Mutation proof: 13 mutations, each in a fresh `git archive` copy and each checked to land. 12 turn exactly one test red:
  - eight on the publish workflow: no PR trigger, no workflow path, a static group, login on PRs, QEMU on PRs, push on PRs, multi-arch PR build, master cache changed;
  - the master push `paths:` widened;
  - a separate compose `build` step;
  - the image ref in an `env:` key;
  - `up -d --build` restored to ci.yml's comment.
  The 13th, the image ref in a comment only, stays green, as intended. The unmutated copy passes 18/18.
