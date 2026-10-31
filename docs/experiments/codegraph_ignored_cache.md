# Retain queries after ignored cache generation

Creating an ignored cache must not invalidate an unchanged selected source
view. The repair in [#949](https://github.com/sysevol-ai/CodeNib/pull/949)
keeps the existing MCP connection, definition worker
and index after Python creates `pkg/__pycache__`; a subsequent selected-source
edit still rejects the query before another definition request and reaps the
worker. This is a functional repair, not evidence of comparative task benefit.

## Source boundary

The existing scanner already excludes cache paths before retaining entries.
Its inventory nevertheless included the full version of their visible parent
directory. Creating an excluded child changes that directory's timestamps and
link count, so an existing binding rejected a source view whose fingerprint
and all 24 selected files were unchanged.

Across scans, bind directory object identity, permissions and Windows deletion
state. Continue recording every selected child and strict file/link versions.
Full directory versions still bracket each scan, rejecting concurrent mutation;
root, containment, symlink and final publication checks are unchanged. The
existing authentication lease and lock order remain in force. There is no new
persistence, source exclusion, saved fingerprint version or graph schema.

## Verification and limits

Six corrected Linux/Windows-fake cases fail on unchanged production code and
pass after the repair. Two additional cases retain rejection when even an
ignored child changes during a scan. The complete source-binding suite passes
234 cases, with five platform skips. Windows uses deterministic fake handles;
this is not a native Windows installed-service run.

The same installed MCP driver runs against the admitted combined CI wheel and
a developer wheel differing in exactly `source_fingerprint.py` and wheel
`RECORD`. The former reproduces the stale-source error; the latter passes cache
creation, same-worker reuse and genuine-edit rejection. Both workers exit
normally and are independently absent. The developer overlay is retained as
that local control, separately from the actual CI build.

The [unmodified CI admission](codegraph_ignored_cache_ci_2026_10_31.json)
closes the installed-build gate. Wheel `53b58f6b` from source head `89424a80`
matches all 486 packaged Python files and the worker asset to the reviewed
source; every product byte equals the developer admission. Only
`source_fingerprint.py` differs from the previous admitted CI product. The same
unchanged MCP driver passes all three phases on this actual wheel, with its
worker normally reaped and independently absent. All 20 executed source-head
PR checks pass with eight conditional/publication skips. Later documentation
reconciliation retains these exact product bytes; final review and check status
is tracked in the PR.

The first POSIX test attempt used the wrong fingerprint attribute; its failure
is retained separately from the six corrected regressions. The first full
unit run also retains its failure: a directory-capture test expects mode 0644
but local umask 0002 creates 0664. That exact failure reproduces on unchanged
main; the candidate passes with umask 0022. The complete corrected-environment
unit run passes 8,957 cases with 51 skips, 212 marker deselections and 46
warnings. It is recorded separately rather than replacing the failed attempt.

The [exact evidence packet](codegraph_ignored_cache_2026_10_31.json) retains the
driver, software/source bindings, original replies and unsuccessful attempts.
No model question runs, previous cohorts stay frozen, independent successes
remain 0/2 and external enrollment remains 0/5. Complete workflow benefit and
public release are still open.
