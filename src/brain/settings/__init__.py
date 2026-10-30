"""Feature settings leaves: where new knobs live, so ``config.py`` does not grow (RM-08).

``config.py`` is over the 800-line ceiling, and every knob added there costs a
two-place growth record (``CLAUDE.md``, "File-size ceiling"). So a knob added
from roadmap ticket RM-08 on lives in a leaf here, one module per feature
(``settings/rerank.py``, ``settings/brief_delivery.py`` and so on), and
``config.py`` is not edited for it (roadmap spec §2.3, decision G3). Existing
knobs stay where they are.

**A leaf is a frozen dataclass plus one loader**::

    @dataclass(frozen=True)
    class RerankSettings:
        backend: str
        depth: int

    def load_rerank_settings(env: Mapping[str, str]) -> RerankSettings:
        return RerankSettings(
            backend=env_choice(env, "BRAIN_RERANKER", BACKENDS, "none"),
            depth=env_int(env, "BRAIN_RERANK_DEPTH", 20, minimum=2, maximum=50),
        )

The parsers are in :mod:`brain.settings._env`. Each one treats an unset or
blank variable as its default, and refuses a bad value with a
``brain.config.ConfigError`` that names the variable and never quotes the value.

**Where ``env`` comes from.** A caller passes ``os.environ`` **after**
``Config.load()`` (or ``load_minimal()``) has run. That call publishes the
dotenv chain into the process environment (``Config._load_field_dict``, the
``os.environ.setdefault`` loop), so a leaf knob is read from the same ``.env``
files as every other knob, with the process environment still winning. Tests
pass a plain ``dict``. There is one fallback to ``os.environ``:
``eval.runner.run_eval(stages=None, env=None)`` reads it when neither argument
is given (§2.3's carve-out, RM-14a), and every test of ``run_eval`` passes
``stages`` or ``env`` explicitly (``tests/test_eval_runner_stages.py`` guards
that).

**Rules a leaf keeps**, each enforced by ``tests/test_settings_leaf_guards.py``
for every module in this package:

- A leaf never imports ``os``, ``posix``, ``nt`` or ``dotenv`` and never names
  ``environ`` / ``getenv`` (it uses ``pathlib`` for paths); it reads the ``env``
  it was given.
- A field that holds a secret (an API token, a webhook URL) is declared
  ``field(repr=False)``, and its name ends in ``_token``, ``_secret``,
  ``_password``, ``_api_key``, ``_secret_key``, ``_private_key`` or
  ``_webhook_url`` (in any case), which is how the guard finds it.
  ``brain doctor`` reports only whether a secret is set.
- ``brain.config`` never imports this package (it would be a cycle: the parsers
  raise ``brain.config.ConfigError``).
"""
