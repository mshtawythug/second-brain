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
  undo the change. With no local image, Compose's default `pull_policy`
  (`missing`) builds from the `build:` stanza. That is the fallback.
- The compose service has both `image:` and `build:`. A pull fetches the
  `image:` ref, and a fallback build tags its result with that same ref. One
  string names both paths, so the two cannot drift apart in the workflows.

## Files

| File | Change |
|---|---|
| `tests/test_age_image_pull_first_contract.py` (new, `nodb`) | Written first, and fails first. It finds every workflow that names `docker-compose.age-test.yml`, and the set must include ci/eval/benchmark. Each must run a compose `pull … \|\| true` step before an `up -d` step that has no `--build`. No workflow may name the image ref directly. No workflow may still carry the pull-first TODO. The compose service must keep both `image:` and `build:`. |
| `.github/workflows/eval.yml` | Two steps in place of one. Remove the TODO. Correct the header comment that said `up -d --build`. |
| `.github/workflows/benchmark.yml` | The same as eval.yml. Correct the header's "build + start" prose and its stale `pgv0.8.2` tag. |
| `CLAUDE.md`, `AGENTS.md` | "Eval gate (CI)" step 1 now describes pull-first with a build fallback. The two files hold the same paragraph. |

Out of scope: the local instructions (`CONTRIBUTING.md`, `tests/conftest.py`,
`tests/test_maintenance.py`, the compose header) keep `up -d --build`. It is
still a correct local command. The historical specs are not edited.

## Risks

1. **A pulled image older than the Dockerfile. The version pin covers part of
   this, and part is left over.** The tag encodes the pins
   (`pg16-v<AGE ref>-pgv<pgvector>`).
   `tests/test_packaging_templates.py::test_age_image_version_pins_agree_everywhere`
   fails when the Dockerfile `FROM`/`AGE_REF` and the compose tag disagree. So
   a version bump without a tag bump cannot pass CI.
   **Left over:** a Dockerfile edit that changes no version (a new apt
   package, a build flag, a `RUN` tweak) keeps the same tag. The pull then
   succeeds against the previously published image. That PR's CI never builds
   the changed Dockerfile, and a broken edit can go green. `publish-age-image.yml`
   rebuilds and re-pushes the same mutable tag on the next master push that
   touches `src/brain/templates/docker/age/**`, so the next master run would
   catch it, one merge late. `ci.yml` already had this exposure. Changing
   eval/benchmark adds no new kind of risk. Workaround: a PR that edits the
   Dockerfile should bump the tag, or be checked locally with `up -d --build`.
   A paths-filtered forced build is a possible follow-up. It is not done here.
2. **The image goes private again, or GHCR is down.** `|| true` falls through
   to the `build:` fallback. CI is slower but still correct.
3. **The tag is mutable.** A digest pin would make it immutable, but the build
   fallback cannot produce a digest ref. The trade-off is accepted, the same as
   in `ci.yml`.

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
