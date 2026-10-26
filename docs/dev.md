# Developing nestaudiocast

The rules of the project (notes/consensus.md, docs/v020-contracts.md) are enforced by tools
wherever a tool can do it. This page says which, and how to run them.

## Environment

[uv](https://docs.astral.sh/uv/) manages `.venv` from `uv.lock`:

```sh
uv sync                 # runtime + dev tools (pytest, ruff, pre-commit), exactly as locked
uv run pre-commit install
```

- Runtime dependencies are in `pyproject.toml` (`[project].dependencies`); dev tools are in the
  `dev` group and never go to CT 121.
- `requirements.txt` is **generated** from `uv.lock` by a hook (`uv export`), with hashes. It
  exists for CT 121, which installs with `pip install -r requirements.txt`. Do not edit it.
- Adding a dependency: `uv add <pkg>` (or `uv add --dev <pkg>`), commit `pyproject.toml`,
  `uv.lock` and the regenerated `requirements.txt` together.

## Tests and safety tiers

```sh
./tests/v020/run.sh                     # = .venv/bin/pytest
./tests/v020/run.sh test_sync -k drift  # one module, pytest arguments
```

Every test is in exactly one tier (`tests/v020/conftest.py`):

| tier | marker | what it may do | runs when |
|---|---|---|---|
| offline | none (default) | loopback only; **any socket to another address raises** | always |
| lan | `@pytest.mark.lan` | read-only traffic to LAN devices | `NAC_ALLOW_LAN=1` |
| device | `@pytest.mark.device` | launch an app on the Chromecast | `NAC_ALLOW_DEVICE=1` |
| audible | `@pytest.mark.audible` | make sound in the house | `NAC_ALLOW_SOUND=1` **and** inside the window where sound may start (`QUIET_START..QUIET_END`, contracts §7) |

CI sets none of these, so CI only ever runs the offline tier. A hardware test must carry its
marker; an unmarked test that opens a LAN socket fails with `OfflineViolation`.
`tests/v020/test_safety_tiers.py` checks that these rules hold.

The suite is written with `unittest`, and `python -m unittest discover -s tests/v020` still
works, but it has no tiers and no network guard: use pytest.

## Hooks

`.pre-commit-config.yaml`, installed by `uv run pre-commit install`:

| stage | hook | why |
|---|---|---|
| commit | ruff check + ruff format (Python only) | one style; real errors (undefined names, unused results) caught before review |
| commit | uv-lock, uv-export | `uv.lock` and `requirements.txt` never drift from `pyproject.toml` |
| commit | gitleaks, detect-private-key | the repository is public |
| commit | `tools/safety_guards.sh` | mute-never-zero, startup unmute, relay yield (A23, A36, A37, §7) |
| push | the offline suite | nothing leaves this host red |

`uv run pre-commit run --all-files` runs them all; CI does exactly that, then pytest.

v0.1 code (`lib/castv2.py`, `lib/silence.py`, `lib/emergency.py`, `lib/groupguard.py`,
`relay/`, the v0.1 `probe/`, `measure/` and `tools/` scripts) is linted for real errors but
never reformatted: the lab notes cite it line by line. The list is in `pyproject.toml`.

## tools/prove.sh

Still the single command that replays every proof, but **it is not offline**: §3 and §4 read
the speakers and the groups on the LAN (read-only). §5 is `tools/safety_guards.sh`, and
`prove.sh` now exits non-zero when it fails (it used to print the regression and exit 0).
