from dataclasses import dataclass
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config

@dataclass
class CodeWikiDeps:
    absolute_docs_path: str
    absolute_repo_path: str
    registry: dict
    components: dict[str, Node]
    path_to_current_module: list[str]
    current_module_name: str
    module_tree: dict[str, any]
    max_depth: int
    current_depth: int
    config: Config  # LLM configuration
    custom_instructions: str = None
    # L0 file summaries {file_path: summary} from the L0 layer (C).  Lets the
    # leaf (big-model) prompt cite digested summaries instead of raw source.
    l0_summaries: dict = None
    # Emit the condensed signature+summary card instead of full file source (A).
    condensed_view: bool = False
    # Reverse call index {callee_id: [caller_ids]} for the call-graph section.
    reverse_call_index: dict = None