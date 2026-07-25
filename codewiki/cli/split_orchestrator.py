"""
Split orchestrator for very large projects.

When a repository is too large to fit through a single CodeWiki run
(context-window / memory / timeout limits), `codewiki generate --split N`
partitions the tree into per-subdirectory runs, then aggregates the
per-subdirectory documentation into a top-level index + architecture overview
with links to each sub-module's docs.

Layered split semantics (depth measured from ``--split-root``, default cwd):

    codewiki generate --split 2 --split-override root/opencode=3

discovers subdirectories at depth 2 (``root/soft``, ``root/hermes``) and runs
a scoped ``codewiki generate`` in each; the override raises ``root/opencode``
to depth 3 so its children (``root/opencode/soft``, ``root/opencode/test``)
become the split points instead.

Resume: each split point's status (done/failed) is persisted to
``<cache_dir>/split_state.json``.  Re-running the same command skips ``done``
points and only re-runs ``failed`` ones.  Within a single split, the existing
``CheckpointManager`` (namespaced per split via ``--cache-dir``) handles
leaf-level resume.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("codewiki.cli.split_orchestrator")

SPLIT_STATE_FILENAME = "split_state.json"
SPLITS_SUBDIR = "splits"  # per-split outputs live under <root_output>/splits/<name>

# Directories that must never become a split point or be descended into.
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    ".codewiki_cache", ".omo",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    "venv", ".venv", "env", ".env",
    "dist", "build", "target", "out",
    ".next", ".idea", ".vscode",
    "docs",  # avoid recursing into a previous run's output
}


class SplitPoint:
    """A single subdirectory that gets its own scoped `codewiki generate` run."""

    __slots__ = ("abs_path", "relpath", "depth")

    def __init__(self, abs_path: Path, relpath: str, depth: int) -> None:
        self.abs_path = abs_path
        self.relpath = relpath
        self.depth = depth

    @property
    def safe_name(self) -> str:
        """Filesystem-safe, unique-ish name for this split's artifacts.

        Includes a short hash of the relpath so two splits whose final
        segment collides (e.g. two ``utils`` dirs) don't overwrite each other.
        """
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", self.relpath).strip("_") or "root"
        h = abs(hash(self.relpath)) % 100000
        return "{}_{:05d}".format(stem, h)


def parse_overrides(items: List[str]) -> Dict[str, int]:
    """Parse ``--split-override PATH=DEPTH`` strings into ``{relpath: depth}``.

    Paths are normalized (no leading ``./``, no trailing ``/``).
    """
    overrides: Dict[str, int] = {}
    for raw in items or []:
        if "=" not in raw:
            raise ValueError("--split-override expects PATH=DEPTH, got: {!r}".format(raw))
        path_part, depth_part = raw.rsplit("=", 1)
        path_part = path_part.strip().strip("./").rstrip("/")
        depth = int(depth_part.strip())
        if depth < 1:
            raise ValueError("--split-override depth must be >= 1, got: {}".format(depth))
        if not path_part:
            raise ValueError("--split-override path is empty: {!r}".format(raw))
        overrides[path_part] = depth
    return overrides


def _subdirs(path: Path) -> List[Path]:
    """Immediate subdirectories of *path*, skipping junk + hidden dirs."""
    out: List[Path] = []
    try:
        for entry in sorted(path.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name in SKIP_DIRS or name.startswith("."):
                continue
            out.append(entry)
    except (PermissionError, FileNotFoundError):
        pass
    return out


def _assess_split(
    sp: SplitPoint, min_files: int, min_bytes: int
) -> Tuple[bool, str, int, int]:
    """Decide whether a split point has enough code to be worth running.

    Walks *sp.abs_path* for supported code files (skipping junk dirs) and
    returns ``(should_run, reason, file_count, total_bytes)``. A split is
    skipped when it has no supported code, too few files, or too few bytes —
    these would only fail inside the sub-run anyway (e.g. ``validate_repository``
    raises on empty dirs), so skipping them up front avoids wasted subprocess
    + LLM cost and keeps ``split_state`` honest (``skipped`` vs ``failed``).

    Set ``min_files=0`` and ``min_bytes=0`` to disable pre-assessment.
    """
    from codewiki.cli.utils.repo_validator import SUPPORTED_EXTENSIONS

    file_count = 0
    total_bytes = 0
    for root, dirs, files in os.walk(sp.abs_path):
        # Prune junk + hidden dirs in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            file_count += 1
            try:
                total_bytes += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass

    if file_count == 0:
        return False, "no supported code files", file_count, total_bytes
    if min_files > 0 and file_count < min_files:
        return False, "too few code files ({})".format(file_count), file_count, total_bytes
    if min_bytes > 0 and total_bytes < min_bytes:
        return False, "code too small ({} bytes)".format(total_bytes), file_count, total_bytes
    return True, "ok", file_count, total_bytes



def find_split_points(
    root: Path, global_depth: int, overrides: Dict[str, int]
) -> List[SplitPoint]:
    """Walk *root* depth-first; a directory at its configured depth is a split point.

    depth is measured from *root* (root itself = 0).  cfg_depth for a directory
    is ``overrides.get(relpath, global_depth)``.  When ``depth >= cfg_depth``
    the directory is a split point and is NOT descended further; otherwise we
    recurse into its subdirectories.
    """
    root = root.resolve()
    results: List[SplitPoint] = []

    def walk(dir_path: Path, depth: int) -> None:
        if dir_path == root:
            relkey = ""
        else:
            relkey = dir_path.as_posix().replace(root.as_posix() + "/", "")
        cfg_depth = overrides.get(relkey, global_depth)
        if depth >= cfg_depth:
            results.append(SplitPoint(dir_path, relkey or ".", depth))
            return
        for child in _subdirs(dir_path):
            walk(child, depth + 1)

    walk(root, 0)
    return results


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class SplitState:
    """Persists per-split-point status to ``<cache_dir>/split_state.json``."""

    def __init__(self, cache_dir: Path, root: Path, global_depth: int,
                 overrides: Dict[str, int]) -> None:
        self.path = Path(cache_dir) / SPLIT_STATE_FILENAME
        self.data: Dict[str, Any] = {
            "root": str(root),
            "global_depth": global_depth,
            "overrides": overrides,
            "generated_at": None,
            "splits": {},
        }

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("[Split] corrupted %s; starting fresh", self.path)
        self.data.setdefault("splits", {})

    def save(self) -> None:
        self.data["generated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, relpath: str) -> Dict[str, Any]:
        return self.data["splits"].get(relpath, {})

    def set(self, relpath: str, entry: Dict[str, Any]) -> None:
        self.data["splits"][relpath] = entry


def _split_is_valid(entry: Dict[str, Any]) -> bool:
    """A split counts as successfully done iff overview + module_tree exist."""
    if not entry or entry.get("status") != "done":
        return False
    for key in ("overview", "module_tree"):
        p = entry.get(key)
        if not p or not os.path.exists(p):
            return False
    return True


# ---------------------------------------------------------------------------
# Subprocess execution of a single split point
# ---------------------------------------------------------------------------

def _forwarded_argv(
    user_opts: Dict[str, Any], split_output: Path, split_cache: Path
) -> List[str]:
    """Build the argv for a scoped `codewiki generate` subprocess."""
    argv = [
        sys.executable, "-m", "codewiki", "generate",
        "--split", "0",          # never recurse from a split sub-run
        "--no-kb",                # KB linking is a top-level concern
        "-o", str(split_output),
        "--cache-dir", str(split_cache),
    ]
    passthrough = [
        ("include", "--include"),
        ("exclude", "--exclude"),
        ("focus", "--focus"),
        ("doc_type", "--doc-type"),
        ("instructions", "--instructions"),
        ("concurrency", "--concurrency"),
        ("mode", "--mode"),
        ("max_tokens", "--max-tokens"),
        ("max_token_per_module", "--max-token-per-module"),
        ("max_token_per_leaf_module", "--max-token-per-leaf-module"),
        ("max_depth", "--max-depth"),
    ]
    for key, flag in passthrough:
        val = user_opts.get(key)
        if val is not None:
            argv += [flag, str(val)]
    if user_opts.get("verbose"):
        argv.append("-v")
    elif user_opts.get("quiet"):
        argv.append("-q")
    return argv


def _run_one_split(
    sp: SplitPoint,
    argv: List[str],
    timeout: Optional[int],
) -> Tuple[bool, str]:
    """Run the scoped generate subprocess for *sp*; return (ok, stderr_tail)."""
    log_path = sp.abs_path / ".codewiki_split_run.log"
    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write("$ " + " ".join(argv) + "\n# cwd=" + str(sp.abs_path) + "\n")
            logf.flush()
            proc = subprocess.run(
                argv,
                cwd=str(sp.abs_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
            logf.write(proc.stdout or "")
            logf.flush()
        ok = proc.returncode == 0
        tail = "\n".join((proc.stdout or "").splitlines()[-12:])
        return ok, tail
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        tail = "TIMEOUT after {}s\n".format(timeout) + "\n".join(out.splitlines()[-12:])
        return False, tail
    except Exception as e:  # pragma: no cover - defensive
        return False, "subprocess error: {}".format(e)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _make_backend(config: Dict[str, Any], repo_path: Path):
    """Build a backend for the top-level aggregation LLM call."""
    from codewiki.src.config import Config as BackendConfig, set_cli_context
    from codewiki.src.be.backend import get_backend
    set_cli_context(True)
    raw_key = config.get("api_key", "") or ""
    first_key = raw_key.split(",")[0].strip() if raw_key else ""
    backend_config = BackendConfig.from_cli(
        repo_path=str(repo_path),
        output_dir=str(repo_path),
        llm_base_url=config.get("base_url"),
        llm_api_key=first_key,
        main_model=config.get("main_model"),
        cluster_model=config.get("cluster_model"),
        fallback_model=config.get("fallback_model"),
        provider=config.get("provider", "openai-compatible"),
        aws_region=config.get("aws_region", "us-east-1"),
        max_tokens=config.get("max_tokens", 32768),
        max_token_per_module=config.get("max_token_per_module", 36369),
        max_token_per_leaf_module=config.get("max_token_per_leaf_module", 16000),
        max_depth=config.get("max_depth", 2),
        agent_instructions=config.get("agent_instructions"),
        api_keys=config.get("api_keys", ""),
        concurrency=config.get("concurrency", 0),
        disable_proxy=config.get("disable_proxy", True),
        cache_dir=config.get("cache_dir", ".codewiki_cache"),
        resume=config.get("resume", True),
        model_context_window=config.get("model_context_window", 0),
        llm_timeout=config.get("llm_timeout", 1200),
        llm_max_retries=config.get("llm_max_retries", 10),
        llm_retry_interval=config.get("llm_retry_interval", 60),
        analysis_mode=config.get("analysis_mode", "standard"),
    )
    return get_backend(backend_config)


def aggregate_splits(
    root_output: Path,
    root_name: str,
    splits: List[Tuple[SplitPoint, Dict[str, Any]]],
    config: Dict[str, Any],
    repo_path: Path,
) -> None:
    """Build the top-level index, stats doc, and architecture overview.

    *splits* is the list of (SplitPoint, state_entry) for points that are
    ``done``.  The overview is generated from each split's overview.md via
    ``REPO_OVERVIEW_PROMPT`` (LLM, best-effort).  Structural artifacts
    (``split_index.json``, ``split_stats.md``, ``module_tree.json``) are always
    written even if the LLM call fails.
    """
    root_output.mkdir(parents=True, exist_ok=True)

    aggregate_tree: Dict[str, Any] = {}
    index_entries: List[Dict[str, Any]] = []
    child_docs: Dict[str, Dict[str, Any]] = {}

    for sp, entry in splits:
        name = sp.safe_name
        status = entry.get("status", "unknown")
        is_done = _split_is_valid(entry)
        overview_text = ""
        if is_done and entry.get("overview") and os.path.exists(entry["overview"]):
            try:
                with open(entry["overview"], "r", encoding="utf-8") as f:
                    overview_text = f.read()
            except OSError:
                overview_text = ""

        # Only completed splits contribute to the top-level architecture tree
        # and the LLM overview (they're the ones with real docs to summarize).
        if is_done:
            aggregate_tree[name] = {
                "components": [],
                "children": {},
                "relpath": sp.relpath,
                "module_count": entry.get("module_count", 0),
                "status": status,
                "overview": name + ".md",
            }
            child_docs[name] = {
                "docs": overview_text,
                "relpath": sp.relpath,
                "module_count": entry.get("module_count", 0),
            }

        index_entries.append({
            "name": name,
            "relpath": sp.relpath,
            "depth": sp.depth,
            "status": status,
            "module_count": entry.get("module_count", 0),
            "file_count": entry.get("file_count", 0),
            "total_bytes": entry.get("total_bytes", 0),
            "skip_reason": entry.get("skip_reason", ""),
            "error": entry.get("error", ""),
            "output_dir": entry.get("output_dir", ""),
            "overview": entry.get("overview", ""),
            "module_tree": entry.get("module_tree", ""),
        })

    from codewiki.src.utils import file_manager
    file_manager.save_json(aggregate_tree, str(root_output / "module_tree.json"))
    (root_output / "split_index.json").write_text(
        json.dumps(
            {"root": str(repo_path), "root_name": root_name, "splits": index_entries},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # ---- stats markdown (plain .format, no nested f-string quotes) ----
    lines = ["# " + root_name + " - split documentation index", "",
             "| module | relpath | depth | modules | status | note | overview |",
             "|---|---|---|---|---|---|---|"]
    for e in index_entries:
        ov = e.get("overview", "")
        link = os.path.relpath(ov, str(root_output)) if ov else ""
        note = e.get("skip_reason") or (e.get("error", "")[:80] if e.get("error") else "")
        lines.append("| {name} | `{rel}` | {depth} | {mc} | {st} | {note} | [{link}]({link}) |".format(
            name=e["name"], rel=e["relpath"], depth=e["depth"],
            mc=e["module_count"], st=e["status"],
            note=note.replace("|", "/").replace("\n", " "), link=link))
    lines += ["", "## Per-split documentation", ""]
    for e in index_entries:
        ov = e.get("overview", "")
        link = os.path.relpath(ov, str(root_output)) if ov else ""
        lines += ["### {name} (`{rel}`) - {st}".format(
            name=e["name"], rel=e["relpath"], st=e["status"]), ""]
        if e.get("status") == "done":
            lines += ["- modules: {mc}".format(mc=e["module_count"]),
                      "- overview: [{link}]({link})".format(link=link), ""]
        elif e.get("status") == "skipped":
            lines += ["- skipped: {reason} ({fc} files, {tb} bytes)".format(
                reason=e.get("skip_reason", ""), fc=e.get("file_count", 0),
                tb=e.get("total_bytes", 0)), ""]
        elif e.get("status") == "failed":
            err = (e.get("error", "") or "")[-200:]
            lines += ["- failed: {err}".format(err=err.replace("\n", " ")), ""]
        else:
            lines += ["", ]
    (root_output / "split_stats.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- LLM architecture overview (best-effort) ----
    overview_path = root_output / "overview.md"
    if not child_docs:
        overview_path.write_text(
            "# " + root_name + "\n\nNo completed split points to aggregate.\n",
            encoding="utf-8",
        )
        return
    try:
        from codewiki.src.be.prompt_template import REPO_OVERVIEW_PROMPT
        backend = _make_backend(config, repo_path)
        prompt = REPO_OVERVIEW_PROMPT.format(
            repo_name=root_name,
            repo_structure=json.dumps(child_docs, indent=4, ensure_ascii=False),
        )
        resp = backend.complete(prompt)
        if "<OVERVIEW>" in resp and "</OVERVIEW>" in resp:
            content = resp.split("<OVERVIEW>")[1].split("</OVERVIEW>")[0].strip()
        else:
            content = resp.strip()
        link_section = ["", "## Sub-module documentation", ""]
        for e in index_entries:
            ov = e.get("overview", "")
            link = os.path.relpath(ov, str(root_output)) if ov else ""
            link_section.append("- [{name} ({rel})]({link}) - {mc} modules".format(
                name=e["name"], rel=e["relpath"], link=link, mc=e["module_count"]))
        overview_path.write_text(
            content + "\n" + "\n".join(link_section) + "\n",
            encoding="utf-8",
        )
        logger.info("[Split] Aggregated overview written to %s", overview_path)
    except Exception as e:
        logger.warning("[Split] LLM aggregation failed (%s); structural docs still written", e)
        if not overview_path.exists():
            overview_path.write_text(
                "# " + root_name + "\n\n_(LLM aggregation skipped; see split_stats.md)_\n",
                encoding="utf-8",
            )


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def run_split(
    *,
    repo_path: Path,
    output_dir: Path,
    cache_dir: Path,
    split_depth: int,
    split_root: Path,
    overrides: Dict[str, int],
    user_opts: Dict[str, Any],
    aggregate: bool = True,
    jobs: int = 1,
    timeout: Optional[int] = None,
    min_files: int = 1,
    min_bytes: int = 1024,
) -> int:
    """Orchestrate a split run. Returns a process exit code.

    *min_files* / *min_bytes* gate pre-assessment: a split point whose
    supported code is empty or below these thresholds is recorded as
    ``skipped`` (not run, not retried) instead of spawning a sub-run that
    would just fail. Set both to 0 to disable.
    """
    if split_depth < 1:
        logger.error("[Split] --split must be >= 1 (got %d)", split_depth)
        return 2

    split_root = split_root.resolve()
    cache_dir = cache_dir.resolve()
    output_dir = output_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    points = find_split_points(split_root, split_depth, overrides)
    logger.info(
        "[Split] root=%s depth=%d overrides=%s -> %d split point(s)",
        split_root, split_depth, overrides, len(points),
    )
    if not points:
        logger.warning(
            "[Split] no subdirectories at the requested depth under %s; "
            "nothing to split. Run without --split for a single-pass generation.",
            split_root,
        )
        return 1
    for sp in points:
        logger.info("[Split]   - %s (depth %d)", sp.relpath, sp.depth)

    state = SplitState(cache_dir, split_root, split_depth, overrides)
    state.load()

    todo: List[SplitPoint] = []
    empty_skipped = 0
    for sp in points:
        entry = state.get(sp.relpath)
        if _split_is_valid(entry):
            logger.info("[Split] skip (done): %s", sp.relpath)
            continue
        # Pre-assess: skip subdirs with no/too-small code. These would only
        # fail inside the sub-run (validate_repository raises on empty dirs),
        # so skipping up front avoids wasted subprocess + LLM cost and keeps
        # split_state honest (skipped, not retried on resume).
        should_run, reason, fc, tb = _assess_split(sp, min_files, min_bytes)
        if not should_run:
            logger.info(
                "[Split] skip (empty/too-small): %s - %s (%d files, %d bytes)",
                sp.relpath, reason, fc, tb,
            )
            state.set(sp.relpath, {
                "status": "skipped",
                "depth": sp.depth,
                "output_dir": "",
                "overview": "",
                "module_tree": "",
                "module_count": 0,
                "skip_reason": reason,
                "file_count": fc,
                "total_bytes": tb,
                "attempts": 0,
                "last_run": time.time(),
            })
            empty_skipped += 1
            continue
        todo.append(sp)
    logger.info(
        "[Split] %d to run, %d done-skipped, %d empty-skipped",
        len(todo), len(points) - len(todo) - empty_skipped, empty_skipped,
    )
    state.save()

    def _execute(sp: SplitPoint) -> Tuple[SplitPoint, bool, str]:
        sp_output = output_dir / SPLITS_SUBDIR / sp.safe_name
        sp_cache = cache_dir / SPLITS_SUBDIR / sp.safe_name
        sp_output.mkdir(parents=True, exist_ok=True)
        sp_cache.mkdir(parents=True, exist_ok=True)
        argv = _forwarded_argv(user_opts, sp_output, sp_cache)
        logger.info("[Split] run: %s -> %s", sp.relpath, sp_output)
        ok, err_tail = _run_one_split(sp, argv, timeout)

        overview = sp_output / "overview.md"
        module_tree = sp_output / "module_tree.json"
        module_count = 0
        if module_tree.exists():
            try:
                mt = json.loads(module_tree.read_text(encoding="utf-8"))
                module_count = len(mt) if isinstance(mt, dict) else 0
            except (json.JSONDecodeError, OSError):
                module_count = 0
        prev = state.get(sp.relpath)
        attempts = int(prev.get("attempts", 0)) + 1
        entry = {
            "status": "done" if (ok and overview.exists() and module_tree.exists()) else "failed",
            "depth": sp.depth,
            "output_dir": str(sp_output),
            "overview": str(overview),
            "module_tree": str(module_tree),
            "module_count": module_count,
            "error": "" if ok else err_tail,
            "attempts": attempts,
            "last_run": time.time(),
        }
        state.set(sp.relpath, entry)
        return sp, ok, err_tail

    failures = 0
    if jobs > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(todo))) as ex:
            futures = {ex.submit(_execute, sp): sp for sp in todo}
            for fut in as_completed(futures):
                sp, ok, err_tail = fut.result()
                if not ok:
                    failures += 1
                    logger.error("[Split] FAILED: %s\n%s", sp.relpath, err_tail)
                state.save()
    else:
        for sp in todo:
            _sp, ok, err_tail = _execute(sp)
            if not ok:
                failures += 1
                logger.error("[Split] FAILED: %s\n%s", sp.relpath, err_tail)
            state.save()

    state.save()

    all_splits: List[Tuple[SplitPoint, Dict[str, Any]]] = []
    done_count = 0
    for sp in points:
        entry = state.get(sp.relpath)
        if entry:
            all_splits.append((sp, entry))
            if _split_is_valid(entry):
                done_count += 1
    if aggregate and all_splits:
        root_name = split_root.name or "repository"
        logger.info(
            "[Split] aggregating %d split(s) (%d done, %d empty-skipped) into %s",
            len(all_splits), done_count, empty_skipped, output_dir,
        )
        aggregate_splits(output_dir, root_name, all_splits, user_opts, repo_path)
    elif aggregate and not all_splits:
        logger.warning("[Split] no split entries; skipping aggregation")

    if failures:
        logger.error(
            "[Split] finished with %d failed/%d total. Re-run the same command to retry "
            "the failed ones (completed splits will be skipped).",
            failures, len(points),
        )
        return 1
    logger.info("[Split] all %d split point(s) done.", len(points))
    return 0
