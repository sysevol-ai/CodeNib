# Evaluation Artifact Bundles

`codeminer-artifact-bundle` stages content-locked evaluation releases from
explicit local sources. It is intended for retained experiment records that
are too large or too environment-specific to keep in Git.

The bundle manifest contains only named roots and relative paths. Machine paths
are supplied at runtime with repeated `--root NAME=PATH` arguments. This keeps
storage layout out of the release contract while making the selected files
reviewable.

## Manifest

```json
{
  "schema_version": 1,
  "bundle": {
    "name": "example evaluation",
    "version": "v1"
  },
  "sources": [
    {
      "id": "reports",
      "root": "results",
      "path": "batch-v1",
      "destination": "inputs/reports",
      "include": ["*.json", "*.log"],
      "exclude": ["debug-*.log"],
      "exclude_dir_names": ["cache", "__pycache__"]
    },
    {
      "id": "readme",
      "root": "release",
      "path": "README.md",
      "destination": "README.md"
    }
  ]
}
```

A directory source preserves paths below `destination`. A file source is copied
to the exact destination path. The builder rejects absolute paths, `..`,
overlapping destinations, symbolic links, non-regular files, empty selections,
and outputs nested inside selected sources.

## Freeze Inputs

Create a source lock after the experiment inputs are final:

```bash
codeminer-artifact-bundle lock \
  --manifest bundle-manifest.json \
  --root results=/path/to/results \
  --root release=/path/to/release-files \
  --output source-lock.json
```

The lock stores a path-sensitive SHA-256 identity plus file and byte counts for
every source. Commit the manifest and reviewed lock, not the bound local paths.
Any later input change causes a locked build to fail.

## Build And Verify

```bash
codeminer-artifact-bundle build \
  --manifest bundle-manifest.json \
  --source-lock source-lock.json \
  --root results=/path/to/results \
  --root release=/path/to/release-files \
  --output /path/to/staged-bundle

codeminer-artifact-bundle verify --bundle /path/to/staged-bundle
```

The builder writes to a temporary sibling, copies each selected file while
checking for concurrent changes, generates `BUNDLE_MANIFEST.json`,
`SOURCE_LOCK.json`, `PROVENANCE.json`, and a complete `SHA256SUMS`, verifies the
payload, then atomically renames it into place. Verification rejects both
missing files and unlisted additions.

Create a portable, deterministic archive only after all release-specific
checks pass:

```bash
codeminer-artifact-bundle archive \
  --bundle /path/to/staged-bundle \
  --output /path/to/staged-bundle.tar.gz
```

The archive normalizes timestamps and ownership, uses a single top-level
directory, and writes a sibling `.sha256` file. Generated analyses should be
written outside the staged bundle unless they were selected by the manifest;
otherwise the complete-inventory verifier will reject the modified copy.
