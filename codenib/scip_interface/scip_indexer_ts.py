#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for TypeScript and JavaScript projects using scip-typescript.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from ..log_utils import get_logger
from ..paths import PROJECT_INSTALL_SENTINEL
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_ts_indexer")


class SCIPTypeScriptIndexer(SCIPIndexerBase):
    """
    SCIP indexer for TypeScript and JavaScript projects.

    Uses the scip-typescript tool to generate SCIP indices for TypeScript/JavaScript codebases.
    """

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
    ):
        """
        Initialize the TypeScript SCIP indexer.

        Args:
            project_root: Root directory of the TypeScript/JavaScript project
            output_dir: Directory to store output files
                (defaults to ~/.codenib/{project_name})
            exclude_patterns: List of patterns to exclude from indexing
            profiler: Profiler instance for performance tracking
            decoder_backend: ``"serial"`` (default) or ``"core"``.
        """
        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language="typescript",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        """
        Check if scip-typescript is available.

        Returns:
            bool: True if scip-typescript is available, False otherwise
        """
        try:
            result = subprocess.run(
                ["scip-typescript", "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info(f"scip-typescript version: {result.stdout.strip()}")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(
                "scip-typescript not found. Run make scip-typescript-tool and "
                "use make active-scip-env for PATH."
            )
            return False

    def _build_index_command(
        self,
        project_name: Optional[str] = None,
        infer_tsconfig: bool = False,
        yarn_workspaces: bool = False,
        pnpm_workspaces: bool = False,
        npm_workspaces: bool = False,
        patched_tsconfig: Optional[str] = None,
        **kwargs,
    ) -> List[str]:
        """
        Build the command to generate the SCIP index for TypeScript.

        Args:
            project_name: Project name (not used by scip-typescript, kept for compatibility)
            infer_tsconfig: Infer tsconfig for JavaScript projects
                without tsconfig.json and jsconfig.json
            yarn_workspaces: Enable Yarn workspaces support
            pnpm_workspaces: Enable pnpm workspaces support
            npm_workspaces: Enable npm workspaces support
            patched_tsconfig: Path to a patched tsconfig file to use instead of the default
            **kwargs: Additional arguments (ignored)

        Returns:
            List[str]: Command as list of strings
        """
        cmd = ["scip-typescript", "index"]

        # Set working directory
        cmd.extend(["--cwd", str(self.project_root)])

        # Output path
        cmd.extend(["--output", str(self.index_file)])

        # Handle workspace options (mutually exclusive)
        if yarn_workspaces:
            cmd.append("--yarn-workspaces")
        elif pnpm_workspaces:
            cmd.append("--pnpm-workspaces")
        elif npm_workspaces:
            cmd.append("--npm-workspaces")

        # Infer tsconfig for JavaScript projects
        if infer_tsconfig:
            cmd.append("--infer-tsconfig")

        # Use patched tsconfig via positional [projects...] argument
        if patched_tsconfig:
            cmd.append(patched_tsconfig)

        return cmd

    def _get_decoder_class(self):
        """
        Get the decoder class for TypeScript.

        Returns:
            SCIPTypeScriptGraphDecoder class
        """
        from .scip_decode_ts import SCIPTypeScriptGraphDecoder

        return SCIPTypeScriptGraphDecoder

    def _install_dependencies(self, timeout_sec: int = 600) -> None:
        """
        Install Node dependencies in project root before indexing.

        scip-typescript requires project dependencies to be available for
        reliable indexing, especially for JS projects inferred from package.json.
        """
        package_json = self.project_root / "package.json"
        if not package_json.exists():
            return

        # Indexer-only experiment: skip install if lockfile unchanged from
        # the cached prior install. Saves ~30s per cold-start on monorepos
        # like docusaurus where yarn install does a no-op integrity check.
        import hashlib

        sentinel = self.project_root / PROJECT_INSTALL_SENTINEL
        node_modules = self.project_root / "node_modules"
        lockfile = None
        for name in (
            "yarn.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        ):
            if (self.project_root / name).exists():
                lockfile = self.project_root / name
                break
        if lockfile and node_modules.is_dir() and sentinel.exists():
            cur_h = hashlib.sha1(lockfile.read_bytes()).hexdigest()
            try:
                saved_h = sentinel.read_text().strip()
                if cur_h == saved_h:
                    logger.info(
                        "Skipping install: %s unchanged (cached)",
                        lockfile.name,
                    )
                    return
            except Exception:
                pass  # missing/corrupt sentinel; fall through to install

        needs_pnpm = (self.project_root / "pnpm-lock.yaml").exists() or (
            self.project_root / "pnpm-workspace.yaml"
        ).exists()
        if needs_pnpm and not shutil.which("pnpm"):
            logger.info("pnpm needed but not installed; installing via npm...")
            try:
                subprocess.run(
                    ["npm", "install", "-g", "pnpm"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                logger.info("pnpm installed successfully")
            except Exception as e:
                logger.warning("Failed to install pnpm: %s", e)

        has_bun_lock = (self.project_root / "bun.lockb").exists() or (
            self.project_root / "bun.lock"
        ).exists()

        # Determine install command and a fallback without --frozen-lockfile.
        # The fallback is used when the lock file is out of sync with
        # package.json (common for old swebench commits).
        cmd: List[str]
        fallback_cmd: Optional[List[str]] = None

        if has_bun_lock and shutil.which("bun"):
            cmd = ["bun", "install", "--frozen-lockfile"]
            fallback_cmd = ["bun", "install"]
        elif (self.project_root / "yarn.lock").exists() and shutil.which("yarn"):
            cmd = ["yarn", "install", "--frozen-lockfile"]
            fallback_cmd = ["yarn", "install"]
        elif (self.project_root / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
            cmd = ["pnpm", "install", "--frozen-lockfile"]
            fallback_cmd = ["pnpm", "install", "--no-frozen-lockfile"]
        elif (self.project_root / "pnpm-workspace.yaml").exists() and shutil.which(
            "pnpm"
        ):
            cmd = ["pnpm", "install", "--no-frozen-lockfile"]
        elif (self.project_root / "package-lock.json").exists():
            cmd = ["npm", "ci"]
            fallback_cmd = ["npm", "install"]
        else:
            cmd = ["npm", "install"]

        self._run_install(cmd, fallback_cmd, timeout_sec)

    def _run_install(
        self,
        cmd: List[str],
        fallback_cmd: Optional[List[str]],
        timeout_sec: int,
    ) -> None:
        """Execute an install command, retrying with fallback on failure."""
        logger.info("Installing TypeScript dependencies: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            # Cache: write lockfile hash so next call can skip if unchanged
            import hashlib

            sentinel = self.project_root / PROJECT_INSTALL_SENTINEL
            lockfile = None
            for name in (
                "yarn.lock",
                "package-lock.json",
                "pnpm-lock.yaml",
                "bun.lock",
                "bun.lockb",
            ):
                if (self.project_root / name).exists():
                    lockfile = self.project_root / name
                    break
            if lockfile:
                # Best-effort: cache write is non-fatal; next run will retry.
                try:
                    sentinel.write_text(hashlib.sha1(lockfile.read_bytes()).hexdigest())
                except Exception as cache_err:
                    logger.debug("skip-cache write failed (%s); continuing", cache_err)
            return
        except FileNotFoundError:
            logger.warning(
                "Package manager command not found (%s). Continuing without install.",
                cmd[0],
            )
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "Dependency install timed out after %ss. Continuing anyway.",
                timeout_sec,
            )
            return
        except subprocess.CalledProcessError as e:
            logger.warning("Dependency install failed: %s", e)
            if e.stderr:
                logger.warning("install stderr: %s", e.stderr[:500].strip())

        # Retry without --frozen-lockfile if the strict install failed.
        if fallback_cmd is not None:
            logger.info("Retrying without frozen lockfile: %s", " ".join(fallback_cmd))
            try:
                subprocess.run(
                    fallback_cmd,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
            except Exception as e2:
                logger.warning("Fallback install also failed: %s", e2)

    _PATCHED_TSCONFIG_NAME = ".tsconfig.scip.json"

    def _ensure_allow_js(self) -> Optional[Path]:
        """Create a temporary tsconfig with JS and repository-filter settings.

        The patched config is written to ``.tsconfig.scip.json`` in the
        project root — the original ``tsconfig.json`` is never modified.

        If the tsconfig has an ``include`` list with ``*.ts`` globs, also
        adds corresponding ``*.js`` / ``*.jsx`` / ``*.mjs`` globs so that
        JS source files in the same directories are covered.

        Returns the path to the patched tsconfig, or *None* if no patching
        was needed.
        """
        tsconfig_path = self.project_root / "tsconfig.json"
        jsconfig_path = self.project_root / "jsconfig.json"

        # scip-typescript won't recognise jsconfig.json.
        # Read jsconfig.json as a base when tsconfig.json is absent.
        created_from_jsconfig = False
        if not tsconfig_path.exists():
            if jsconfig_path.exists():
                created_from_jsconfig = True
                raw_content = jsconfig_path.read_text(encoding="utf-8")
                logger.info(
                    "No tsconfig.json found; using jsconfig.json as base in %s",
                    self.project_root,
                )
            else:
                return None
        else:
            raw_content = tsconfig_path.read_text(encoding="utf-8")

        try:
            # Strip JS-style comments while preserving string contents.
            # A naive /\*.*?\*/ regex corrupts glob patterns like
            # "src/**/*.ts" by treating /**/ as a comment delimiter.
            # Instead, skip over quoted strings so that only real
            # comments outside of strings are removed.
            stripped = re.sub(
                r'"(?:[^"\\]|\\.)*"|(/\*.*?\*/|//[^\n]*)',
                lambda m: "" if m.group(1) else m.group(0),
                raw_content,
                flags=re.DOTALL,
            )
            stripped = re.sub(r",\s*([}\]])", r"\1", stripped)
            config = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse %s; skipping patch",
                "jsconfig.json" if created_from_jsconfig else "tsconfig.json",
            )
            return None

        needs_patch = False

        # 1. Ensure allowJs is enabled
        compiler_opts = config.setdefault("compilerOptions", {})
        if compiler_opts.get("allowJs") is not True:
            compiler_opts["allowJs"] = True
            needs_patch = True

        # 2. Broaden include: *.ts → also *.js / *.jsx / *.mjs
        include = config.get("include")
        if isinstance(include, list):
            extra = []
            existing = set(include)
            for pat in include:
                if pat.endswith("*.ts"):
                    base = pat[: -len("*.ts")]
                    for ext in ("*.js", "*.jsx", "*.mjs"):
                        candidate = base + ext
                        if candidate not in existing and candidate not in extra:
                            extra.append(candidate)
                elif pat.endswith("*.tsx"):
                    base = pat[: -len("*.tsx")]
                    for ext in ("*.jsx",):
                        candidate = base + ext
                        if candidate not in existing and candidate not in extra:
                            extra.append(candidate)
            if extra:
                config["include"] = include + extra
                needs_patch = True
                logger.info(
                    "Broadened tsconfig include with %d JS patterns (e.g. %s)",
                    len(extra),
                    extra[0],
                )

        # 3. Apply the shared repository policy before scip-typescript scans
        # source. Post-decode filtering keeps graph semantics correct, but it
        # cannot recover time spent indexing generated or vendored trees.
        exclude = config.get("exclude")
        if exclude is None or isinstance(exclude, list):
            existing_excludes = [
                str(pattern)
                for pattern in (exclude or [])
                if isinstance(pattern, str) and pattern.strip()
            ]
            merged_excludes = list(
                dict.fromkeys([*existing_excludes, *self.exclude_patterns])
            )
            if merged_excludes != existing_excludes:
                config["exclude"] = merged_excludes
                needs_patch = True
                logger.info(
                    "Added %d repository exclusion patterns to temporary tsconfig",
                    len(merged_excludes) - len(existing_excludes),
                )

        if not needs_patch and not created_from_jsconfig:
            return None

        patched_path = self.project_root / self._PATCHED_TSCONFIG_NAME
        logger.info(
            "Writing patched tsconfig to %s (allowJs:true, source: %s)",
            patched_path,
            "jsconfig.json" if created_from_jsconfig else "tsconfig.json",
        )
        patched_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return patched_path

    def _cleanup_patched_tsconfig(self) -> None:
        """Remove the temporary patched tsconfig after indexing."""
        patched_path = self.project_root / self._PATCHED_TSCONFIG_NAME
        patched_path.unlink(missing_ok=True)

    def _normalize_workspace_kwargs(self, kwargs: dict) -> dict:
        """
        Ensure workspace flags are consistent with available package managers.
        """
        yarn_available = bool(shutil.which("yarn"))
        pnpm_available = bool(shutil.which("pnpm"))
        npm_available = bool(shutil.which("npm"))

        if kwargs.get("yarn_workspaces") and not yarn_available:
            logger.warning(
                "yarn_workspaces requested but yarn is not installed; disabling."
            )
            kwargs["yarn_workspaces"] = False
            if npm_available:
                kwargs["npm_workspaces"] = True

        if kwargs.get("pnpm_workspaces") and not pnpm_available:
            logger.warning(
                "pnpm_workspaces requested but pnpm is not installed; disabling."
            )
            kwargs["pnpm_workspaces"] = False
            if npm_available:
                kwargs["npm_workspaces"] = True

        if kwargs.get("npm_workspaces") and not npm_available:
            logger.warning(
                "npm_workspaces requested but npm is not installed; disabling."
            )
            kwargs["npm_workspaces"] = False

        # Keep flags mutually exclusive.
        if kwargs.get("yarn_workspaces"):
            kwargs["pnpm_workspaces"] = False
            kwargs["npm_workspaces"] = False
        elif kwargs.get("pnpm_workspaces"):
            kwargs["npm_workspaces"] = False

        return kwargs

    def generate_index(
        self,
        project_name: Optional[str] = None,
        infer_tsconfig: bool = False,
        yarn_workspaces: bool = False,
        pnpm_workspaces: bool = False,
        npm_workspaces: bool = False,
        patched_tsconfig: Optional[str] = None,
    ) -> bool:
        """
        Generate SCIP index for the TypeScript/JavaScript project.

        Args:
            project_name: Project name (not used by scip-typescript)
            infer_tsconfig: Infer tsconfig for JavaScript projects without tsconfig.json
            yarn_workspaces: Enable Yarn workspaces support
            pnpm_workspaces: Enable pnpm workspaces support
            npm_workspaces: Enable npm workspaces support
            patched_tsconfig: Path to a patched tsconfig file to use

        Returns:
            bool: True if index generation was successful, False otherwise
        """
        # Guardrail: if no explicit ts/js config exists, force infer mode.
        has_tsconfig = (self.project_root / "tsconfig.json").exists()
        has_jsconfig = (self.project_root / "jsconfig.json").exists()
        if not infer_tsconfig and not has_tsconfig and not has_jsconfig:
            infer_tsconfig = True
            logger.info(
                "No tsconfig.json/jsconfig.json in %s; forcing infer_tsconfig=True",
                self.project_root,
            )

        return super().generate_index(
            project_name=project_name,
            infer_tsconfig=infer_tsconfig,
            yarn_workspaces=yarn_workspaces,
            pnpm_workspaces=pnpm_workspaces,
            npm_workspaces=npm_workspaces,
            patched_tsconfig=patched_tsconfig,
        )

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        """
        Run TypeScript pipeline with language-specific defaults.

        If neither tsconfig.json nor jsconfig.json exists, enable
        --infer-tsconfig automatically unless caller explicitly sets it.
        """
        # Drop Python-specific kwargs that may be forwarded by callers.
        kwargs.pop("target_dir", None)
        kwargs.pop("cwd", None)

        # Auto-select workspace mode when not explicitly provided.
        workspace_flags = ("yarn_workspaces", "pnpm_workspaces", "npm_workspaces")
        has_workspace_mode = any(kwargs.get(flag) for flag in workspace_flags)
        if not has_workspace_mode:
            # pnpm defines workspaces in pnpm-workspace.yaml (not package.json),
            # so check for it independently.
            has_pnpm_workspace_yaml = (
                self.project_root / "pnpm-workspace.yaml"
            ).exists()

            has_pkg_workspaces = False
            package_json = self.project_root / "package.json"
            if package_json.exists():
                try:
                    payload = json.loads(package_json.read_text(encoding="utf-8"))
                    has_pkg_workspaces = bool(payload.get("workspaces"))
                except Exception:
                    pass

            if has_pnpm_workspace_yaml and shutil.which("pnpm"):
                kwargs["pnpm_workspaces"] = True
                logger.info("Detected pnpm workspace in %s", self.project_root)
            elif has_pkg_workspaces:
                if (self.project_root / "yarn.lock").exists() and shutil.which("yarn"):
                    kwargs["yarn_workspaces"] = True
                    logger.info("Detected yarn workspace in %s", self.project_root)
                else:
                    kwargs["npm_workspaces"] = True
                    logger.info("Detected npm workspace in %s", self.project_root)

        # Only enable --infer-tsconfig when no tsconfig/jsconfig exists.
        if "infer_tsconfig" not in kwargs:
            has_tsconfig = (self.project_root / "tsconfig.json").exists()
            has_jsconfig = (self.project_root / "jsconfig.json").exists()
            if not has_tsconfig and not has_jsconfig:
                kwargs["infer_tsconfig"] = True
                logger.info(
                    "No tsconfig.json/jsconfig.json; enabling infer_tsconfig for %s",
                    self.project_root,
                )
            else:
                kwargs["infer_tsconfig"] = False
                logger.info(
                    "Using existing tsconfig/jsconfig for %s", self.project_root
                )

        kwargs = self._normalize_workspace_kwargs(kwargs)

        # Determine whether index generation will actually run.
        # and dependency installation / tsconfig patching
        needs_generate = True
        if skip_level == "graph" and self.graph_file.exists():
            needs_generate = False
        elif skip_level in ("graph", "decode") and self.decoded_file.exists():
            needs_generate = False
        elif skip_level in ("graph", "decode", "raw") and self.index_file.exists():
            needs_generate = False

        if needs_generate:
            self._install_dependencies()
            patched_tsconfig = self._ensure_allow_js()
            if patched_tsconfig is not None:
                kwargs["patched_tsconfig"] = str(patched_tsconfig)
        else:
            logger.info(
                "Skipping dependency install and tsconfig patching (cached artifacts exist)"
            )

        logger.info("TypeScript run_pipeline kwargs: %s", kwargs)
        try:
            return super().run_pipeline(
                output_file=output_file,
                skip_level=skip_level,
                reset_profiler=reset_profiler,
                report_profile=report_profile,
                **kwargs,
            )
        finally:
            self._cleanup_patched_tsconfig()
