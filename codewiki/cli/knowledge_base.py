"""Post-generation knowledge base linker.

After ``codewiki generate`` finishes, this module:
1. Creates a symlink from the output docs directory into a central knowledge
   base directory (default ``/work/SPK-OS/knowledge/base/raw/codewiki``).
2. Generates / updates an ``index.html`` listing all linked repos.
3. Starts a background ``python3 -m http.server`` on the configured port so
   the knowledge base is immediately browsable.

The knowledge base path and HTTP port are configurable via CLI options
``--kb-dir`` and ``--kb-port``; pass ``--no-kb`` to skip the hook entirely.
"""

from __future__ import annotations

import html
import logging
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_KB_DIR = "/work/SPK-OS/knowledge/base/raw/codewiki"
DEFAULT_KB_PORT = 8081


def link_to_knowledge_base(
    output_dir: str | Path,
    repo_name: str,
    repo_url: Optional[str] = None,
    kb_dir: str = DEFAULT_KB_DIR,
    kb_port: int = DEFAULT_KB_PORT,
) -> None:
    """Create symlink, update index, and ensure HTTP server is running.

    Parameters
    ----------
    output_dir : str | Path
        The docs output directory (e.g. ``/path/to/repo/docs``).
    repo_name : str
        Repository name used as the symlink leaf name.
    repo_url : str, optional
        Remote URL for display in the index page.
    kb_dir : str
        Root knowledge base directory.
    kb_port : int
        Port for the HTTP server.
    """
    kb_path = Path(kb_dir)
    try:
        kb_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create knowledge base dir %s: %s", kb_path, exc)
        return

    abs_output = os.path.abspath(str(output_dir))

    # --- 1. Create / refresh symlink ---
    link_path = kb_path / repo_name
    try:
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(abs_output)
        logger.info("Linked %s -> %s", link_path, abs_output)
    except OSError as exc:
        logger.warning("Cannot create symlink %s: %s", link_path, exc)
        return

    # --- 1b. Ensure HTML viewer exists ---
    _ensure_html_viewer(Path(abs_output), repo_name)

    # --- 2. Update index.html ---
    _update_index_html(kb_path, repo_name, repo_url)

    # --- 3. Ensure HTTP server is running ---
    _ensure_http_server(kb_path, kb_port)

    click_echo = None
    try:
        import click
        click_echo = click.echo
    except ImportError:
        pass

    msg = (
        f"\n📚 Knowledge base linked: {repo_name}\n"
        f"   Index:  http://localhost:{kb_port}/\n"
        f"   Browse: http://localhost:{kb_port}/{repo_name}/"
    )
    if click_echo:
        click_echo(msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_html_viewer(docs_dir: Path, repo_name: str) -> None:
    """Generate ``index.html`` viewer in *docs_dir* if missing."""
    index_path = docs_dir / "index.html"
    if index_path.exists():
        return

    try:
        from codewiki.cli.html_generator import HTMLGenerator

        html_gen = HTMLGenerator()
        repo_path = docs_dir.parent
        repo_info = html_gen.detect_repository_info(repo_path)
        html_gen.generate(
            output_path=index_path,
            title=repo_info.get("name") or repo_name,
            repository_url=repo_info.get("url"),
            github_pages_url=repo_info.get("github_pages_url"),
            docs_dir=docs_dir,
        )
        logger.info("Generated HTML viewer at %s", index_path)
    except Exception as exc:
        logger.warning("Cannot generate HTML viewer: %s", exc)


def _update_index_html(kb_path: Path, new_repo: str, repo_url: Optional[str] = None) -> None:
    """Scan *kb_path* for all repo symlinks and regenerate ``index.html``."""
    repos: list[dict] = []
    for entry in sorted(kb_path.iterdir()):
        if entry.name == "index.html":
            continue
        if not (entry.is_symlink() or entry.is_dir()):
            continue
        has_index = (entry / "index.html").exists()
        has_overview = (entry / "overview.md").exists()
        repos.append({
            "name": entry.name,
            "path": entry.name,
            "has_index": has_index,
            "has_overview": has_overview,
        })

    html_content = _generate_index_html(repos)
    index_path = kb_path / "index.html"
    try:
        index_path.write_text(html_content, encoding="utf-8")
        logger.info("Updated index.html with %d repos", len(repos))
    except OSError as exc:
        logger.warning("Cannot write index.html: %s", exc)


def _generate_index_html(repos: list[dict]) -> str:
    """Return the full HTML for the knowledge base index page."""
    rows = ""
    for repo in repos:
        name = html.escape(repo["name"])
        if repo["has_index"]:
            link_target = f"{repo['path']}/index.html"
            status = "✓ HTML"
        elif repo["has_overview"]:
            link_target = f"{repo['path']}/overview.md"
            status = "✓ MD"
        else:
            link_target = f"{repo['path']}/"
            status = "—"
        rows += (
            f"\n        <tr>"
            f'<td><a href="{link_target}">{name}</a></td>'
            f"<td>{status}</td>"
            f"</tr>"
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeWiki 知识库索引</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            max-width: 960px; margin: 40px auto; padding: 0 20px;
            color: #1a1a1a; background: #fafafa;
        }}
        h1 {{
            border-bottom: 3px solid #4CAF50; padding-bottom: 12px;
            font-size: 1.8em;
        }}
        .summary {{ color: #666; margin: 8px 0 24px; font-size: 0.95em; }}
        table {{
            width: 100%; border-collapse: collapse;
            background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            border-radius: 6px; overflow: hidden;
        }}
        th, td {{ padding: 14px 18px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f0f0f0; font-weight: 600; font-size: 0.9em; text-transform: uppercase; letter-spacing: 0.5px; }}
        tr:hover {{ background: #f5f5f5; }}
        a {{ color: #2196F3; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .footer {{ margin-top: 32px; color: #aaa; font-size: 0.82em; text-align: center; }}
    </style>
</head>
<body>
    <h1>📚 CodeWiki 知识库索引</h1>
    <p class="summary">共 {len(repos)} 个仓库文档 · 点击仓库名称浏览文档</p>
    <table>
        <thead><tr><th>仓库名称</th><th>文档格式</th></tr></thead>
        <tbody>{rows}
        </tbody>
    </table>
    <div class="footer">由 CodeWiki 自动生成 · {now}</div>
</body>
</html>"""


def _ensure_http_server(kb_path: Path, port: int) -> None:
    """Start ``python3 -m http.server`` in the background if the port is free."""
    # Check if port is already in use (server already running)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        sock.close()
    except OSError:
        # Port is in use — assume our server is already running
        logger.debug("Port %d already in use, HTTP server likely running", port)
        return

    try:
        subprocess.Popen(
            ["python3", "-m", "http.server", str(port)],
            cwd=str(kb_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent process group
        )
        logger.info("Started HTTP server on port %d serving %s", port, kb_path)
    except Exception as exc:
        logger.warning("Failed to start HTTP server: %s", exc)
