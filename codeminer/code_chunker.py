#!/usr/bin/env python3
"""
Code chunking module with both file-level and repository-level functionality.
This file maintains backward compatibility with the old API and adds repository processing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Import from the new modular code chunking system
from .code_chunking import CodeChunk, create_chunker
from .log_utils import get_logger
from .utils import is_test_file

logger = get_logger(__name__)


@dataclass
class RepoChunkingConfig:
    """Configuration for repository chunking."""

    languages: List[str] = field(default_factory=list)
    # File type configurations
    python_extensions: Set[str] = None
    cpp_extensions: Set[str] = None
    rust_extensions: Set[str] = None
    javascript_extensions: Set[str] = None
    typescript_extensions: Set[str] = None

    # Directory filtering
    ignore_dirs: Set[str] = None
    include_dirs: Set[str] = None  # If specified, only these dirs will be processed

    # File filtering
    ignore_patterns: Set[str] = None
    max_file_size_mb: float = 10.0  # Skip files larger than this
    filter_tests: bool = True  # Skip test files by default

    def __post_init__(self):
        """Initialize default values."""
        if not self.languages:
            self.languages = []

        if self.python_extensions is None:
            self.python_extensions = {".py", ".pyx", ".pyi"}

        if self.cpp_extensions is None:
            self.cpp_extensions = {".cpp", ".cxx", ".cc", ".c", ".hpp", ".h", ".hxx"}

        if self.rust_extensions is None:
            self.rust_extensions = {".rs"}

        if self.javascript_extensions is None:
            self.javascript_extensions = {".js", ".jsx", ".mjs"}

        if self.typescript_extensions is None:
            self.typescript_extensions = {".ts", ".tsx", ".mts", ".cts"}

        if self.ignore_dirs is None:
            self.ignore_dirs = {
                "__pycache__",
                ".git",
                ".svn",
                ".hg",
                "node_modules",
                ".venv",
                "venv",
                "env",
                ".env",
                "build",
                "dist",
                ".pytest_cache",
                ".mypy_cache",
                ".tox",
                "htmlcov",
            }

        if self.ignore_patterns is None:
            self.ignore_patterns = {
                ".*",
                "*.pyc",
                "*.pyo",
                "*.pyd",
                "*.so",
                "*.dll",
                "*.exe",
                "*.o",
                "*.obj",
                "*~",
                "*.bak",
                "*.tmp",
            }


@dataclass
class HybridChunkingConfig:
    """
    Configuration for hybrid chunk selection.

    The goal is to route high-importance chunks to a stronger embedding model while
    sending the rest to a smaller/faster model. Importance is currently measured
    with a simple heuristic (chunk size), but the config keeps things flexible for
    future graph-aware signals.
    """

    importance_metric: str = "chunk_size"
    high_importance_ratio: float = 0.3
    min_high_importance_lines: int = 40

    def __post_init__(self):
        if self.high_importance_ratio <= 0 or self.high_importance_ratio > 1:
            logger.warning(
                "high_importance_ratio must be in (0, 1]; falling back to 0.3"
            )
            self.high_importance_ratio = 0.3


@dataclass
class HybridChunkingResult:
    """Result of hybrid chunk selection with importance annotations."""

    primary_chunks: List[CodeChunk]
    secondary_chunks: List[CodeChunk]
    cutoff_score: float
    config: HybridChunkingConfig

    def all_chunks(self) -> List[Dict[str, Any]]:
        """Return all annotated chunks, primary first."""
        return self.primary_chunks + self.secondary_chunks


# For backward compatibility, expose the old class name
class CodeChunker:
    """
    Unified code chunker that supports both file-level and repository-level processing.
    Maintains backward compatibility with the old API while adding repository functionality.
    """

    def __init__(
        self,
        language: str = "python",
        repo_config: Optional[RepoChunkingConfig] = None,
        max_lines_per_chunk: Optional[int] = None,
        chunk_depth: int = 2,
        include_header_epilogue: bool = False,
        l2_level_exclusive: bool = True,
        skeleton_mode: bool = False,
        include_l2_in_file_skeleton: bool = True,
    ):
        """
        Initialize the code chunker for a specific language.

        Args:
            language: Programming language to parse ('python', 'cpp', 'java', etc.)
            repo_config: Configuration for repository-level chunking. Uses defaults if
                None.
            max_lines_per_chunk: Maximum number of lines per emitted chunk.
                Default: None (no splitting)
            chunk_depth: Depth of AST traversal (1=top-level only, 2=include methods)
            include_header_epilogue: Whether to include file headers and epilogues.
                Default: False
            l2_level_exclusive: When chunk_depth is 2, whether to omit L1 container
                nodes (classes/structs/impls) and emit only L2 members. Default: True.
            skeleton_mode: Emit signature-only skeletons instead of full bodies when True.
            include_l2_in_file_skeleton: When chunk_depth is 0, include member
                signatures in file-level skeletons. Default: True.
        """
        self.language = language
        self.max_lines_per_chunk = max_lines_per_chunk
        self.chunk_depth = chunk_depth
        self.include_header_epilogue = include_header_epilogue
        self.l2_level_exclusive = l2_level_exclusive
        self.skeleton_mode = skeleton_mode
        self.include_l2_in_file_skeleton = include_l2_in_file_skeleton
        self._chunker = create_chunker(
            language,
            max_lines_per_chunk=self.max_lines_per_chunk,
            chunk_depth=self.chunk_depth,
            include_header_epilogue=self.include_header_epilogue,
            l2_level_exclusive=self.l2_level_exclusive,
            skeleton_mode=self.skeleton_mode,
            include_l2_in_file_skeleton=self.include_l2_in_file_skeleton,
        )
        if repo_config is None:
            repo_config = RepoChunkingConfig()
        if not repo_config.languages:
            repo_config.languages = [language]
        self.repo_config = repo_config
        self._chunkers = {language: self._chunker}  # Cache chunkers by language
        self.nodes = (
            []
        )  # List of node IDs in code graph format (dir/file.py:A.b(), dir/file.py:A)

    def chunk_file(
        self,
        file_path: str,
        relative_path: Optional[str] = None,
        skeleton_mode: Optional[bool] = None,
    ):
        """
        Chunk a code file into function/class level pieces.

        Args:
            file_path: Path to the code file to chunk
            relative_path: Relative path for node_id generation. If None, uses file_path.
            skeleton_mode: Override instance-level skeleton setting. When True, chunk content
                contains signatures only.

        Returns:
            List of CodeChunk objects
        """
        chunks = self._chunker.chunk_file(
            file_path, relative_path=relative_path, skeleton_mode=skeleton_mode
        )
        # Collect unique node IDs from chunks
        self._update_nodes_from_chunks(chunks)
        return chunks

    def save_chunks_to_json(self, chunks, output_path: str):
        """Save chunks to a JSON file."""
        return self._chunker.save_chunks_to_json(chunks, output_path)

    def print_chunk_summary(self, chunks):
        """Print a summary of the generated chunks."""
        return self._chunker.print_chunk_summary(chunks)

    # Repository-level methods
    def chunk_repository(self, repo_path: str) -> List[CodeChunk]:
        """
        Chunk all relevant files in a repository.

        Args:
            repo_path: Path to the repository root.
                Languages come from RepoChunkingConfig; when empty they are inferred
                from file extensions present in the repo.

        Returns:
            List of CodeChunk objects from all processed files

        Raises:
            ValueError: If repo_path doesn't exist or languages are unsupported
        """
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

        if not repo_path.is_dir():
            raise ValueError(f"Repository path is not a directory: {repo_path}")

        languages = self.repo_config.languages or self._detect_repo_language(repo_path)

        logger.info(f"Chunking repository: {repo_path}")
        logger.info(f"Target languages: {', '.join(languages)}")

        # Discover files
        files_to_process = self._discover_files(repo_path, languages)
        logger.info(f"Found {len(files_to_process)} files to process")

        # Process files
        all_chunks = []
        processed_count = 0

        for file_path, language in files_to_process:
            try:
                chunks = self._chunk_file_with_language(file_path, language, repo_path)
                all_chunks.extend(chunks)
                processed_count += 1

            except Exception as e:
                logger.warning(f"Failed to process {file_path}: {e}")
                continue

        logger.info(
            f"Successfully processed {processed_count}/{len(files_to_process)} files"
        )
        logger.info(f"Generated {len(all_chunks)} total chunks")

        # Collect unique node IDs from all chunks
        self._update_nodes_from_chunks(all_chunks)
        logger.info(f"Collected {len(self.nodes)} unique nodes")

        return all_chunks

    def get_repository_stats(self, repo_path: str) -> Dict[str, Any]:
        """
        Get statistics about the repository without processing files.

        Args:
            repo_path: Path to the repository root.
                Languages come from RepoChunkingConfig; when empty they are inferred.

        Returns:
            Dictionary containing repository statistics
        """
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

        languages = self.repo_config.languages or self._detect_repo_language(repo_path)
        files_to_process = self._discover_files(repo_path, languages)

        stats = {
            "repo_path": str(repo_path),
            "total_files": len(files_to_process),
            "languages": list(languages),
            "files_by_language": {},
            "total_size_mb": 0.0,
        }

        # Group by language and calculate sizes
        for file_path, language in files_to_process:
            if language not in stats["files_by_language"]:
                stats["files_by_language"][language] = []

            file_size = file_path.stat().st_size / (1024 * 1024)  # MB
            stats["files_by_language"][language].append(
                {
                    "path": str(file_path.relative_to(repo_path)),
                    "size_mb": round(file_size, 3),
                }
            )
            stats["total_size_mb"] += file_size

        stats["total_size_mb"] = round(stats["total_size_mb"], 3)

        # Add counts by language
        for language in stats["files_by_language"]:
            count = len(stats["files_by_language"][language])
            logger.debug(f"  {language}: {count} files")

        return stats

    def print_repository_summary(self, chunks: List[CodeChunk]):
        """Print a summary of all chunks from the repository."""
        if not chunks:
            logger.info("No chunks generated")
            return

        logger.info(f"\n=== Repository Chunk Summary ===")
        logger.info(f"Total chunks: {len(chunks)}")

        # Group by file
        chunks_by_file = {}
        chunk_types = {}

        for chunk in chunks:
            if chunk.file not in chunks_by_file:
                chunks_by_file[chunk.file] = []
            chunks_by_file[chunk.file].append(chunk)

            # Count chunk types
            chunk_types[chunk.chunk_type] = chunk_types.get(chunk.chunk_type, 0) + 1

        logger.info(f"Files processed: {len(chunks_by_file)}")
        logger.info("Chunk types:")
        for chunk_type, count in sorted(chunk_types.items()):
            logger.info(f"  {chunk_type}: {count}")

        logger.info(f"\nChunks per file:")
        for file_path, file_chunks in chunks_by_file.items():
            rel_path = Path(file_path).name  # Just show filename for brevity
            logger.info(f"  {rel_path}: {len(file_chunks)} chunks")

    def _discover_files(self, repo_path: Path, languages: List[str]) -> List[tuple]:
        """
        Discover all relevant code files in the repository.

        Args:
            repo_path: Path to repository root
            languages: List of languages to look for

        Returns:
            List of (file_path, language) tuples
        """
        files_to_process = []

        # Get extension mappings for requested languages
        extension_to_language = {}
        for language in languages:
            if language.lower() == "python":
                for ext in self.repo_config.python_extensions:
                    extension_to_language[ext] = "python"
            elif language.lower() in ("cpp", "c++", "cxx"):
                for ext in self.repo_config.cpp_extensions:
                    extension_to_language[ext] = "cpp"
            elif language.lower() == "rust":
                for ext in self.repo_config.rust_extensions:
                    extension_to_language[ext] = "rust"
            elif language.lower() in ("javascript", "js"):
                for ext in self.repo_config.javascript_extensions:
                    extension_to_language[ext] = "javascript"
            elif language.lower() in ("typescript", "ts"):
                for ext in self.repo_config.typescript_extensions:
                    extension_to_language[ext] = "typescript"
            else:
                logger.warning("Language %r not supported yet", language)

        # Walk through repository
        for root, dirs, files in os.walk(repo_path):
            root_path = Path(root)

            # Filter directories
            dirs[:] = [d for d in dirs if self._should_include_directory(root_path / d)]

            # Process files in current directory
            for file_name in files:
                file_path = root_path / file_name

                if self._should_process_file(file_path, extension_to_language):
                    language = extension_to_language[file_path.suffix]
                    files_to_process.append((file_path, language))

        return files_to_process

    def _should_include_directory(self, dir_path: Path) -> bool:
        """Check if a directory should be included in processing."""
        dir_name = dir_path.name

        # Skip ignored directories
        if dir_name in self.repo_config.ignore_dirs:
            return False

        # If include_dirs is specified, only include those
        if self.repo_config.include_dirs:
            return dir_name in self.repo_config.include_dirs

        return True

    def _should_process_file(
        self, file_path: Path, extension_to_language: Dict[str, str]
    ) -> bool:
        """Check if a file should be processed."""
        # Check extension
        if file_path.suffix not in extension_to_language:
            return False

        # Check file size
        try:
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            if file_size_mb > self.repo_config.max_file_size_mb:
                logger.debug(f"Skipping large file ({file_size_mb:.1f}MB): {file_path}")
                return False
        except OSError:
            return False

        # Check ignore patterns (basic pattern matching)
        file_name = file_path.name
        for pattern in self.repo_config.ignore_patterns:
            if pattern.startswith("*") and file_name.endswith(pattern[1:]):
                return False
            elif pattern.endswith("*") and file_name.startswith(pattern[:-1]):
                return False
            elif pattern == file_name:
                return False

        # Check if test files should be filtered
        if self.repo_config.filter_tests:
            if is_test_file(str(file_path)):
                return False

        return True

    def _chunk_file_with_language(
        self, file_path: Path, language: str, repo_path: Optional[Path] = None
    ) -> List[CodeChunk]:
        """
        Chunk a single file using the appropriate language chunker.

        Args:
            file_path: Path to the file to chunk
            language: Programming language of the file
            repo_path: Repository root path (for relative path calculation)

        Returns:
            List of CodeChunk objects
        """
        # Get or create chunker for this language
        if language not in self._chunkers:
            self._chunkers[language] = create_chunker(
                language,
                max_lines_per_chunk=self.max_lines_per_chunk,
                chunk_depth=self.chunk_depth,
                include_header_epilogue=self.include_header_epilogue,
                l2_level_exclusive=self.l2_level_exclusive,
                skeleton_mode=self.skeleton_mode,
                include_l2_in_file_skeleton=self.include_l2_in_file_skeleton,
            )

        chunker = self._chunkers[language]

        # Calculate relative path if repo_path is provided
        if repo_path:
            relative_path = str(file_path.relative_to(repo_path))
            # Pass relative path to chunker for proper node_id generation
            return chunker.chunk_file(
                str(file_path),
                relative_path=relative_path,
                skeleton_mode=self.skeleton_mode,
            )
        else:
            return chunker.chunk_file(str(file_path), skeleton_mode=self.skeleton_mode)

    def chunk_swebench_instance(
        self,
        instance: Dict[str, Any],
        cache_dir: Optional[str] = None,
        languages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Chunk a SWE-bench instance repository.

        Args:
            instance: SWE-bench instance dictionary containing repo, base_commit,
                instance_id, etc.
            cache_dir: Directory to cache repositories. Defaults to ~/.codeminer
            languages: List of languages to process. If None, uses repository's
                primary language

        Returns:
            Dictionary containing instance info, repo path, and chunks
        """
        from .env.process_swebench_data import process_swebench_instance

        if cache_dir is None:
            cache_dir = "~/.codeminer"

        # Process the SWE-bench instance (download repo, checkout commit)
        repo_path = process_swebench_instance(instance, cache_dir)

        # Auto-detect primary language if not specified
        if languages is None:
            languages = self._detect_repo_language(Path(repo_path))

        logger.info(f"Processing SWE-bench instance: {instance['instance_id']}")
        logger.info(f"Repository: {instance['repo']}")
        logger.info(f"Base commit: {instance['base_commit']}")
        logger.info(f"Repository path: {repo_path}")

        # Chunk the repository
        previous_languages = list(self.repo_config.languages)
        self.repo_config.languages = languages
        try:
            chunks = self.chunk_repository(repo_path)
        finally:
            self.repo_config.languages = previous_languages

        # Return comprehensive information
        result = {
            "instance_id": instance["instance_id"],
            "repo": instance["repo"],
            "base_commit": instance["base_commit"],
            "problem_statement": instance.get("problem_statement", ""),
            "repo_path": repo_path,
            "chunks": chunks,
            "languages": languages,
            "total_chunks": len(chunks),
            "chunk_summary": self._get_chunk_summary_dict(chunks),
            "nodes": self.nodes.copy(),  # Include collected nodes
        }

        return result

    def _detect_repo_language(self, repo_path: Path) -> List[str]:
        """
        Auto-detect the primary programming language(s) in a repository.

        Args:
            repo_path: Path to the repository

        Returns:
            List of detected languages
        """
        language_extensions = {
            "python": {".py", ".pyx", ".pyi"},
            "cpp": {".cpp", ".cxx", ".cc", ".c", ".hpp", ".h", ".hxx"},
            "java": {".java"},
            "javascript": {".js", ".jsx", ".ts", ".tsx"},
            "rust": {".rs"},
        }

        detected_languages = set()

        # Count files by extension
        extension_counts = {}
        for root, dirs, files in os.walk(repo_path):
            # Skip common ignore directories
            dirs[:] = [d for d in dirs if d not in self.repo_config.ignore_dirs]

            for file in files:
                file_path = Path(root) / file
                if file_path.suffix:
                    extension_counts[file_path.suffix] = (
                        extension_counts.get(file_path.suffix, 0) + 1
                    )

        # Map extensions to languages
        for language, extensions in language_extensions.items():
            if any(ext in extension_counts for ext in extensions):
                detected_languages.add(language)

        # Default to Python if no languages detected or if Python is present
        if not detected_languages or "python" in detected_languages:
            detected_languages.add("python")

        result = list(detected_languages)
        logger.info(f"Detected languages: {result}")
        return result

    def _get_chunk_summary_dict(self, chunks: List[CodeChunk]) -> Dict[str, Any]:
        """Get a dictionary summary of chunks (for JSON serialization)."""
        if not chunks:
            return {}

        chunk_types = {}
        files_processed = set()

        for chunk in chunks:
            files_processed.add(chunk.file)
            chunk_types[chunk.chunk_type] = chunk_types.get(chunk.chunk_type, 0) + 1

        return {
            "total_chunks": len(chunks),
            "files_processed": len(files_processed),
            "chunk_types": chunk_types,
        }

    def save_swebench_chunks(self, swebench_result: Dict[str, Any], output_path: str):
        """
        Save SWE-bench chunking result to JSON file.

        Args:
            swebench_result: Result from chunk_swebench_instance()
            output_path: Path to save the JSON file
        """
        import json

        # Create a serializable version (excluding chunks content for size)
        save_data = {
            "instance_id": swebench_result["instance_id"],
            "repo": swebench_result["repo"],
            "base_commit": swebench_result["base_commit"],
            "problem_statement": swebench_result["problem_statement"],
            "repo_path": swebench_result["repo_path"],
            "languages": swebench_result["languages"],
            "total_chunks": swebench_result["total_chunks"],
            "chunk_summary": swebench_result["chunk_summary"],
            "chunks_preview": [
                {
                    "file": chunk.file,
                    "chunk_type": chunk.chunk_type,
                    "name": chunk.name,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content_length": len(chunk.content),
                }
                for chunk in swebench_result["chunks"][
                    :10
                ]  # Save preview of first 10 chunks
            ],
        }

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=2, ensure_ascii=False)
            logger.info(f"SWE-bench chunking result saved to {output_path}")
        except Exception as e:
            logger.error(f"Error saving SWE-bench result: {e}")

    def _update_nodes_from_chunks(self, chunks: List[CodeChunk]):
        """
        Update the nodes list with unique node IDs from chunks.

        Args:
            chunks: List of CodeChunk objects to extract node IDs from
        """
        # Extract node_ids from chunks, filtering for symbolic nodes (not just file paths)
        new_node_ids = set()
        for chunk in chunks:
            if chunk.node_id and ":" in chunk.node_id:
                # Only include nodes that have symbols (contain ':')
                # This excludes header/epilogue chunks which just use file paths
                new_node_ids.add(chunk.node_id)

        # Add new unique node IDs to the nodes list
        existing_nodes = set(self.nodes)
        for node_id in new_node_ids:
            if node_id not in existing_nodes:
                self.nodes.append(node_id)

    def clear_nodes(self):
        """Clear the collected nodes list."""
        self.nodes.clear()

    # Hybrid chunking helpers -------------------------------------------------
    def hybrid_chunk_repository(
        self,
        repo_path: str,
        hybrid_config: Optional[HybridChunkingConfig] = None,
    ) -> HybridChunkingResult:
        """
        Chunk the repository and annotate chunks with importance tiers for hybrid
        embedding strategies. Primary chunks can be embedded with a larger/more
        accurate model while secondary chunks use a lighter model.

        Args:
            repo_path: Path to the repository root.
            hybrid_config: Configuration controlling the importance heuristic.

        Returns:
            HybridChunkingResult with primary/secondary chunk lists that include
            importance metadata.
        """
        config = hybrid_config or HybridChunkingConfig()
        chunks = self.chunk_repository(repo_path)
        if not chunks:
            return HybridChunkingResult(
                primary_chunks=[],
                secondary_chunks=[],
                cutoff_score=0.0,
                config=config,
            )

        # Score chunks using the selected heuristic
        scored_chunks = [
            (self._score_chunk_importance(chunk, config), chunk) for chunk in chunks
        ]
        scored_chunks.sort(key=lambda pair: pair[0], reverse=True)

        num_primary = max(1, int(len(scored_chunks) * config.high_importance_ratio))
        cutoff_score = scored_chunks[num_primary - 1][0] if scored_chunks else 0.0

        primary_chunks: List[CodeChunk] = []
        secondary_chunks: List[CodeChunk] = []

        for idx, (score, chunk) in enumerate(scored_chunks):
            tier = "primary"
            if idx >= num_primary or score < config.min_high_importance_lines:
                tier = "secondary"

            if tier == "primary":
                primary_chunks.append(chunk)
            else:
                secondary_chunks.append(chunk)

        return HybridChunkingResult(
            primary_chunks=primary_chunks,
            secondary_chunks=secondary_chunks,
            cutoff_score=cutoff_score,
            config=config,
        )

    def _score_chunk_importance(
        self, chunk: CodeChunk, config: HybridChunkingConfig
    ) -> float:
        """
        Score a chunk for importance. Currently uses chunk size (lines of code)
        as a simple heuristic, but can be extended with graph reference counts
        or other signals.
        """
        if config.importance_metric == "chunk_size":
            return max(1, chunk.end_line - chunk.start_line + 1)

        if config.importance_metric == "content_length":
            return len(chunk.content)

        logger.warning(
            "Unknown importance_metric %r; defaulting to chunk_size",
            config.importance_metric,
        )
        return max(1, chunk.end_line - chunk.start_line + 1)
