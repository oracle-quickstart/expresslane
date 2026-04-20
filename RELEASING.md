# Cutting an ExpressLane Release

This is the maintainer playbook for publishing a new ExpressLane release to [`oracle-quickstart/expresslane`](https://github.com/oracle-quickstart/expresslane). It is intentionally a punch-list — no prose.

**Audience:** project maintainers with write access to the GitHub repo. End users should read [UPDATING.md](./UPDATING.md) instead.

## Prerequisites

- `git` with push access to `oracle-quickstart/expresslane`.
- [`gh` CLI](https://cli.github.com/) installed and authenticated (`gh auth status`).
- Clean working tree on `main` (no uncommitted changes, no untracked files).

## Release Flow

### 1. Bump the version

Edit `version.py` to the new release number:

```python
__version__ = "1.3.0"
```

Bump follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`. Breaking changes bump MAJOR, new features bump MINOR, bug-fix-only bumps PATCH.

### 2. Update the CHANGELOG

Add a new top entry to `CHANGELOG.md` under the existing header. Follow the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format with `### Added`, `### Changed`, `### Fixed`, `### Security`, and/or `### Database` subsections as appropriate. Date the release with today's date in `YYYY-MM-DD` format.

### 3. Commit and tag

```bash
git add version.py CHANGELOG.md
git commit -m "Release v1.3.0"
git tag -a v1.3.0 -m "ExpressLane v1.3.0"
git push origin main
git push origin v1.3.0
```

Tag names use the `vX.Y.Z` convention — the leading `v` matters because `gh release create` and `git archive` both use the tag name verbatim.

### 4. Build the release zip

Use `git archive` to produce a clean zip from the tag. The `.gitattributes` file in the repo root marks dev-only paths (`tests/`, `.github/`, `.gitattributes`, `.gitignore`) with `export-ignore` so they don't end up in the archive:

```bash
git archive --format=zip --prefix=expresslane/ -o expresslane.zip v1.3.0
```

The `--prefix=expresslane/` is critical — it means users who `unzip expresslane.zip` get a `expresslane/` directory, matching what the install docs assume.

Verify the zip looks right before publishing:

```bash
unzip -l expresslane.zip | head -30
```

You should see files under `expresslane/` (app.py, Dockerfile, docker-compose.yml, deploy/, templates/, static/, screenshots/, etc.) and **no** `tests/`, `.github/`, or `.git*` entries.

### 5. Publish the GitHub release

```bash
gh release create v1.3.0 expresslane.zip \
    --title "ExpressLane v1.3.0" \
    --notes-file CHANGELOG.md
```

If you want a draft first (to review the release page before making it public):

```bash
gh release create v1.3.0 expresslane.zip \
    --title "ExpressLane v1.3.0" \
    --notes-file CHANGELOG.md \
    --draft
```

Then review at `https://github.com/oracle-quickstart/expresslane/releases` and click **Publish release** when satisfied.

> **Note:** `--notes-file CHANGELOG.md` pastes the **entire** changelog as release notes. If you only want this release's section as notes, create a temporary `release-notes.md` with just the new entry and pass that file instead.

### 6. Verify the latest URL works

The `curl` commands in `README.md` and `UPDATING.md` point at the **latest-release** URL pattern, which auto-follows to the most recent published release:

```
https://github.com/oracle-quickstart/expresslane/releases/latest/download/expresslane.zip
```

After publishing, confirm it resolves:

```bash
curl -LI https://github.com/oracle-quickstart/expresslane/releases/latest/download/expresslane.zip \
    | grep -iE "^(location|HTTP)"
```

You should see the redirect chain ending at the new version's zip asset. If it still points at the previous release, the new release hasn't propagated yet — wait a minute and retry.

### 7. Clean up local artifacts

```bash
rm expresslane.zip
```

The release zip lives on GitHub now; you don't need a local copy.

## Rolling Back a Release

If a release is broken and needs to be pulled:

```bash
# Delete the GitHub release (keeps the tag)
gh release delete v1.3.0

# Delete the tag locally and on the remote
git tag -d v1.3.0
git push origin :refs/tags/v1.3.0
```

After deletion, the `releases/latest` URL automatically falls back to the previous release. Users who already downloaded the broken version will need to be told to re-pull.

**Alternative: mark as pre-release instead of deleting.** This keeps the version available for anyone who needs to reproduce the bug, but removes it from `releases/latest`:

```bash
gh release edit v1.3.0 --prerelease
```

## Version Pinning

Users who need a specific version (for reproducibility or compliance) can pin to the versioned URL instead of the latest URL:

```
https://github.com/oracle-quickstart/expresslane/releases/download/v1.3.0/expresslane.zip
```

Every published release is available at this URL pattern indefinitely, so old versions don't rot.

## Future — Automate with GitHub Actions

The manual flow above can be automated with a GitHub Actions workflow that runs on tag push. A minimal version:

```yaml
# .github/workflows/release.yml
name: Release
on:
  push:
    tags:
      - 'v*.*.*'
jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - name: Build release zip
        run: git archive --format=zip --prefix=expresslane/ -o expresslane.zip ${{ github.ref_name }}
      - name: Publish release
        uses: softprops/action-gh-release@v2
        with:
          files: expresslane.zip
          body_path: CHANGELOG.md
```

Not enabled today — the manual `gh release create` flow is good enough for v1.x cadence. Add the workflow when releases become frequent enough that manual work is a bottleneck.

---

*ExpressLane — Release Playbook*
*Copyright (c) 2026 Oracle and/or its affiliates. Released under UPL-1.0.*
