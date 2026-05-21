"""
Single-provider state lock detection and removal.

Detection: terragrunt plan -lock-timeout=0s -refresh=false
  With -lock-timeout=0s terraform fails IMMEDIATELY if it cannot acquire
  the lock — before doing any real planning work. The error output contains
  the full lock info (ID, Who, Operation, Created) AND the terragrunt cache
  directory where terraform was initialised.

Unlock strategy:
  1. Primary:  terraform force-unlock -force <id>  from the terragrunt cache
               directory captured during detection. No dependency resolution
               needed — the backend is already configured there.
  2. Fallback: terragrunt force-unlock -force <id> from the provider directory,
               used when the cache dir is unknown or has been cleaned up.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from .common import debug, warn, error, info

# Timeout per provider when running plan for lock detection (seconds)
LOCK_CHECK_TIMEOUT = 60

# Strings that indicate a state lock error in terraform output
LOCK_INDICATORS = [
    "Error acquiring the state lock",
    "state blob is already locked",
    "Failed to lock state",
]


def _parse_lock_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract Terraform lock info from any block of text.

    Looks for the standard "Lock Info:" block that terraform outputs when a
    lock acquisition fails:

        Lock Info:
          ID:        xxxx-xxxx-xxxx-xxxx
          Operation: OperationTypePlan
          Who:       user@hostname
          Version:   1.14.4
          Created:   2026-05-21 07:09:03 +0000 UTC

    Returns:
        Dict with id, user, operation, created — or None if no lock found
    """
    if not any(indicator in text for indicator in LOCK_INDICATORS):
        return None

    def _extract(pattern: str) -> str:
        m = re.search(pattern, text, re.MULTILINE)
        if not m:
            return ""
        value = m.group(1).strip()
        # Strip ANSI escape codes (e.g. \x1b[0m color-reset appended by terraform)
        return re.sub(r'\x1b\[[0-9;]*[mK]', '', value)

    return {
        "id":        _extract(r'\bID:\s+(.+)$'),
        "user":      _extract(r'\bWho:\s+(.+)$') or "unknown",
        "operation": _extract(r'\bOperation:\s+(.+)$') or "unknown",
        "created":   _extract(r'\bCreated:\s+(.+)$') or "unknown",
    }


def _extract_cache_dir(text: str) -> Optional[Path]:
    """
    Extract the terragrunt cache directory from plan error output.

    Terragrunt prints:
        Failed to execute "terraform plan ..." in /tmp/terragrunt_cache/abc/xyz

    That directory has .terraform/ already configured with the backend,
    so we can run 'terraform force-unlock' directly from there.
    """
    m = re.search(r'Failed to execute ".+?" in (.+?)(?:\s|$)', text, re.MULTILINE)
    if not m:
        return None
    cache_dir = Path(m.group(1).strip())
    return cache_dir if cache_dir.is_dir() else None


def check_lock(provider_dir: Path, project_root: Path) -> Optional[Dict[str, Any]]:
    """
    Detect a state lock on one provider by running 'terragrunt plan -lock-timeout=0s'.

    Also captures the terragrunt cache directory from the error output so that
    unlock_one() can run 'terraform force-unlock' directly from there, bypassing
    any dependency resolution issues.

    Returns:
        Lock info dict if locked, None if not locked or on timeout/error
    """
    debug(f"Checking lock: {provider_dir}")

    try:
        result = subprocess.run(
            ["terragrunt", "plan", "-lock-timeout=0s", "-refresh=false"],
            cwd=str(provider_dir),
            capture_output=True,
            text=True,
            timeout=LOCK_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        warn(f"Timed out checking lock for {provider_dir}")
        return None
    except FileNotFoundError:
        error("terragrunt not found in PATH")
        return None

    # Exit code 0 means plan succeeded — no lock
    if result.returncode == 0:
        return None

    combined = result.stderr + result.stdout
    lock_info = _parse_lock_from_text(combined)
    if lock_info is None:
        # Plan failed for a reason other than a lock
        debug(f"Non-lock error in {provider_dir}: {combined[:200]}")
        return None

    try:
        location = str(provider_dir.relative_to(project_root))
    except ValueError:
        location = str(provider_dir)

    lock_info["location"]      = location
    lock_info["_provider_dir"] = provider_dir
    lock_info["_cache_dir"]    = _extract_cache_dir(combined)

    debug(f"Cache dir for {location}: {lock_info['_cache_dir']}")
    return lock_info


def show_lock(lock: Dict[str, Any]) -> None:
    """Print details of one lock."""
    info(f"  {lock['location']}")
    info(f"    Locked by: {lock['user']}")
    info(f"    Operation: {lock['operation']}")
    info(f"    Created:   {lock['created']}")
    if lock["id"]:
        info(f"    Lock ID:   {lock['id']}")
    else:
        warn("    Lock ID:   (not found — cannot force-unlock)")


def unlock_one(lock: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Force-unlock a single provider's state lock.

    Strategy:
      1. If the terragrunt cache dir from detection is available, run
         'terraform force-unlock -force <id>' directly from there.
         This bypasses terragrunt dependency resolution entirely.
      2. Otherwise fall back to 'terragrunt force-unlock -force <id>'
         from the provider directory.

    Returns:
        (success: bool, message: str)
    """
    lock_id      = lock["id"]
    provider_dir = lock["_provider_dir"]
    cache_dir    = lock.get("_cache_dir")
    location     = lock["location"]

    if not lock_id:
        msg = f"Cannot unlock {location}: lock ID not found"
        warn(f"  {msg}")
        return False, msg

    # ------------------------------------------------------------------
    # Primary: terraform directly from cache dir (no dependency resolution)
    # ------------------------------------------------------------------
    if cache_dir and cache_dir.is_dir():
        cmd     = ["terraform", "force-unlock", "-force", lock_id]
        cmd_str = f"cd {cache_dir} && terraform force-unlock -force {lock_id}"
        info(f"Running: {cmd_str}")

        try:
            result = subprocess.run(
                cmd,
                cwd=str(cache_dir),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                info(f"  Unlocked: {location}")
                if result.stdout.strip():
                    debug(result.stdout.strip())
                return True, "success"
            else:
                stderr = result.stderr.strip() or result.stdout.strip()
                error(f"  Failed to unlock {location}: {stderr}")
                return False, stderr

        except FileNotFoundError:
            warn("  terraform not found in PATH, falling back to terragrunt")

    # ------------------------------------------------------------------
    # Fallback: terragrunt force-unlock from provider dir
    # ------------------------------------------------------------------
    cmd     = ["terragrunt", "force-unlock", "-force", lock_id]
    cmd_str = f"cd {provider_dir} && terragrunt force-unlock -force {lock_id}"
    info(f"Running: {cmd_str}")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(provider_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            info(f"  Unlocked: {location}")
            if result.stdout.strip():
                debug(result.stdout.strip())
            return True, "success"
        else:
            stderr = result.stderr.strip() or result.stdout.strip()
            error(f"  Failed to unlock {location}: {stderr}")
            return False, stderr

    except FileNotFoundError:
        msg = "terragrunt not found in PATH"
        error(f"  {msg}")
        return False, msg
