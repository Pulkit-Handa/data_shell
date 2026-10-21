# Publishing Data Shell to PyPI

This document is the release procedure: what gets published, under which
name, how the upload is authorised, and how to verify that what was
published is what was intended.

---

## 1. The three names

Data Shell has three names, and only one of them is a distribution name.
They are deliberately distinct, and the release gate
(`scripts/check_release_metadata.py`) fails if they drift apart.

| Name | Value | Where it appears |
|---|---|---|
| PyPI distribution | `datashell-engine` | `pip install datashell-engine` |
| Import package | `datashell` | `import datashell` |
| Console script | `datashell` | `datashell run`, `datashell doctor` |
| Product name | Data Shell | Documentation, banners, `/health` |

**Why the distribution is not called `datashell`.** The `datashell` name on
PyPI belongs to an unrelated project (a pandas shell tool). A PyPI project
name is globally unique and can never be released twice, so this project
cannot be published under the name its import package uses. Renaming the
*distribution* is the only part of the interface that had to change: the
import package and the console script keep their names, so **no operator
command and no line of user code changes**. A user who upgrades from a
source checkout installs the distribution once and then uses
`import datashell` and `datashell <command>` exactly as before.

The Airflow provider is a second, independent distribution:
`datashell-airflow` (import package `datashell_airflow`).

---

## 2. One-time setup: register the publisher

Publishing uses **PyPI Trusted Publishing (OIDC)**: PyPI verifies the
identity of the GitHub Actions workflow with a short-lived OIDC token, so
**no API token is stored in this repository** and no secret has to be
rotated. The cost is a one-time registration per PyPI project.

For each PyPI project (`datashell-engine` and `datashell-airflow`):

1. Create the project on PyPI if it does not exist yet
   (project name, a summary, and a description; the description is
   replaced by the README when the first release is uploaded).
2. On the project page, open **Manage → Publishing → Add a new pending
   publisher**.
3. Fill in:

   | Field | Value |
   |---|---|
   | PyPI project name | `datashell-engine` (resp. `datashell-airflow`) |
   | Owner | `Pulkit-Handa` |
   | Repository name | `data_shell` |
   | Workflow name | `publish-pypi.yml` |
   | Environment name | `pypi` |

4. Create the GitHub environment **pypi** in the repository settings
   (Settings → Environments → New environment). The environment is what
   the pending publisher is bound to; the workflow requests it as
   `environment: pypi`.

The workflow file path is `publish-pypi.yml`; PyPI matches the *file name*,
not the full path.

---

## 3. Cutting a release

1. **Make the tree releasable.** The version must be identical in every
   copy the gate checks:

   | Copy | File |
   |---|---|
   | Distribution version | `pyproject.toml` (`[project] version`) |
   | Import package version | `datashell/__init__.py` (`__version__`) |
   | Demo application version | `web/main.py` (the FastAPI `version=`) |
   | Provider distribution version | `integrations/airflow/pyproject.toml` |
   | Provider import package version | `integrations/airflow/datashell_airflow/__init__.py` |
   | Changelog | `CHANGELOG.md` (newest `## [x.y.z]` entry) |

   Run the gate — it is the same command CI and the release workflow run:

   ```bash
   python scripts/check_release_metadata.py
   python scripts/check_release_metadata.py --tag v2.0.1
   ```

2. **Regenerate the committed OpenAPI document** if the API surface
   changed (`python scripts/export_openapi.py --out docs/openapi.yaml`);
   CI fails if it is out of sync.

3. **Build and inspect the artifacts locally** (the same gates the
   workflow runs):

   ```bash
   python -m build --no-isolation --outdir dist
   python scripts/verify_distribution_contents.py --dist-dir dist

   (cd integrations/airflow && python -m build --no-isolation --outdir dist)
   python scripts/verify_provider_distribution_contents.py --dist-dir integrations/airflow/dist
   ```

4. **Tag the release.** The tag must be `v<version>`:

   ```bash
   git tag -a v2.0.1 -m "Data Shell 2.0.1"
   git push origin v2.0.1
   ```

   The tag triggers three workflows:

   | Workflow | What it does |
   |---|---|
   | `ci.yml` | the full test matrix, static gates, container build |
   | `release.yml` | builds both distributions and creates the GitHub Release with the artifacts and the SBOM |
   | `publish-pypi.yml` | verifies the tag, rebuilds both distributions, and uploads them to PyPI with Trusted Publishing |

5. **Verify the publication.** After the workflow completes:

   ```bash
   pip download datashell-engine==2.0.1 --no-deps -d /tmp/ds
   pip install /tmp/ds/datashell_engine-2.0.1-py3-none-any.whl
   datashell --version
   python -c "import datashell; print(datashell.__version__)"
   ```

   The installed distribution must report `datashell-engine`, and the
   import package and the CLI must both be `datashell`.

### Dry run

`publish-pypi.yml` can be started manually (**Actions → Publish to PyPI →
Run workflow**). The `dry_run` input is **on by default**: the workflow
builds and verifies both distributions and stops before the upload. Turn it
off to publish from a branch — do that only for a version that has not
been published yet, because PyPI rejects a second upload of the same
version.

---

## 4. What the publish workflow does and does not do

**Does:**

- build with the pinned build backend (`requirements-build.lock`), never
  with an unpinned one;
- run `scripts/check_release_metadata.py --tag <tag>`, so a tag that does
  not match the package version fails the job before anything is uploaded;
- run the distribution-content gates on the exact artifacts it uploads;
- upload with Trusted Publishing — no API token in the repository, no
  long-lived credential to leak or rotate.

**Does not:**

- bump versions, cut tags, or create the GitHub Release — those are
  deliberate human actions (section 3);
- re-publish a version that already exists on PyPI. PyPI rejects it, and
  that rejection is intentional: a published version is immutable.

---

## 5. Recovery: a version that must not exist on PyPI

PyPI does not allow a version to be deleted and re-uploaded, and a
re-upload under the same name is rejected. If a broken version was
published:

1. **Do not** try to overwrite it. Yanking it
   (project → Manage → Releases → Yank) hides it from the resolver while
   keeping it installable by exact version, which is the right move for a
   broken release.
2. Publish the fix as the **next** version (`2.0.2`), with the changelog
   entry describing the defect.
3. If the yank is not enough, the project name itself is the last resort:
   a new name means a new project, and the old one can be abandoned. The
   distribution name is declared in exactly one place (`pyproject.toml`),
   and the release gate reads it from there, so a rename touches the
   packaging metadata, the two gates and the documentation — not the code.

---

## 6. Related documents

| Document | Relevance |
|---|---|
| [`production/RELEASE_CHECKLIST.md`](production/RELEASE_CHECKLIST.md) | The sign-off sheet, including the publisher-registration gate |
| [`production/RELEASE_VALIDATION_PACKAGE.md`](production/RELEASE_VALIDATION_PACKAGE.md) | The authoritative gate matrix (row F10 covers publication) |
| [`../CHANGELOG.md`](../CHANGELOG.md) | What each published version contains |
| [`../.github/workflows/publish-pypi.yml`](../.github/workflows/publish-pypi.yml) | The workflow this document describes |
