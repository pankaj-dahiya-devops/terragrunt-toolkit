"""
State lock discovery and removal for Terraform/Terragrunt projects.

Locks are always detected live via terragrunt — never from cached files.

Detection:
    Runs "terragrunt plan -lock-timeout=0s -refresh=false" in each provider
    directory in parallel. With -lock-timeout=0s, terraform fails IMMEDIATELY
    if it cannot acquire the lock — before doing any real planning work.

Provider scope is determined by the caller:
    - Ticket:        providers that were part of that ticket's plan run
    - Provider path: all provider directories under the given path

Unlock:
    Runs "terragrunt force-unlock -force <id>" in each locked provider
    directory in parallel. Terragrunt reads its own backend config.
"""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

from .common import info, error, is_interactive
from .project import TerraformProject
from .unlock import check_lock, unlock_one, show_lock


# =============================================================================
# PROVIDER DISCOVERY
# =============================================================================

def providers_from_ticket(ticket: str, project: TerraformProject) -> List[Path]:
    """
    Return provider directories that were part of a previous plan ticket.

    Scans tickets/{ticket}/providers/ for plan output files and maps each
    back to its provider directory under providers/.
    """
    ticket_providers_dir = project.root / "tickets" / ticket / "providers"
    if not ticket_providers_dir.exists():
        return []

    providers = []
    for txt_file in ticket_providers_dir.rglob("*.txt"):
        rel          = txt_file.relative_to(ticket_providers_dir)
        provider_rel = rel.parent
        provider_abs = project.root / "providers" / provider_rel
        if provider_abs.is_dir() and (provider_abs / "terragrunt.hcl").exists():
            if provider_abs not in providers:
                providers.append(provider_abs)

    return providers


# =============================================================================
# LOCK DISCOVERY
# =============================================================================

def find_locks(
    provider_dirs: List[Path],
    project: TerraformProject,
    concurrency: int,
) -> List[Dict[str, Any]]:
    """
    Find locked providers by running 'terragrunt plan -lock-timeout=0s' in parallel.

    Args:
        provider_dirs: Provider directories to check
        project:       TerraformProject (for display paths)
        concurrency:   Parallel workers

    Returns:
        List of lock dicts for providers that are currently locked, sorted by location
    """
    info(f"Checking {len(provider_dirs)} provider(s) for state locks...")

    locks: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(check_lock, d, project.root): d
            for d in provider_dirs
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                locks.append(result)

    return sorted(locks, key=lambda l: l["location"])


# =============================================================================
# ORCHESTRATION
# =============================================================================

def run_unlock(
    locks: List[Dict[str, Any]],
    dry_run: bool,
    force: bool,
    concurrency: int,
) -> int:
    """
    Display found locks, confirm, and force-unlock in parallel.

    Args:
        locks:       Lock dicts already discovered by find_locks()
        dry_run:     Show locks but do not unlock
        force:       Skip confirmation prompt
        concurrency: Parallel workers for unlock phase

    Returns:
        0 = success, 1 = one or more failures
    """
    if not locks:
        info("No locked providers found")
        return 0

    # Always display all found locks
    info(f"\nFound {len(locks)} locked provider(s):\n")
    for lock in locks:
        show_lock(lock)
        print()

    if dry_run:
        info("Dry run — nothing was unlocked")
        return 0

    # Always confirm unless --force
    if not force:
        if is_interactive():
            print(
                f"Enter 'yes' to force-unlock {len(locks)} provider(s): ",
                end="", flush=True,
            )
            if input().strip() != "yes":
                info("Aborted")
                return 1
        else:
            error("--force required in non-interactive mode")
            return 1

    # Unlock in parallel
    info(f"\nForce-unlocking {len(locks)} provider(s)...")
    ok_count = fail_count = 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(unlock_one, lock): lock for lock in locks}
        for future in as_completed(futures):
            ok, _ = future.result()
            if ok:
                ok_count += 1
            else:
                fail_count += 1

    info(f"\nUnlocked {ok_count} provider(s)")
    if fail_count:
        error(f"Failed to unlock {fail_count} provider(s)")
        return 1
    return 0
