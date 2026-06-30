# Publishing the split packages — `puffo-agent-core` & `puffo-agent-cloud`

This repo is a `uv` workspace with three members (root `pyproject.toml`,
`[tool.uv.workspace]`):

| Member             | Lives in                     | Deps                          |
| ------------------ | ---------------------------- | ----------------------------- |
| `puffo-agent`      | repo root (`src/puffo_agent`) | the fat local daemon (everything) |
| `puffo-agent-core` | `packages/puffo-agent-core`  | **stdlib only**               |
| `puffo-agent-cloud`| `packages/puffo-agent-cloud` | `aiohttp`, `pyyaml`, `puffo-agent-core` |

Dependency edge is acyclic: **core ← cloud ← fat**. All three are at
`version = "1.0.3"` today.

The fat `puffo-agent` keeps its existing workflows
(`.github/workflows/publish-pypi.yml`, `publish-testpypi.yml`) — **unchanged**.
The two split packages get a new, separate workflow:
`.github/workflows/publish-cloud-pkgs.yml`.

---

## ✅ What this PR gives us (no external setup required)

- **A publish workflow** — `publish-cloud-pkgs.yml` fires on every GitHub
  Release (`release: published`) and on demand (`workflow_dispatch`).
- **Standalone wheels + sdists** for both packages, built with `uv`:
  - `uv build --wheel --sdist --package puffo-agent-core` →
    `puffo_agent_core-1.0.3-py3-none-any.whl` (+ `puffo_agent_core-1.0.3.tar.gz`)
  - `uv build --wheel --sdist --package puffo-agent-cloud` →
    `puffo_agent_cloud-1.0.3-py3-none-any.whl` (+ `puffo_agent_cloud-1.0.3.tar.gz`)
  These are normal PEP-427 wheels — no workspace tooling needed to install them.
- **GitHub-Release assets as the ZERO-SETUP install path.** The workflow
  attaches both wheels + sdists to the Release using only the built-in
  `GITHUB_TOKEN`. No PyPI account, no project, no secret. Install with either:

  ```sh
  # direct asset URL
  pip install \
    https://github.com/puffo-ai/puffo-agent/releases/download/v1.0.3/puffo_agent_cloud-1.0.3-py3-none-any.whl

  # or let pip resolve from the release's asset listing (gets core too)
  pip install puffo-agent-cloud puffo-agent-core \
    --find-links https://github.com/puffo-ai/puffo-agent/releases/expanded_assets/v1.0.3
  ```

- **Per-package isolation for Trusted Publishing.** The PyPI jobs use two
  distinct GitHub Environments (`pypi-core`, `pypi-cloud`) and each uploads only
  its own package's `dist/` dir — so each PyPI project's OIDC binding only ever
  sees its own files.

---

## ⛔ What is still externally gated ("还差什么")

These cannot be done from this PR — they need a PyPI account and/or repo-admin.
Until they exist, the PyPI jobs are **skipped, not failed** (gated on the repo
variable `PUBLISH_PYPI`), so a release is green with only the zero-setup
Release-asset path active.

### (a) PyPI projects + Trusted Publishers + GitHub Environments + the gate flag

For **each** of `puffo-agent-core` and `puffo-agent-cloud`:

1. On **pypi.org** → Account settings → Publishing → *Add a new pending
   publisher*:
   - PyPI project name: `puffo-agent-core` (resp. `puffo-agent-cloud`)
   - Owner: `puffo-ai`
   - Repository name: `puffo-agent`
   - Workflow filename: `publish-cloud-pkgs.yml`
   - Environment name: `pypi-core` (resp. `pypi-cloud`)
2. In the repo → Settings → Environments → create `pypi-core` and `pypi-cloud`
   (recommended: add a *Required reviewers* rule — a PyPI version can never be
   re-uploaded once taken, so the approval is the last cheap safety gate).
3. In the repo → Settings → Secrets and variables → Actions → **Variables** →
   set `PUBLISH_PYPI = true`. This flips the `pypi-core` / `pypi-cloud` jobs on.

   *(Optional TestPyPI dry-run: repeat step 1 on **test.pypi.org** with
   environments `testpypi-core` / `testpypi-cloud` and add matching jobs — not
   wired up in this PR to keep the diff minimal.)*

The existing OIDC binding only covers `puffo-agent` → environment `pypi`. It
does **not** extend to the two new project names; each needs its own pending
publisher.

### (b) #105 (`fleet/puffo-agent-thin-refactor`) merged to mainline

`packages/` only exists after the Stage-A "uv workspace + slim
puffo-agent-cloud" commit. A real release must fire from a branch where
`packages/` is present — i.e. #105 must land on the line releases are cut from
before the publish actually produces these packages.

### (c) Alternative: an internal registry instead of PyPI

If we'd rather not put these on public PyPI, swap the publish target: point the
publish step at an internal index (registry URL + credentials, e.g. via a
`UV_PUBLISH_URL` / token secret or `pypa/gh-action-pypi-publish`'s
`repository-url`). That registry URL + credentials must exist first; it's a
drop-in replacement for the PyPI path, the Release-asset path is unaffected.

---

## ⚠️ Two verified metadata gaps to fix before any *real* PyPI publish

These don't block this PR (nothing is published here) but they will bite on the
first real upload, so they're called out explicitly.

### 1. The cloud wheel's dependency on core is **unpinned**

`packages/puffo-agent-cloud/pyproject.toml` declares
`dependencies = [... "puffo-agent-core"]` plus
`[tool.uv.sources] puffo-agent-core = { workspace = true }`. The
`{ workspace = true }` source is a **uv-local** concept — it is *stripped* from
the standard wheel metadata. Verified:

```sh
$ unzip -p puffo_agent_cloud-1.0.3-py3-none-any.whl '*/METADATA' | grep Requires-Dist
Requires-Dist: aiohttp>=3.9
Requires-Dist: pyyaml>=6.0
Requires-Dist: puffo-agent-core          # <-- NO version constraint
```

So once published, `pip install puffo-agent-cloud` pulls **whatever
`puffo-agent-core` is latest on PyPI**, not the matching `1.0.3`.
**Recommendation:** pin it (`puffo-agent-core==1.0.3`, or `~=1.0.0` for
patch-compatible) in `packages/puffo-agent-cloud/pyproject.toml` before the
first real publish. (Left as a follow-up — no `pyproject.toml` is edited in this
PR.)

### 2. The fat `puffo-agent` becomes **uninstallable from PyPI** until core+cloud exist there

The workspace-root `pyproject.toml` now lists `puffo-agent-core` and
`puffo-agent-cloud` as ordinary (also unpinned) dependencies. Verified the fat
wheel carries:

```
Requires-Dist: puffo-agent-core
Requires-Dist: puffo-agent-cloud
```

A `pip install puffo-agent` from PyPI resolves those two names against PyPI —
which currently have **no project** — so the install fails. **Consequence: gap
(a) — creating the PyPI projects + publishers — is required for the *fat*
package too, not just the new split ones.** Publish core + cloud (or at least
create the projects) before the next `puffo-agent` PyPI release, or the existing
release pipeline breaks.

---

## Versioning recommendation — adopt **independent** versions

Core / cloud / fat all share `1.0.3` today. **Recommendation: give the split
packages independent version numbers going forward**, so a core-only fix doesn't
force a cloud release and vice-versa, and neither drags the fat package's version
along. This is informational only — versions are **not** changed in this PR:

- `1.0.3` is unreleased for `puffo-agent-core` / `puffo-agent-cloud`, so the
  first publish is clean as-is; no bump is needed now.
- Forcing a bump now would risk desyncing the fat package's
  `puffo-agent-core` / `puffo-agent-cloud` dependency pins (they're unpinned
  today — see gap #1/#2), so it should land together with the pinning change as a
  deliberate follow-up.

---

## How to cut a release

1. Make sure `packages/` is present on the release branch (gap (b)).
2. Bump `version` in the relevant `pyproject.toml`(s) if needed and tag
   `vX.Y.Z` (the build reads the version from `pyproject.toml`, not the tag —
   the tag is a human label).
3. Publish a **GitHub Release** for that tag. `publish-cloud-pkgs.yml` runs:
   - `build` always builds both packages;
   - `release-assets` always attaches the wheels + sdists to the Release
     (zero-setup install path, works immediately);
   - `pypi-core` / `pypi-cloud` publish to PyPI **only if** `PUBLISH_PYPI=true`
     and the pending publishers/environments exist — otherwise skipped.
4. To publish to PyPI on demand without a release, run the workflow manually
   (Actions → *Publish cloud packages* → *Run workflow*) with `PUBLISH_PYPI=true`.
