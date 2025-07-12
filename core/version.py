# core/version.py

import subprocess
import logging

log = logging.getLogger(__name__)

def get_current_version() -> str:
    """
    Fetches the current git commit hash (short version) to identify the build.
    Returns 'development' if git is not installed or if it's not a git repository.
    """
    try:
        # Execute the git command to get the short 7-character commit hash.
        # stderr=subprocess.PIPE prevents git error messages from printing to console.
        version = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.PIPE
        ).strip().decode("utf-8")
        return version
    except (subprocess.CalledProcessError, FileNotFoundError):
        # This handles cases where git is not installed (e.g., in a minimal
        # Docker container) or this isn't a git repository.
        log.warning("Could not determine git version. Defaulting to 'development'.")
        return "development"