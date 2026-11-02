"""Git clone function - clones a git repository"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "git_clone",
        "description": "Clone a git repository from a URL. Downloads the repository to the working_root folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The git repository URL to clone (e.g., 'https://github.com/user/repo.git')"
                },
                "target_dir": {
                    "type": "string",
                    "description": "Optional target directory name (defaults to repository name)"
                },
                "depth": {
                    "type": "integer",
                    "description": "Clone depth for shallow clone (use 1 for latest commit only)",
                    "default": 1
                }
            },
            "required": ["url"]
        }
    }
}

import logging
import os
import subprocess

from ..utils import get_working_root, guard_path_inside_working_root

logger = logging.getLogger("COOLEMS.Tools.Calc")


def git_clone(url: str, target_dir: str = None, depth: int = 1) -> str:
    """Clone a git repository."""
    logger.info(f"[GIT_CLONE] Cloning: {url}")

    if not url.startswith(('http://', 'https://', 'git@')):
        return f"❌ Invalid git URL: {url}"

    working_dir = get_working_root()
    os.makedirs(working_dir, exist_ok=True)

    if target_dir:
        dest_path = os.path.normpath(os.path.join(working_dir, target_dir))
    else:
        repo_name = url.rstrip('/').split('/')[-1]
        if repo_name.endswith('.git'):
            repo_name = repo_name[:-4]
        dest_path = os.path.join(working_dir, repo_name)

    # GUARD — ensure resolved destination stays inside working_root (realpath for symlinks)
    try:
        guard_path_inside_working_root(dest_path, working_dir, label="Clone target directory")
    except ValueError as e:
        return f"❌ {e}"

    # Check if already exists
    if os.path.exists(dest_path):
        try:
            # (2026-11 review fix B7) subprocess.run(cwd=...) instead of os.chdir():
            # os.chdir() mutates the PROCESS-wide CWD, which is shared by every other
            # coroutine/thread in the CLIENT. A concurrent tool (or a crash between
            # chdir and the restore) would run with the cloned repo as its CWD.
            result = subprocess.run(
                ["git", "pull"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=dest_path
            )

            if result.returncode == 0:
                return f"✅ Repository already exists, pulled latest:\n{dest_path}\n{result.stdout[:500]}"
            else:
                return f"⚠️ Repository exists at {dest_path} but pull failed."
        except Exception as e:
            return f"⚠️ Repository exists at {dest_path}: {e}"

    # Clone
    try:
        cmd = ["git", "clone"]
        if depth > 0:
            cmd.extend(["--depth", str(depth)])
        cmd.extend([url, dest_path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode == 0:
            return f"✅ Cloned successfully!\n\nURL: {url}\nLocation: {dest_path}\n\n{result.stdout[:500] if result.stdout else 'Clone completed.'}"
        else:
            return f"❌ Clone failed:\n{result.stderr[:1000]}"

    except subprocess.TimeoutExpired:
        return "❌ Clone timed out after 300 seconds."
    except FileNotFoundError:
        return "❌ Git is not installed. Please install git first."
    except Exception as e:
        return f"❌ Clone failed: {str(e)}"
