"""
Checkpoint manager for resumable documentation generation.

Persists per-task progress and an LLM response cache under a per-repo
directory so re-running CodeWiki on the same repository can skip work
already completed in previous runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class PipelineStage(str, Enum):
    ANALYSIS = "ANALYSIS"
    CLUSTERING = "CLUSTERING"
    DEPENDENCY_GRAPH = "DEPENDENCY_GRAPH"
    DECOMPOSE = "DECOMPOSE"
    LEAF_DOC = "LEAF_DOC"
    PARENT_DOC = "PARENT_DOC"
    OVERVIEW = "OVERVIEW"


@dataclass
class TaskRecord:
    task_id: str
    stage: str
    status: str = TaskStatus.PENDING.value
    result_key: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    done_at: Optional[float] = None
    retries: int = 0


@dataclass
class CheckpointState:
    repo_path: str
    repo_hash: str
    created_at: float
    updated_at: float
    tasks: Dict[str, TaskRecord] = field(default_factory=dict)
    dep_graph_path: Optional[str] = None
    module_tree_path: Optional[str] = None
    analysis_artifacts_path: Optional[str] = None
    components_meta_path: Optional[str] = None
    stage: str = PipelineStage.DEPENDENCY_GRAPH.value

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "repo_hash": self.repo_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": {k: asdict(v) for k, v in self.tasks.items()},
            "dep_graph_path": self.dep_graph_path,
            "module_tree_path": self.module_tree_path,
            "analysis_artifacts_path": self.analysis_artifacts_path,
            "components_meta_path": self.components_meta_path,
            "stage": self.stage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckpointState":
        tasks_data = data.get("tasks") or {}
        tasks = {k: TaskRecord(**v) for k, v in tasks_data.items()}
        return cls(
            repo_path=data["repo_path"],
            repo_hash=data["repo_hash"],
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            tasks=tasks,
            dep_graph_path=data.get("dep_graph_path"),
            module_tree_path=data.get("module_tree_path"),
            analysis_artifacts_path=data.get("analysis_artifacts_path"),
            components_meta_path=data.get("components_meta_path"),
            stage=data.get("stage", PipelineStage.DEPENDENCY_GRAPH.value),
        )


class CheckpointManager:
    """Thread-safe per-repo checkpoint manager with an LLM response cache."""

    def __init__(self, repo_path: str, cache_root: str = ".codewiki_cache") -> None:
        self.repo_path = os.path.abspath(repo_path)
        self.cache_root = os.path.abspath(cache_root)
        self._lock = threading.Lock()

        self.repo_hash = self._get_repo_hash()
        self.cache_dir = os.path.join(self.cache_root, self.repo_hash)
        self.llm_cache_dir = os.path.join(self.cache_dir, "llm_cache")
        self.checkpoint_path = os.path.join(self.cache_dir, "checkpoint.json")

        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.llm_cache_dir, exist_ok=True)

        self.state: CheckpointState = self.load_or_create()

    def _get_repo_hash(self) -> str:
        """Use git HEAD commit hash, falling back to a hash of the repo path."""
        try:
            result = subprocess.run(
                ["git", "-C", self.repo_path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                commit = result.stdout.strip()
                if commit:
                    return commit
        except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
            logger.debug("git rev-parse failed for %s: %s", self.repo_path, e)

        return hashlib.sha256(self.repo_path.encode("utf-8")).hexdigest()[:16]

    def load_or_create(self) -> CheckpointState:
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                state = CheckpointState.from_dict(data)
                if state.repo_hash != self.repo_hash:
                    logger.warning(
                        "Checkpoint repo_hash mismatch (%s vs %s); creating fresh state.",
                        state.repo_hash, self.repo_hash,
                    )
                else:
                    logger.info(
                        "[Checkpoint] Loaded existing state from %s (tasks=%d)",
                        self.checkpoint_path, len(state.tasks),
                    )
                    return state
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Failed to load checkpoint %s: %s", self.checkpoint_path, e)

        now = time.time()
        state = CheckpointState(
            repo_path=self.repo_path,
            repo_hash=self.repo_hash,
            created_at=now,
            updated_at=now,
        )
        return state

    def register_tasks(self, task_ids: List[str], stage: PipelineStage) -> None:
        stage_value = stage.value if isinstance(stage, PipelineStage) else str(stage)
        with self._lock:
            for task_id in task_ids:
                if task_id not in self.state.tasks:
                    self.state.tasks[task_id] = TaskRecord(task_id=task_id, stage=stage_value)
            self._persist()

    def is_done(self, task_id: str) -> bool:
        with self._lock:
            record = self.state.tasks.get(task_id)
            return record is not None and record.status == TaskStatus.DONE.value

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            record = self.state.tasks.get(task_id)
            if record is None:
                record = TaskRecord(task_id=task_id, stage="")
                self.state.tasks[task_id] = record
            record.status = TaskStatus.RUNNING.value
            record.started_at = time.time()
            self._persist()

    def mark_done(self, task_id: str, result_key: Optional[str] = None) -> None:
        with self._lock:
            record = self.state.tasks.get(task_id)
            if record is None:
                record = TaskRecord(task_id=task_id, stage="")
                self.state.tasks[task_id] = record
            record.status = TaskStatus.DONE.value
            record.done_at = time.time()
            record.result_key = result_key
            record.error = None
            self._persist()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            record = self.state.tasks.get(task_id)
            if record is None:
                record = TaskRecord(task_id=task_id, stage="")
                self.state.tasks[task_id] = record
            record.status = TaskStatus.FAILED.value
            record.done_at = time.time()
            record.error = error
            record.retries += 1
            self._persist()

    def set_stage_artifact(self, key: str, path: str) -> None:
        with self._lock:
            if key == "dep_graph_path":
                self.state.dep_graph_path = path
            elif key == "module_tree_path":
                self.state.module_tree_path = path
            elif key == "analysis_artifacts_path":
                self.state.analysis_artifacts_path = path
            elif key == "components_meta_path":
                self.state.components_meta_path = path
            else:
                logger.debug("Unknown stage artifact key: %s", key)
                return
            self._persist()

    def _prompt_hash(self, prompt: str, model: str) -> str:
        h = hashlib.sha256()
        h.update((model or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((prompt or "").encode("utf-8"))
        return h.hexdigest()

    def get_llm_cache(self, prompt: str, model: str) -> Optional[str]:
        key = self._prompt_hash(prompt, model)
        cache_file = os.path.join(self.llm_cache_dir, f"{key}.json")
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("response")
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("LLM cache read failed for %s: %s", key, e)
            return None

    def save_llm_cache(self, prompt: str, model: str, response: str) -> str:
        key = self._prompt_hash(prompt, model)
        cache_file = os.path.join(self.llm_cache_dir, f"{key}.json")
        payload = {
            "model": model,
            "prompt": prompt,
            "response": response,
            "saved_at": time.time(),
        }
        tmp_path = cache_file + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, cache_file)
        except OSError as e:
            logger.warning("Failed to write LLM cache %s: %s", cache_file, e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return key

    def progress(self) -> dict:
        with self._lock:
            total = len(self.state.tasks)
            done = sum(1 for r in self.state.tasks.values() if r.status == TaskStatus.DONE.value)
            failed = sum(1 for r in self.state.tasks.values() if r.status == TaskStatus.FAILED.value)
            pending = sum(
                1 for r in self.state.tasks.values()
                if r.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
            )
            pct = (done / total * 100.0) if total > 0 else 0.0
            return {
                "total": total,
                "done": done,
                "failed": failed,
                "pending": pending,
                "pct": pct,
            }

    def _persist(self) -> None:
        """Atomic write of checkpoint state. Caller must hold self._lock."""
        self.state.updated_at = time.time()
        tmp_path = self.checkpoint_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.checkpoint_path)
        except OSError as e:
            logger.warning("Failed to persist checkpoint %s: %s", self.checkpoint_path, e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
