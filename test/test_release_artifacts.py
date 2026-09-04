# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import smoke_release_services
from scripts.verify_release_artifacts import (
    ReleaseValidationError,
    expected_tag,
    project_identity,
    select_compatible_wheel,
    validate_readme_citation,
    validate_readme_mcp_ownership,
    validate_release,
    validate_release_notes,
    validate_tag,
)

_PACKAGED_README = """# CodeNib

<!-- mcp-name: ai.codenib/codenib -->

## Citation

https://arxiv.org/abs/2607.25431

@misc{yu2026codenibmultiviewdataserving,
"""


def _write_project_release_contract(root: Path) -> Path:
    project = root / "pyproject.toml"
    project.write_text(
        '[project]\nname = "codenib"\nversion = "0.2.1"\n',
        encoding="utf-8",
    )
    notes = root / "docs" / "releases"
    notes.mkdir(parents=True)
    (notes / "0.2.1.md").write_text(
        "# CodeNib 0.2.1\n\nInstall with `codenib==0.2.1`.\n",
        encoding="utf-8",
    )
    return project


def _write_test_wheel(path: Path, *, native_member: str) -> None:
    metadata = f"""Metadata-Version: 2.4
Name: codenib
Version: 0.2.1
License-Expression: Apache-2.0
Description-Content-Type: text/markdown

{_PACKAGED_README}
"""
    members = {
        "codenib/__init__.py": "",
        "codenib/__main__.py": "",
        "codenib/agent/tools/sandbox.py": "",
        "codenib/cli.py": "",
        "codenib/sandbox/__init__.py": "",
        "codenib/sandbox/docker.py": "",
        "codenib/sandbox/protocol.py": "",
        "codenib/sandbox/types.py": "",
        "codenib/storage.py": "",
        "codenib/toolchains.py": "",
        "codenib/web/frontend/index.html": "",
        "codenib/web/frontend/codenib-icon.svg": "",
        "codenib/web/frontend/runtime-config.js": "",
        "codenib/web/frontend/assets/index.js": "",
        native_member: "native",
        "codenib-0.2.1.dist-info/METADATA": metadata,
        "codenib-0.2.1.dist-info/entry_points.txt": (
            "[console_scripts]\ncodenib = codenib.cli:main\n"
        ),
    }
    with zipfile.ZipFile(path, mode="w") as archive:
        for member, contents in members.items():
            archive.writestr(member, contents)


def _write_test_sdist(
    path: Path,
    *,
    include_native_source: bool = True,
    binary_member: str | None = None,
    extra_member: str | None = None,
) -> None:
    root = "codenib-0.2.1"
    members = {
        f"{root}/CHANGELOG.md": "",
        f"{root}/LICENSE": "",
        f"{root}/README.md": _PACKAGED_README,
        f"{root}/codenib/storage.py": "",
        f"{root}/server.json": "{}",
        f"{root}/pyproject.toml": "[project]\n",
        f"{root}/web/package-lock.json": "{}",
    }
    if include_native_source:
        members[f"{root}/native/workspace_owner.c"] = "/* source */\n"
    if binary_member is not None:
        members[f"{root}/{binary_member}"] = "native"
    if extra_member is not None:
        members[f"{root}/{extra_member}"] = "retired"
    with tarfile.open(path, mode="w:gz") as archive:
        for member, contents in members.items():
            payload = contents.encode("utf-8")
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_test_release(
    root: Path,
    *,
    x86_native: str = "codenib/_workspace_owner_impl.abi3.so",
    arm_native: str = "codenib/_workspace_owner_impl.abi3.so",
    include_native_source: bool = True,
    sdist_binary: str | None = None,
    sdist_extra_member: str | None = None,
) -> tuple[Path, Path]:
    project = _write_project_release_contract(root)
    dist = root / "dist"
    dist.mkdir()
    _write_test_wheel(
        dist / "codenib-0.2.1-cp310-abi3-manylinux_2_28_x86_64.whl",
        native_member=x86_native,
    )
    _write_test_wheel(
        dist / "codenib-0.2.1-cp310-abi3-manylinux_2_28_aarch64.whl",
        native_member=arm_native,
    )
    _write_test_sdist(
        dist / "codenib-0.2.1.tar.gz",
        include_native_source=include_native_source,
        binary_member=sdist_binary,
        extra_member=sdist_extra_member,
    )
    return project, dist


def test_release_service_smoke_accepts_authenticated_source_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calculator.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commands: list[tuple[str, ...]] = []

    def record_git(command, *, cwd, env=None):
        assert cwd == repo
        git_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(smoke_release_services, "_run", record_git)
    monkeypatch.setattr(
        smoke_release_services.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            2,
            "",
            "error: repository source content does not match the manifest\n",
        ),
    )

    smoke_release_services._assert_stale_snapshot_rejected(
        tmp_path,
        repo,
        executable="codenib",
        env={},
    )

    assert git_commands == [
        ("git", "add", "calculator.py"),
        ("git", "commit", "--quiet", "-m", "advance fixture"),
    ]


def test_project_identity_and_tag_match_release_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    name, version = project_identity(root / "pyproject.toml")

    assert name == "codenib"
    assert expected_tag(version) == "v0.2.3"
    validate_tag("v0.2.3", version)


def test_release_tag_must_match_project_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match"):
        validate_tag("v0.1.0", "0.2.0")


def test_curated_release_notes_match_the_project_version(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "codenib"\nversion = "0.2.1"\n',
        encoding="utf-8",
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes = notes_dir / "0.2.1.md"
    notes.write_text(
        "# CodeNib 0.2.1\n\nInstall with `codenib[graph,mcp]==0.2.1`.\n",
        encoding="utf-8",
    )

    assert validate_release_notes(project, notes_dir) == notes


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ("# CodeNib 0.2.0\n\ncodenib==0.2.1\n", "version heading"),
        ("# CodeNib 0.2.1\n\ncodenib\n", "installation example"),
        (
            "# CodeNib 0.2.1\n\ncodenib==0.2.1 --extra-index-url x\n",
            "prerelease installation markers",
        ),
    ),
)
def test_curated_release_notes_reject_invalid_contract(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "codenib"\nversion = "0.2.1"\n',
        encoding="utf-8",
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "0.2.1.md").write_text(body, encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match=message):
        validate_release_notes(project, notes_dir)


def test_packaged_readme_requires_the_arxiv_citation() -> None:
    citation = """
## Citation
https://arxiv.org/abs/2607.25431
@misc{yu2026codenibmultiviewdataserving,
"""
    validate_readme_citation(citation)

    with pytest.raises(ReleaseValidationError, match="citation markers"):
        validate_readme_citation("# CodeNib\n")


def test_packaged_readme_requires_mcp_registry_ownership() -> None:
    validate_readme_mcp_ownership("<!-- mcp-name: ai.codenib/codenib -->")

    with pytest.raises(ReleaseValidationError, match="MCP ownership marker"):
        validate_readme_mcp_ownership("# CodeNib\n")


def test_release_requires_both_native_abi3_manylinux_wheels(tmp_path: Path) -> None:
    project, dist = _write_test_release(tmp_path)

    wheels, sdist = validate_release(dist, project_file=project)

    assert {wheel.name for wheel in wheels} == {
        "codenib-0.2.1-cp310-abi3-manylinux_2_28_x86_64.whl",
        "codenib-0.2.1-cp310-abi3-manylinux_2_28_aarch64.whl",
    }
    assert sdist.name == "codenib-0.2.1.tar.gz"

    wheels[0].unlink()
    with pytest.raises(
        ReleaseValidationError, match="production wheels must be exactly"
    ):
        validate_release(dist, project_file=project)

    _write_test_wheel(wheels[0], native_member="codenib/_workspace_owner_impl.abi3.so")
    (dist / "codenib-0.2.1-py3-none-any.whl").touch()
    with pytest.raises(ReleaseValidationError, match="unexpected .*py3-none-any"):
        validate_release(dist, project_file=project)


def test_release_wheel_requires_exact_native_extension_name(tmp_path: Path) -> None:
    project, dist = _write_test_release(
        tmp_path,
        x86_native="codenib/_workspace_owner_impl.cpython-310-x86_64-linux-gnu.so",
    )

    with pytest.raises(
        ReleaseValidationError,
        match="must contain exactly one codenib/_workspace_owner_impl.abi3.so",
    ):
        validate_release(dist, project_file=project)

    x86_wheel = dist / "codenib-0.2.1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_wheel(x86_wheel, native_member="codenib/_workspace_owner_impl.abi3.so")
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(x86_wheel, mode="a") as archive:
            archive.writestr("codenib/_workspace_owner_impl.abi3.so", "duplicate")
    with pytest.raises(ReleaseValidationError, match="must contain exactly one"):
        validate_release(dist, project_file=project)


def test_release_wheel_rejects_retired_storage_runtime(tmp_path: Path) -> None:
    project, dist = _write_test_release(tmp_path)
    wheel = dist / "codenib-0.2.1-cp310-abi3-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(wheel, mode="a") as archive:
        archive.writestr("codenib/storage/models.py", "retired")

    with pytest.raises(ReleaseValidationError, match="retired storage runtime"):
        validate_release(dist, project_file=project)


def test_release_wheel_rejects_experimental_index_persistence(
    tmp_path: Path,
) -> None:
    project, dist = _write_test_release(tmp_path)
    wheel = dist / "codenib-0.2.1-cp310-abi3-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(wheel, mode="a") as archive:
        archive.writestr("codenib/index/persistence/catalog.py", "experimental")

    with pytest.raises(ReleaseValidationError, match="retired storage runtime"):
        validate_release(dist, project_file=project)


def test_release_sdist_rejects_retired_compiler_bridge(tmp_path: Path) -> None:
    project, dist = _write_test_release(
        tmp_path,
        sdist_extra_member="codenib/compiler/cache_import.py",
    )

    with pytest.raises(ReleaseValidationError, match="retired storage runtime"):
        validate_release(dist, project_file=project)


@pytest.mark.parametrize(
    ("include_native_source", "sdist_binary", "message"),
    (
        (False, None, "native/workspace_owner.c"),
        (True, "codenib/_workspace_owner_impl.abi3.so", "compiled binaries"),
    ),
)
def test_release_sdist_is_source_only(
    tmp_path: Path,
    include_native_source: bool,
    sdist_binary: str | None,
    message: str,
) -> None:
    project, dist = _write_test_release(
        tmp_path,
        include_native_source=include_native_source,
        sdist_binary=sdist_binary,
    )

    with pytest.raises(ReleaseValidationError, match=message):
        validate_release(dist, project_file=project)


@pytest.mark.parametrize(
    ("machine", "architecture"),
    (("x86_64", "x86_64"), ("AMD64", "x86_64"), ("aarch64", "aarch64")),
)
def test_select_compatible_wheel_uses_current_architecture(
    tmp_path: Path,
    machine: str,
    architecture: str,
) -> None:
    _, dist = _write_test_release(tmp_path)

    selected = select_compatible_wheel(dist, system="linux", machine=machine)

    assert selected.name.endswith(f"manylinux_2_28_{architecture}.whl")


def test_select_compatible_wheel_rejects_unsupported_platform(tmp_path: Path) -> None:
    _, dist = _write_test_release(tmp_path)

    with pytest.raises(ReleaseValidationError, match="require Linux"):
        select_compatible_wheel(dist, system="darwin", machine="arm64")
    with pytest.raises(ReleaseValidationError, match="no production wheel"):
        select_compatible_wheel(dist, system="linux", machine="ppc64le")


def test_stable_release_notes_describe_the_wiki_storage_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    notes = (root / "docs" / "releases" / "0.2.3.md").read_text(encoding="utf-8")

    assert '"codenib[graph,mcp]==0.2.3"' in notes
    assert "single-file" in notes
    assert "`codenib.storage`" in notes
    assert "`WikiStore`" in notes
    assert "`SQLiteWikiStore`" in notes
    assert "manifest-bound file artifacts" in notes
    assert "`artifact materialize`" in notes
    assert "pinned CodeNib 0.2.2" in notes
    assert "`SQLiteCatalog`" in notes
    assert "`LocalCAS`" in notes
    assert "test-files.pythonhosted.org" not in notes
    assert "--extra-index-url" not in notes
    assert "--index-url" not in notes


def test_020_release_notes_retain_pages_permission_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    notes = (root / "docs" / "releases" / "0.2.0.md").read_text(encoding="utf-8")

    assert '"codenib[semantic]==0.2.0"' in notes
    assert '"codenib[mcp,semantic]==0.2.0"' in notes
    assert "sysevol-ai/CodeNib/.github/workflows/codenib-pages.yml@v0.2.0" in notes
    for permission in ("contents: read", "pages: write", "id-token: write"):
        assert permission in notes


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/agent_integrations.md",
        "docs/codegraph.md",
        "docs/index.md",
        "docs/mcp.md",
        "docs/quickstart.md",
        "docs/scip_index.md",
        "docs/web_demo.md",
    ),
)
def test_public_install_commands_select_the_current_stable_release(
    relative_path: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / relative_path).read_text(encoding="utf-8")
    install_lines = [
        line.strip()
        for line in text.splitlines()
        if line.lstrip().startswith(("pip install ", "python -m pip install "))
        and "codenib" in line
        and " -e " not in line
    ]

    assert install_lines
    assert "CODENIB_ALPHA_WHEEL=" not in text
    assert all("==0.2.3" in line for line in install_lines)


def test_readme_install_commands_remain_unpinned() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "README.md").read_text(encoding="utf-8")
    install_lines = [
        line.strip()
        for line in text.splitlines()
        if line.lstrip().startswith(("pip install ", "python -m pip install "))
        and "codenib" in line
        and " -e " not in line
    ]

    assert install_lines
    assert "CODENIB_ALPHA_WHEEL=" not in text
    assert all("==" not in line for line in install_lines)


def test_registry_publishers_use_separate_workflows() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"

    def load(name: str) -> dict[str, object]:
        with (workflows / name).open(encoding="utf-8") as handle:
            return yaml.load(handle, Loader=yaml.BaseLoader)

    def publisher_step(job: dict[str, object]) -> dict[str, object]:
        return next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
        )

    production = load("release.yml")
    test = load("release-test.yml")
    verification = load("release-verify.yml")

    production_verification_job = {
        "name": "Verify release artifacts",
        "uses": "./.github/workflows/release-verify.yml",
        "with": {"full": "${{ github.event_name != 'pull_request' }}"},
        "permissions": {"contents": "read"},
    }
    test_verification_job = {
        "name": "Verify release artifacts",
        "uses": "./.github/workflows/release-verify.yml",
        "permissions": {"contents": "read"},
    }
    assert production["jobs"]["verify"] == production_verification_job
    assert test["jobs"]["verify"] == test_verification_job
    assert production["concurrency"]["cancel-in-progress"] == (
        "${{ github.ref_type != 'tag' }}"
    )
    assert "docs/releases/**" in production["on"]["pull_request"]["paths"]
    assert "native/**" in production["on"]["pull_request"]["paths"]

    release_tag_push = (
        "github.event_name == 'push' && github.ref_type == 'tag' && "
        "startsWith(github.ref_name, 'v')"
    )

    assert set(production["jobs"]) == {
        "verify",
        "registry-auth-preflight",
        "publish-pypi",
        "verify-pypi-install",
        "publish-mcp-registry",
        "github-release",
    }
    registry_preflight = production["jobs"]["registry-auth-preflight"]
    assert registry_preflight["if"] == (
        "github.ref_type == 'tag' && startsWith(github.ref_name, 'v')"
    )
    assert registry_preflight["needs"] == "verify"
    assert registry_preflight["runs-on"] == "ubuntu-latest"
    assert registry_preflight["environment"]["name"] == "mcp-registry-publish"
    preflight_steps = {step["name"]: step for step in registry_preflight["steps"]}
    preflight_download = preflight_steps["Install verified MCP publisher"]["run"]
    assert "sha256sum --check" in preflight_download
    assert "--connect-timeout 10 --max-time 90" in preflight_download
    assert "login dns" in preflight_steps["Verify branded namespace ownership"]["run"]
    production_publisher = production["jobs"]["publish-pypi"]
    assert production_publisher["if"] == release_tag_push
    assert production_publisher["needs"] == "registry-auth-preflight"
    assert production_publisher["environment"]["name"] == "pypi"
    assert production_publisher["permissions"] == {"id-token": "write"}
    assert "password" not in publisher_step(production_publisher).get("with", {})
    assert "PYPI_API_TOKEN" not in str(production_publisher)
    pypi_install = production["jobs"]["verify-pypi-install"]
    assert pypi_install["if"] == release_tag_push
    assert pypi_install["needs"] == "publish-pypi"
    assert pypi_install["runs-on"] == "ubuntu-latest"
    pypi_install_steps = {step["name"]: step for step in pypi_install["steps"]}
    public_download = pypi_install_steps["Download and match the published wheel"][
        "run"
    ]
    assert '"codenib==$VERSION"' in public_download
    assert "sha256sum" in public_download
    assert public_download.count("--print-compatible-wheel") == 2
    assert (
        "scripts/smoke_release_services.py"
        in pypi_install_steps["Exercise public Wiki and MCP services"]["run"]
    )
    registry_publisher = production["jobs"]["publish-mcp-registry"]
    assert registry_publisher["if"] == release_tag_push
    assert registry_publisher["needs"] == "verify-pypi-install"
    assert registry_publisher["runs-on"] == "ubuntu-latest"
    assert registry_publisher["environment"]["name"] == "mcp-registry-publish"
    assert registry_publisher["permissions"] == {"contents": "read"}
    registry_steps = {step["name"]: step for step in registry_publisher["steps"]}
    assert "--check-pypi" in registry_steps["Wait for the exact PyPI package"]["run"]
    assert (
        "sha256sum --check" in registry_steps["Install verified MCP publisher"]["run"]
    )
    assert (
        "--connect-timeout 10 --max-time 90"
        in registry_steps["Install verified MCP publisher"]["run"]
    )
    assert registry_steps["Authenticate branded namespace"]["env"] == {
        "MCP_PRIVATE_KEY": "${{ secrets.MCP_PRIVATE_KEY }}"
    }
    assert "--check-registry" in registry_steps["Verify Registry discovery"]["run"]
    assert production["jobs"]["github-release"]["needs"] == [
        "publish-pypi",
        "publish-mcp-registry",
    ]
    assert production["jobs"]["github-release"]["if"] == release_tag_push
    release_steps = {
        step["name"]: step for step in production["jobs"]["github-release"]["steps"]
    }
    assert release_steps["Resolve release channel"]["id"] == "channel"
    resolve_release = release_steps["Resolve release channel"]["run"]
    assert 'RELEASE_NOTES="docs/releases/$VERSION.md"' in resolve_release
    assert 'test -f "$RELEASE_NOTES"' in resolve_release
    assert release_steps["Create release"]["env"]["RELEASE_NOTES"] == (
        "${{ steps.channel.outputs.notes_file }}"
    )
    create_release = release_steps["Create release"]["run"]
    assert "RELEASE_FLAGS=(--latest)" in create_release
    assert "RELEASE_FLAGS=(--prerelease)" in create_release
    assert '--notes-file "$RELEASE_NOTES"' in create_release
    assert "--generate-notes" not in create_release
    assert '"${RELEASE_FLAGS[@]}"' in create_release

    assert set(test["jobs"]) == {
        "verify",
        "publish-testpypi",
        "verify-testpypi-install",
    }
    test_publisher = test["jobs"]["publish-testpypi"]
    assert test_publisher["environment"]["name"] == "testpypi"
    assert (
        publisher_step(test_publisher)["with"]["repository-url"]
        == "https://test.pypi.org/legacy/"
    )
    test_install = test["jobs"]["verify-testpypi-install"]
    assert test_install["needs"] == "publish-testpypi"
    assert test_install["runs-on"] == "ubuntu-latest"
    assert test_install["permissions"] == {"contents": "read"}
    step_names = [step["name"] for step in test_install["steps"]]
    assert step_names.index("Resolve release version") < step_names.index(
        "Download and verify the TestPyPI wheel"
    )
    assert step_names.index("Download and verify the TestPyPI wheel") < (
        step_names.index("Install the registry wheel")
    )
    install_steps = {step["name"]: step for step in test_install["steps"]}
    assert install_steps["Resolve release version"]["id"] == "version"
    download = install_steps["Download and verify the TestPyPI wheel"]["run"]
    assert "--index-url https://test.pypi.org/simple/" in download
    assert "--no-deps" in download
    assert "hashlib.sha256" in download
    assert "select_compatible_wheel" in download
    exercise = install_steps["Exercise the installed CLI"]["run"]
    assert "doctor --require core --require wiki" in exercise
    assert "scripts/smoke_release_install.py" in exercise

    assert set(verification["on"]) == {"workflow_call"}
    assert verification["on"]["workflow_call"]["inputs"]["full"] == {
        "description": "Run the cross-version and installed-service matrix.",
        "type": "boolean",
        "default": "true",
    }
    assert set(verification["jobs"]) == {
        "build-sdist",
        "build-wheel",
        "limited-abi-compile",
        "build",
        "source-smoke",
        "abi3-smoke",
        "install-smoke",
        "upgrade-smoke",
        "service-smoke",
        "agent-smoke",
        "graph-smoke",
    }
    sdist_job = verification["jobs"]["build-sdist"]
    sdist_steps = {step["name"]: step for step in sdist_job["steps"]}
    assert "--sdist" in sdist_steps["Build source distribution"]["run"]
    assert sdist_steps["Upload source distribution"]["with"]["path"] == (
        "dist/*.tar.gz"
    )

    wheel_job = verification["jobs"]["build-wheel"]
    assert wheel_job["needs"] == "build-sdist"
    assert wheel_job["strategy"]["matrix"]["arch"] == ["x86_64", "aarch64"]
    wheel_steps = {step["name"]: step for step in wheel_job["steps"]}
    qemu = wheel_steps["Set up QEMU for aarch64"]
    assert qemu["if"] == "matrix.arch == 'aarch64'"
    assert qemu["uses"] == (
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8"
    )
    wheel_build = wheel_steps["Build and smoke-test wheel"]
    assert wheel_build["uses"] == (
        "pypa/cibuildwheel@4726cd35bb13f7bde50cf2761f2499ac7b3aa32c"
    )
    wheel_env = wheel_build["env"]
    assert wheel_env["CIBW_BUILD"] == "cp310-manylinux_${{ matrix.arch }}"
    assert wheel_env["CIBW_ARCHS_LINUX"] == "${{ matrix.arch }}"
    assert "--cap-add SYS_PTRACE" in wheel_env["CIBW_CONTAINER_ENGINE"]
    assert wheel_env["CIBW_MANYLINUX_X86_64_IMAGE"] == "manylinux_2_28"
    assert wheel_env["CIBW_MANYLINUX_AARCH64_IMAGE"] == "manylinux_2_28"
    assert (
        "manylinux_2_28_${{ matrix.arch }}"
        in wheel_env["CIBW_REPAIR_WHEEL_COMMAND_LINUX"]
    )
    wheel_smoke = wheel_env["CIBW_TEST_COMMAND"]
    assert "workspace_owner_protocol_version == 6" in wheel_smoke
    assert "directory_fd_owner_protocol_version == 1" in wheel_smoke
    assert "directory_fd_owner_supported == 1" in wheel_smoke
    for symbol in (
        "create_directory_fd_owner_exact",
        "open_directory_fd_exact",
        "borrow_directory_fd_exact",
        "mkdir_directory_fd_child_exact",
        "close_directory_fd_owner_exact",
        "directory_fd_owner_closed_exact",
        "fail_fork_child_if_directory_fd_owner_active_exact",
    ):
        assert symbol in wheel_smoke
    for symbol in (
        "provision_owner_interruptibly_exact",
        "capture_owner_destination_exact",
        "acquire_owner_replacement_lease_exact",
        "verify_owner_destination_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_destination_descriptor_exact",
        "claim_owner_replacement_permit_exact",
        "provision_owner_replacement_exact",
        "provision_owner_replacement_interruptibly_exact",
        "verify_owner_replacement_binding_exact",
        "exchange_owner_replacement_exact",
        "seal_owner_directories_interruptibly_exact",
    ):
        assert symbol in wheel_smoke
    for symbol in (
        "acquire_owner_replacement_lease",
        "borrow_owner_parent_descriptor",
        "claim_owner_replacement_permit",
        "provision_owner_replacement",
        "verify_owner_replacement_binding",
        "exchange_owner_replacement",
        "_bind_owner_replacement_permit",
    ):
        assert symbol in wheel_smoke
    assert "destination-leased" in wheel_smoke
    assert wheel_smoke.index(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < wheel_smoke.index("_workspace_owner.claim_owner_replacement_permit(")
    assert wheel_smoke.index(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < wheel_smoke.index("_workspace_owner.provision_owner_replacement(")
    assert "not os.get_inheritable(parent_descriptor)" in wheel_smoke
    assert "implementation.require_support() is None" in wheel_smoke
    assert "_workspace_owner.require_support()" in wheel_smoke
    assert "_workspace_owner._directory_fd_owner_protocol_available" in wheel_smoke
    assert "_workspace_owner._require_directory_fd_owner_support()" in wheel_smoke
    for call in (
        "_workspace_owner._create_directory_fd_owner()",
        "_workspace_owner._open_directory_fd(",
        "_workspace_owner._borrow_directory_fd(",
        "_workspace_owner._mkdir_directory_fd_child(",
        "_workspace_owner._close_directory_fd_owner(",
        "_workspace_owner._directory_fd_owner_closed(",
    ):
        assert call in wheel_smoke
    assert "stat.S_ISDIR(os.fstat(directory_descriptor).st_mode)" in wheel_smoke
    assert "not os.get_inheritable(directory_descriptor)" in wheel_smoke
    assert '(lease_directory / "child").rmdir()' in wheel_smoke
    assert "lease_directory.rmdir()" in wheel_smoke

    limited_abi_job = verification["jobs"]["limited-abi-compile"]
    assert "if" not in limited_abi_job
    limited_abi_steps = {step["name"]: step for step in limited_abi_job["steps"]}
    limited_abi_compile = limited_abi_steps[
        "Compile the native extension against the limited API"
    ]["run"]
    assert "-DPy_LIMITED_API=0x030A0000" in limited_abi_compile
    assert "-Wall" in limited_abi_compile
    assert "-Wextra" in limited_abi_compile
    assert "-Werror" in limited_abi_compile
    assert "-c native/workspace_owner.c" in limited_abi_compile

    build_job = verification["jobs"]["build"]
    assert build_job["needs"] == [
        "build-sdist",
        "build-wheel",
        "limited-abi-compile",
    ]
    verification_steps = {step["name"]: step for step in build_job["steps"]}
    inspect_distributions = verification_steps["Inspect Python distributions"]["run"]
    assert "scripts/verify_release_artifacts.py dist" in inspect_distributions
    assert '"jsonschema>=4,<5"' in inspect_distributions
    assert verification_steps["Upload distributions"]["with"]["path"] == (
        "dist/*.whl\ndist/*.tar.gz\n"
    )
    assert (
        "--connect-timeout 10 --max-time 90"
        in verification_steps["Validate MCP Registry metadata"]["run"]
    )

    source_job = verification["jobs"]["source-smoke"]
    assert "if" not in source_job
    source_steps = {step["name"]: step for step in source_job["steps"]}
    source_install = source_steps["Install source distribution without a compiler"]
    assert source_install["env"] == {"CC": "/bin/false"}
    assert "--no-cache-dir" in source_install["run"]
    assert "dist/*.tar.gz" in source_install["run"]
    source_exercise = source_steps["Exercise core and fail-closed workspace support"][
        "run"
    ]
    assert "doctor --require core" in source_exercise
    assert "scripts/smoke_release_install.py" in source_exercise
    assert 'find_spec("codenib._workspace_owner_impl")' in source_exercise
    assert "_workspace_owner.require_support()" in source_exercise
    assert "_workspace_owner._require_directory_fd_owner_support()" in source_exercise
    assert "LocalWorkspaceProvider(Path(root))" in source_exercise
    assert "provider.require_support()" in source_exercise
    assert "UnsupportedWorkspaceCreation" in source_exercise

    abi3_job = verification["jobs"]["abi3-smoke"]
    assert "if" not in abi3_job
    assert abi3_job["strategy"]["matrix"]["python-version"] == [
        "3.10",
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    ]
    abi3_steps = {step["name"]: step for step in abi3_job["steps"]}
    abi3_install = abi3_steps["Install the compatible ABI3 wheel"]["run"]
    assert "--print-compatible-wheel" in abi3_install
    assert "--no-deps" in abi3_install
    abi3_exercise = abi3_steps["Exercise native ABI and local workspace lifecycle"][
        "run"
    ]
    assert 'name == "_workspace_owner_impl.abi3.so"' in abi3_exercise
    assert "workspace_owner_protocol_version == 6" in abi3_exercise
    assert "directory_fd_owner_protocol_version == 1" in abi3_exercise
    assert "directory_fd_owner_supported == 1" in abi3_exercise
    for symbol in (
        "create_directory_fd_owner_exact",
        "open_directory_fd_exact",
        "borrow_directory_fd_exact",
        "mkdir_directory_fd_child_exact",
        "close_directory_fd_owner_exact",
        "directory_fd_owner_closed_exact",
        "fail_fork_child_if_directory_fd_owner_active_exact",
    ):
        assert symbol in abi3_exercise
    for symbol in (
        "provision_owner_interruptibly_exact",
        "capture_owner_destination_exact",
        "acquire_owner_replacement_lease_exact",
        "verify_owner_destination_binding_exact",
        "borrow_owner_parent_descriptor_exact",
        "borrow_owner_destination_descriptor_exact",
        "claim_owner_replacement_permit_exact",
        "provision_owner_replacement_exact",
        "provision_owner_replacement_interruptibly_exact",
        "verify_owner_replacement_binding_exact",
        "exchange_owner_replacement_exact",
        "seal_owner_directories_interruptibly_exact",
    ):
        assert symbol in abi3_exercise
    assert "implementation.require_support() is None" in abi3_exercise
    assert "_workspace_owner.require_support() is None" in abi3_exercise
    assert "_workspace_owner._directory_fd_owner_protocol_available" in abi3_exercise
    assert "_workspace_owner._require_directory_fd_owner_support()" in abi3_exercise
    for call in (
        "_workspace_owner._create_directory_fd_owner()",
        "_workspace_owner._open_directory_fd(",
        "_workspace_owner._borrow_directory_fd(",
        "_workspace_owner._mkdir_directory_fd_child(",
        "_workspace_owner._close_directory_fd_owner(",
        "_workspace_owner._directory_fd_owner_closed(",
    ):
        assert call in abi3_exercise
    assert "stat.S_ISDIR(os.fstat(directory_descriptor).st_mode)" in abi3_exercise
    assert "not os.get_inheritable(directory_descriptor)" in abi3_exercise
    assert '(lease_directory / "child").rmdir()' in abi3_exercise
    assert "lease_directory.rmdir()" in abi3_exercise
    assert "LocalWorkspaceProvider(root)" in abi3_exercise
    assert "run_strict_workspace(" in abi3_exercise
    assert "root.mkdir(mode=0o700)" in abi3_exercise
    assert "session.write_file" in abi3_exercise
    assert "session.publish_validated" in abi3_exercise
    assert "receipt_owner.active" in abi3_exercise
    assert "receipt_owner.close()" in abi3_exercise
    assert "receipt_owner.closed" in abi3_exercise
    assert "_workspace_owner.capture_owner_destination(" in abi3_exercise
    assert "destination-captured" in abi3_exercise
    assert abi3_exercise.count("_workspace_owner.acquire_owner_replacement_lease(") == 2
    assert "destination-leased" in abi3_exercise
    assert "_workspace_owner.borrow_owner_parent_descriptor(" in abi3_exercise
    assert "_workspace_owner.verify_owner_destination_binding(" in abi3_exercise
    assert "_workspace_owner.borrow_owner_destination_descriptor(" in abi3_exercise
    assert "stat.S_ISDIR(descriptor_metadata.st_mode)" in abi3_exercise
    assert "not os.get_inheritable(destination_descriptor)" in abi3_exercise
    assert "not os.get_inheritable(parent_descriptor)" in abi3_exercise
    assert abi3_exercise.index(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < abi3_exercise.index("_workspace_owner.claim_owner_replacement_permit(")
    assert abi3_exercise.index(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < abi3_exercise.index("_workspace_owner.provision_owner_replacement(")
    assert abi3_exercise.rindex(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < abi3_exercise.rindex("_workspace_owner.claim_owner_replacement_permit(")
    assert abi3_exercise.rindex(
        "_workspace_owner.acquire_owner_replacement_lease("
    ) < abi3_exercise.rindex("_workspace_owner.provision_owner_replacement(")
    assert "_workspace_owner.claim_owner_replacement_permit(" in abi3_exercise
    assert "_workspace_owner.provision_owner_replacement(" in abi3_exercise
    assert "replacement-provisioned" in abi3_exercise
    assert "_workspace_owner.verify_owner_adoption_binding(" in abi3_exercise
    assert "_workspace_owner.verify_owner_replacement_binding(" in abi3_exercise
    assert "_workspace_owner.borrow_owner_root_descriptor(" in abi3_exercise
    assert "not os.get_inheritable(replacement_descriptor)" in abi3_exercise
    assert "_workspace_owner.mark_owner_adopted(" in abi3_exercise
    assert "replacement-adopted" in abi3_exercise
    assert "_workspace_owner.begin_owner_file(" in abi3_exercise
    assert "_workspace_owner.write_owner_file(" in abi3_exercise
    assert "_workspace_owner.finish_owner_file(" in abi3_exercise
    assert "_workspace_owner.seal_owner_directories(" in abi3_exercise
    assert "_workspace_owner.exchange_owner_replacement(" in abi3_exercise
    assert "replacement-exchanged-unreceipted" in abi3_exercise
    assert abi3_exercise.count("_workspace_owner.commit_owner_receipt(") == 2
    assert "replacement-receipted" in abi3_exercise
    assert "os.close(destination_descriptor)" not in abi3_exercise
    assert "os.close(replacement_descriptor)" not in abi3_exercise
    assert "_workspace_owner.close_owner_exact(captured_owner)" in abi3_exercise
    assert "_workspace_owner.owner_closed(captured_owner)" in abi3_exercise
    assert 'owner_state(captured_owner) == "closed"' in abi3_exercise
    assert "assert output.read_bytes() == replacement_payload" in abi3_exercise
    assert "replacement.joinpath" in abi3_exercise
    assert abi3_exercise.count("_workspace_owner.exchange_owner_replacement(") == 2
    assert "_workspace_owner.abort_owner(rollback_owner)" in abi3_exercise
    assert 'owner_state(rollback_owner) == "closed"' in abi3_exercise
    assert "rollback_incumbent_identity" in abi3_exercise
    assert "rollback_identity" in abi3_exercise
    assert "rollback_payload" in abi3_exercise

    compatible_install_steps = {
        "abi3-smoke": "Install the compatible ABI3 wheel",
        "install-smoke": "Install wheel",
        "upgrade-smoke": "Exercise the supported upgrade boundary",
        "service-smoke": "Install wheel with MCP support",
        "agent-smoke": "Install wheel with agent support",
        "graph-smoke": "Install wheel, Python graph runtime, and MCP",
    }
    for job_name, step_name in compatible_install_steps.items():
        steps = {step["name"]: step for step in verification["jobs"][job_name]["steps"]}
        assert "--print-compatible-wheel" in steps[step_name]["run"]
    for job_name in (
        "install-smoke",
        "upgrade-smoke",
        "service-smoke",
        "agent-smoke",
        "graph-smoke",
    ):
        assert verification["jobs"][job_name]["if"] == "inputs.full"
    assert not any(name.startswith("publish-") for name in verification["jobs"])

    for workflow in (production, test, verification):
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is None or action.startswith("./"):
                    continue
                revision = action.rpartition("@")[2]
                assert len(revision) == 40
                assert all(character in "0123456789abcdef" for character in revision)


def test_both_release_wheel_architectures_run_protocol_v6_exchange_smoke() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / ".github/workflows/release-verify.yml").open(
        encoding="utf-8"
    ) as handle:
        verification = yaml.load(handle, Loader=yaml.BaseLoader)

    wheel_job = verification["jobs"]["build-wheel"]
    architectures = wheel_job["strategy"]["matrix"]["arch"]
    wheel_step = next(
        step
        for step in wheel_job["steps"]
        if step["name"] == "Build and smoke-test wheel"
    )
    smoke_template = wheel_step["env"]["CIBW_TEST_COMMAND"]
    expanded_smokes = {
        architecture: smoke_template.replace("${{ matrix.arch }}", architecture)
        for architecture in architectures
    }

    assert set(expanded_smokes) == {"x86_64", "aarch64"}
    for architecture, smoke in expanded_smokes.items():
        assert f"platform.machine() == {architecture!r}" in smoke
        for operation in (
            "capture_owner_destination(",
            "acquire_owner_replacement_lease(",
            "verify_owner_destination_binding(",
            "borrow_owner_parent_descriptor(",
            "claim_owner_replacement_permit(",
            "provision_owner_replacement(",
            "verify_owner_adoption_binding(",
            "borrow_owner_root_descriptor(",
            "mark_owner_adopted(",
            "begin_owner_file(",
            "write_owner_file(",
            "finish_owner_file(",
            "seal_owner_directories(",
            "verify_owner_replacement_binding(",
        ):
            assert f"_workspace_owner.{operation}" in smoke
        assert smoke.index(
            "_workspace_owner.acquire_owner_replacement_lease("
        ) < smoke.index("_workspace_owner.claim_owner_replacement_permit(")
        assert smoke.index(
            "_workspace_owner.acquire_owner_replacement_lease("
        ) < smoke.index("_workspace_owner.provision_owner_replacement(")
        assert "destination-leased" in smoke
        assert "not os.get_inheritable(parent_descriptor)" in smoke
        assert smoke.count("_workspace_owner.exchange_owner_replacement(") == 2
        assert smoke.count("_workspace_owner.commit_owner_receipt(") == 2
        assert "success_committed = True" in smoke
        assert "replacement-exchanged-unreceipted" in smoke
        assert "replacement-receipted" in smoke
        assert smoke.count('owner_state(success_owner) == "closed"') == 1
        assert smoke.count('owner_state(rollback_owner) == "closed"') == 1
        assert "_workspace_owner.abort_owner(rollback_owner)" in smoke
        assert "rollback_incumbent_identity" in smoke
        assert "rollback_candidate_identity" in smoke
        assert 'rollback_destination / "incumbent.txt"' in smoke
        assert 'rollback_slot / "data/result.json"' in smoke
        assert "os.close(" not in smoke


def test_protocol_v6_workspace_replacement_limits_are_documented() -> None:
    root = Path(__file__).resolve().parents[1]
    provider = (root / "docs/development/local-workspace-provider.md").read_text(
        encoding="utf-8"
    )
    provider_prose = " ".join(provider.split())

    # Keep the protocol's safety contract next to its compatibility
    # implementation. The subtraction roadmap records lifecycle and removal
    # decisions instead of duplicating this low-level recovery specification.
    for text in (provider,):
        prose = " ".join(text.split())
        assert "protocol v4" in text.lower()
        assert "protocol v5" in text.lower()
        assert "protocol v6" in text.lower()
        assert "renameat2(RENAME_EXCHANGE)" in text
        assert "flock(LOCK_EX | LOCK_NB)" in text
        assert "open-file-description (OFD)" in text
        assert "replacement-exchanged-unreceipted" in text
        assert "replacement-receipted" in text
        assert "replacement-recovery-required" in text
        assert "SIGKILL" in text
        assert "process exit" in prose
        assert "same exact" in prose
        assert "unconsumed" in prose
        assert "reclassification" in prose
        assert "before a token is returned" in prose
        assert "no token" in prose
        assert "operator-restored" in prose
        assert "before eventual unlock" in prose
        assert "after close" in prose
        assert "no longer a retry capability" in prose
        assert "single-writer" in prose
        assert "stale" in prose
        assert "auto-adopt" in prose
        assert "does not generate" in prose
        assert "randomness" in prose
        assert "destination-leased" in text
        assert "before candidate mutation" in prose
        assert "zero candidate mutation" in prose
        assert "cross-process" in prose
        assert "fork" in prose
        assert "receipted" in prose
        assert "live-name verifier" in prose
        assert "hostile same-UID" in text
        assert "crash journal" in prose

    assert "There is no portable or multi-rename fallback" in provider
    assert "this is live atomicity, not crash recovery" in provider_prose
    assert "borrowers must never close them" in provider_prose
    for symbol in (
        "acquire_owner_replacement_lease_exact",
        "claim_owner_replacement_permit_exact",
        "provision_owner_replacement_exact",
        "verify_owner_replacement_binding_exact",
        "exchange_owner_replacement_exact",
        "WorkspaceReplacementPermit",
        "WorkspaceReceiptToken",
    ):
        assert symbol in provider
    assert "historical Gate C implementation is complete" in provider_prose
    assert "frozen compatibility seam" in provider_prose
    assert "has no remaining promotion gate" in provider_prose


def test_product_storage_scope_and_removal_policy_are_documented() -> None:
    root = Path(__file__).resolve().parents[1]
    provider = (root / "docs/development/local-workspace-provider.md").read_text(
        encoding="utf-8"
    )
    roadmap = (root / "docs/storage_backend_roadmap.md").read_text(encoding="utf-8")
    roadmap_prose = " ".join(roadmap.split())

    assert (
        "CodeNib has one public product database boundary: "
        "`codenib.storage.WikiStore`"
    ) in roadmap_prose
    assert (
        "BM25, FAISS, igraph, and portable context payloads remain file artifacts"
    ) in roadmap_prose
    assert (
        "No generic catalog, CAS, or database compatibility layer remains"
    ) in roadmap_prose
    assert "## Closed Plans" in roadmap
    assert "CodeNib no longer plans PostgreSQL, S3-compatible object storage" in (
        roadmap_prose
    )
    provider_prose = " ".join(provider.split())
    assert "a filesystem publication boundary, not a database" in provider_prose
    assert "v0.2.2 environment before upgrading" in roadmap_prose
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["codenib*"]' in project
    assert (root / "scripts/experimental/hybrid_index").is_dir()
    assert not (root / "codenib/index/persistence").exists()
