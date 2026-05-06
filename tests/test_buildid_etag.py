"""Phase 6 checks for build-id ETag reload polling."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELOAD_JS = REPO_ROOT / "quartz_overrides" / "quartz" / "static" / "reload.js"
README = REPO_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing expected file: {path}"
    return path.read_text(encoding="utf-8")


def test_reload_client_uses_conditional_build_id_requests(tmp_path: Path) -> None:
    """The reload watcher should use ETags, honor 304, and reload on a new id."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reload.js runtime harness")

    source = _read(RELOAD_JS)
    harness = tmp_path / "reload-harness.mjs"
    harness.write_text(
        textwrap.dedent(
            f"""
            import vm from "node:vm"

            const source = {json.dumps(source)}
            const calls = []
            const intervals = []
            let reloads = 0

            const responses = [
              {{ status: 200, ok: true, etag: '"build-a"', body: "20260505-200000-aaaaaa\\n" }},
              {{ status: 304, ok: false, etag: '"build-a"', body: "" }},
              {{ status: 200, ok: true, etag: '"build-b"', body: "20260505-200001-bbbbbb\\n" }},
            ]

            function responseFor(entry) {{
              return {{
                status: entry.status,
                ok: entry.ok,
                headers: {{
                  get(name) {{
                    return name.toLowerCase() === "etag" ? entry.etag : null
                  }},
                }},
                async text() {{
                  if (entry.status === 304) {{
                    throw new Error("304 responses must not read a body")
                  }}
                  return entry.body
                }},
              }}
            }}

            const context = {{
              console: {{ log() {{}} }},
              Date: {{ now() {{ return 123456789 }} }},
              document: {{
                visibilityState: "visible",
                addEventListener() {{}},
              }},
              location: {{
                reload() {{
                  reloads += 1
                }},
              }},
              setInterval(fn, ms) {{
                intervals.push({{ fn, ms }})
                return intervals.length
              }},
              clearInterval() {{}},
              fetch(url, options = {{}}) {{
                calls.push({{ url, options }})
                const entry = responses.shift()
                if (!entry) throw new Error("unexpected fetch call")
                return Promise.resolve(responseFor(entry))
              }},
            }}

            function assert(condition, message) {{
              if (!condition) {{
                throw new Error(message)
              }}
            }}

            async function flush() {{
              await new Promise((resolve) => setImmediate(resolve))
              await new Promise((resolve) => setImmediate(resolve))
            }}

            vm.runInNewContext(source, context)
            await flush()

            assert(intervals.length === 1, "reload watcher should register one timer")
            assert(calls[0].url === "/.build-id", "first poll should not cache-bust")
            assert(calls[0].options.cache !== "no-store", "first poll must not use no-store")
            assert(!calls[0].options.headers?.["If-None-Match"], "first poll has no ETag yet")

            await intervals[0].fn()
            await flush()

            assert(calls[1].url === "/.build-id", "304 poll should not cache-bust")
            assert(
              calls[1].options.headers["If-None-Match"] === '"build-a"',
              "second poll should send the cached ETag",
            )
            assert(reloads === 0, "304 response should not reload")

            await intervals[0].fn()
            await flush()

            assert(
              calls[2].options.headers["If-None-Match"] === '"build-a"',
              "changed-build poll should validate against the previous ETag",
            )
            assert(reloads === 1, "changed build id should reload once")
            """
        ),
        encoding="utf-8",
    )

    subprocess.run([node, str(harness)], check=True)


def test_reload_client_does_not_store_etag_until_body_is_valid(
    tmp_path: Path,
) -> None:
    """A 200 with an empty or invalid body must not advance the cached ETag."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reload.js runtime harness")

    source = _read(RELOAD_JS)
    harness = tmp_path / "reload-invalid-body-harness.mjs"
    harness.write_text(
        textwrap.dedent(
            f"""
            import vm from "node:vm"

            const source = {json.dumps(source)}
            const calls = []
            const intervals = []
            let reloads = 0

            const responses = [
              {{ status: 200, ok: true, etag: '"build-a"', body: "20260505-200000-aaaaaa\\n" }},
              {{ status: 200, ok: true, etag: '"build-b"', body: "<!doctype html>not a build id" }},
              {{ status: 304, ok: false, etag: '"build-a"', body: "" }},
            ]

            function responseFor(entry) {{
              return {{
                status: entry.status,
                ok: entry.ok,
                headers: {{
                  get(name) {{
                    return name.toLowerCase() === "etag" ? entry.etag : null
                  }},
                }},
                async text() {{
                  return entry.body
                }},
              }}
            }}

            const context = {{
              console: {{ log() {{}} }},
              document: {{
                visibilityState: "visible",
                addEventListener() {{}},
              }},
              location: {{
                reload() {{
                  reloads += 1
                }},
              }},
              setInterval(fn, ms) {{
                intervals.push({{ fn, ms }})
                return intervals.length
              }},
              clearInterval() {{}},
              fetch(url, options = {{}}) {{
                calls.push({{ url, options }})
                const entry = responses.shift()
                if (!entry) throw new Error("unexpected fetch call")
                return Promise.resolve(responseFor(entry))
              }},
            }}

            function assert(condition, message) {{
              if (!condition) throw new Error(message)
            }}

            async function flush() {{
              await new Promise((resolve) => setImmediate(resolve))
              await new Promise((resolve) => setImmediate(resolve))
            }}

            vm.runInNewContext(source, context)
            await flush()

            await intervals[0].fn()
            await flush()

            assert(reloads === 0, "invalid build-id body should not reload")

            await intervals[0].fn()
            await flush()

            assert(
              calls[2].options.headers["If-None-Match"] === '"build-a"',
              "invalid 200 body must not advance the cached ETag",
            )
            """
        ),
        encoding="utf-8",
    )

    subprocess.run([node, str(harness)], check=True)


def test_reload_client_preserves_visibility_pause_and_resume(tmp_path: Path) -> None:
    """Hidden tabs should not poll; visible tabs should restart one timer."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reload.js runtime harness")

    source = _read(RELOAD_JS)
    harness = tmp_path / "reload-visibility-harness.mjs"
    harness.write_text(
        textwrap.dedent(
            f"""
            import vm from "node:vm"

            const source = {json.dumps(source)}
            const calls = []
            const intervals = []
            const cleared = []
            const listeners = {{}}
            let visibilityState = "hidden"

            const responses = [
              {{ status: 200, ok: true, etag: '"build-a"', body: "20260505-200000-aaaaaa\\n" }},
              {{ status: 304, ok: false, etag: '"build-a"', body: "" }},
            ]

            function responseFor(entry) {{
              return {{
                status: entry.status,
                ok: entry.ok,
                headers: {{
                  get(name) {{
                    return name.toLowerCase() === "etag" ? entry.etag : null
                  }},
                }},
                async text() {{
                  return entry.body
                }},
              }}
            }}

            const context = {{
              console: {{ log() {{}} }},
              document: {{
                get visibilityState() {{
                  return visibilityState
                }},
                addEventListener(name, fn) {{
                  listeners[name] = fn
                }},
              }},
              location: {{ reload() {{}} }},
              setInterval(fn, ms) {{
                intervals.push({{ fn, ms }})
                return intervals.length
              }},
              clearInterval(handle) {{
                cleared.push(handle)
              }},
              fetch(url, options = {{}}) {{
                calls.push({{ url, options }})
                const entry = responses.shift()
                if (!entry) throw new Error("unexpected fetch call")
                return Promise.resolve(responseFor(entry))
              }},
            }}

            function assert(condition, message) {{
              if (!condition) throw new Error(message)
            }}

            async function flush() {{
              await new Promise((resolve) => setImmediate(resolve))
              await new Promise((resolve) => setImmediate(resolve))
            }}

            vm.runInNewContext(source, context)
            await flush()

            assert(intervals.length === 0, "hidden-at-load tab should not start polling")
            assert(calls.length === 0, "hidden-at-load tab should not fetch")

            visibilityState = "visible"
            listeners.visibilitychange()
            await flush()

            assert(intervals.length === 1, "visible transition should start one timer")
            assert(calls.length === 1, "visible transition should poll immediately")

            listeners.visibilitychange()
            await flush()

            assert(intervals.length === 1, "repeated visible event must not duplicate timer")

            visibilityState = "hidden"
            listeners.visibilitychange()
            await flush()

            assert(cleared.length === 1, "hidden transition should clear the timer")

            visibilityState = "visible"
            listeners.visibilitychange()
            await flush()

            assert(intervals.length === 2, "resume should start a fresh timer")
            assert(calls.length === 2, "resume should poll immediately")
            assert(
              calls[1].options.headers["If-None-Match"] === '"build-a"',
              "resume poll should preserve the accepted ETag",
            )
            """
        ),
        encoding="utf-8",
    )

    subprocess.run([node, str(harness)], check=True)


def test_build_id_caddy_recipe_uses_short_revalidating_cache() -> None:
    """Docs should keep Caddy on a short build-id cache with ETag revalidation."""
    text = _read(README)

    assert 'header @build_id Cache-Control "max-age=2, must-revalidate"' in text
    assert "If-None-Match" in text
    assert "304 Not Modified" in text
