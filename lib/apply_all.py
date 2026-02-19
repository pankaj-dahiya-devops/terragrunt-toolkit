"""
Parallel Terragrunt apply execution.

Handles:
- Loading plans from a ticket directory
- Running multiple applies in parallel
- Safety checks for production applies
- Confirmation prompts for destroy plans

# =============================================================================
# PYTHON CONCEPTS FOR GO/BASH DEVELOPERS
# =============================================================================
#
# This file demonstrates these Python concepts (similar to plan_all.py):
#
# 1. TYPE_CHECKING Import Guard:
#    - Prevents circular imports at runtime
#    - Types inside "if TYPE_CHECKING:" are only loaded for type checkers
#    - Go equivalent: Interface segregation to avoid import cycles
#
# 2. ThreadPoolExecutor:
#    - Python's high-level threading abstraction
#    - Similar to Go's goroutines + sync.WaitGroup but with thread reuse
#    - The 'with' statement ensures proper cleanup (like Go's defer)
#
# 3. as_completed() Iterator:
#    - Yields futures as they complete (not in submission order)
#    - Like reading from a Go channel where workers send results as done
#
# 4. threading.Event:
#    - Thread-safe flag for signaling between threads
#    - .set() turns it on, .is_set() checks it, .clear() resets it
#    - Like Go's sync.Cond or a done channel
#
# 5. Signal Handling:
#    - signal.signal() registers handlers (like Go's signal.Notify)
#    - Different from Bash where you use 'trap' command
#    - Windows doesn't support SIGTERM, hence platform check
#
# 6. subprocess.run():
#    - Executes external commands like os/exec in Go or $() in Bash
#    - capture_output=True captures stdout/stderr
#    - cwd= sets working directory
#
# =============================================================================
"""

# =============================================================================
# STANDARD LIBRARY IMPORTS
# =============================================================================

import os              # Operating system interface (environment variables, paths)
import re              # Regular expressions (for parsing dependency blocks)
import sys             # System-specific parameters (exit, platform detection)
import subprocess      # Running external commands (git, terraform, terragrunt)
import signal          # Signal handling for graceful shutdown (SIGINT, SIGTERM)
import threading       # Threading primitives (Event for cancellation signaling)
from pathlib import Path           # Object-oriented filesystem paths
from datetime import datetime      # Date/time for tracking apply duration
from concurrent.futures import ThreadPoolExecutor, as_completed  # Thread pool for parallel applies
from typing import Optional, List, Dict, Any, TYPE_CHECKING      # Type hints for better code clarity

# =============================================================================
# LOCAL MODULE IMPORTS
# =============================================================================

# Import shared constants, enums, and utility functions from common.py
from .common import (
    CHANGE_TYPES_PAST,           # List of past-tense change types: ["added", "changed", "destroyed"]
    ERROR_PLANS_HEADER,          # Header string for error plans section
    NO_CHANGE_PLANS_HEADER,      # Header string for no-change plans section
    SUCCESSFUL_PLANS_HEADER,     # Header string for successful plans section
    PlanStatus,                  # Enum with values: PENDING, SUCCESS, NOCHANGE, ERROR
    debug,                       # Print debug messages (only if verbose enabled)
    info,                        # Print informational messages
    warn,                        # Print warning messages (yellow)
    error,                       # Print error messages (red)
    is_interactive,              # Check if running in interactive terminal (for prompts)
)

# Import the single apply class that handles one terragrunt apply
from .apply import TerragruntApply

# =============================================================================
# TYPE_CHECKING IMPORT GUARD
# =============================================================================
#
# This import is ONLY evaluated when running type checkers (mypy, pyright, etc).
# At runtime, TYPE_CHECKING is False, so this code doesn't execute.
#
# WHY IS THIS NEEDED?
# - TerraformProject imports from this module's parent
# - If we imported it normally, we'd have a circular import:
#   project.py -> apply_all.py -> project.py (ERROR!)
#
# HOW IT WORKS:
# 1. At runtime: TYPE_CHECKING = False, import skipped, no circular import
# 2. For type checkers: TYPE_CHECKING = True, import runs for type info
# 3. We use quotes around "TerraformProject" (forward reference)
#
# Go equivalent would be to define an interface in a separate package
# that both modules can import without depending on each other.
# =============================================================================
if TYPE_CHECKING:
    from .project import TerraformProject


# =============================================================================
# TerragruntApplyAll CLASS
# =============================================================================
#
# This class orchestrates running multiple terragrunt applies in parallel.
# It's similar to TerragruntPlanAll but with extra safety checks for
# production environments and destroy operations.
#
# COMPARISON TO GO:
# In Go, you'd likely have a struct with methods:
#
#   type ApplyAll struct {
#       project      *TerraformProject
#       ticket       string
#       cmdOptions   map[string]interface{}
#       plans        []*Apply
#       cancelEvent  chan struct{}
#       // ...
#   }
#
#   func (a *ApplyAll) Run() error { ... }
#   func (a *ApplyAll) LoadPlans() error { ... }
#
# =============================================================================

class TerragruntApplyAll:
    """
    Orchestrates running multiple terragrunt applies in parallel.

    Manages a collection of TerragruntApply instances, runs them
    concurrently with safety checks for production environments.

    WORKFLOW:
    1. load_plans() - Scans ticket directory for plan files
    2. show_plans() - Displays what will be applied
    3. check_branch() - Verifies git status for production safety
    4. run() - Executes all applies in parallel
    5. generate_summary() - Shows final results

    SAFETY FEATURES:
    - Production branch check (warns if not on main/master)
    - Uncommitted changes detection
    - Destroy plan confirmation (requires interactive YES)
    - Account ordering (dev before prod)
    """

    # =========================================================================
    # CONSTRUCTOR (__init__)
    # =========================================================================
    #
    # Python's __init__ is called automatically when you create an instance.
    # It's like Go's NewApplyAll() constructor function but built into the class.
    #
    # The 'self' parameter is like Go's receiver (a *ApplyAll) - it refers
    # to the instance being created/modified.
    # =========================================================================

    def __init__(
        self,
        project: "TerraformProject",   # Quoted because of TYPE_CHECKING guard
        ticket: str,                    # JIRA ticket number (e.g., "INFRA-1234")
        cmd_options: Dict[str, Any],    # Command-line options as dictionary
    ):
        """
        Initialize the apply-all operation.

        Args:
            project: The TerraformProject instance with root path and git info
            ticket: JIRA ticket number (e.g., "INFRA-1234")
            cmd_options: Command-line options dictionary containing:
                - concurrency: Number of parallel applies (default: 2)
                - retry: "all", "errors", or "none"
                - allow_destroy: Whether to allow destroy plans
                - stop_on_error: Stop after first failure
                - refresh: Force state refresh before apply
                - cache: Keep terragrunt cache
                - force: Allow production changes from non-main branch
                - dry_run: Preview mode (don't actually apply)
                - verbose: Enable verbose output
        """
        # Store references to project and options
        self.project = project         # TerraformProject instance
        self.ticket = ticket           # JIRA ticket number
        self.cmd_options = cmd_options # Command-line options

        # Build path to ticket directory
        # Path("tickets") / ticket creates: tickets/INFRA-1234
        # This is like filepath.Join("tickets", ticket) in Go
        self.ticketdir = Path("tickets") / ticket

        # List to store TerragruntApply instances (the plans to apply)
        # Type hint: List[TerragruntApply] tells the type checker what's in the list
        # In Go, this would be: plans []*TerragruntApply
        self.plans: List[TerragruntApply] = []

        # Counter for interrupt signals (Ctrl+C)
        # Used to implement progressive shutdown:
        # - 1st interrupt: graceful shutdown, wait for threads
        # - 2nd interrupt: warning
        # - 3rd interrupt: immediate exit
        self._interrupt_count = 0

        # Thread-safe event for signaling cancellation to all worker threads
        # threading.Event is like Go's sync.Cond or a done channel
        # - .set() turns the flag on
        # - .is_set() checks if the flag is on
        # - .clear() resets the flag
        self._cancel_event = threading.Event()

        # Timestamps for tracking apply duration
        # Optional[datetime] means "datetime or None"
        # In Go: starttime *time.Time (pointer to allow nil)
        self.starttime: Optional[datetime] = None
        self.endtime: Optional[datetime] = None

    # =========================================================================
    # __str__ METHOD (String Representation)
    # =========================================================================
    #
    # Special method that defines how the object looks when converted to string.
    # Called when you use str(obj), print(obj), or f-string formatting.
    #
    # Go equivalent: implementing the Stringer interface:
    #   func (a *ApplyAll) String() string { return a.ticket }
    # =========================================================================

    def __str__(self) -> str:
        """Return string representation (the ticket number)."""
        return self.ticket

    # =========================================================================
    # LOAD PLANS METHOD
    # =========================================================================
    #
    # Scans the ticket directory for plan files and creates TerragruntApply
    # instances for each one that should be applied.
    #
    # Plan files are organized like:
    #   tickets/INFRA-1234/providers/dev/us-east-1/my-service/my-service.txt
    #
    # This method handles:
    # - Finding all .txt plan files (excluding .apply result files)
    # - Checking retry options (skip already-applied, retry errors, etc.)
    # - Handling destroy plans (require confirmation)
    # =========================================================================

    def load_plans(self) -> None:
        """
        Scan ticket directory for plans to apply.

        Loads plan files from tickets/<ticket>/providers/**/*.txt
        and creates TerragruntApply instances for each.

        FILTERING LOGIC based on --retry option:
        - retry=None (default): Apply only pending plans, warn about others
        - retry="none": Same as None but no warnings
        - retry="errors": Only retry previously failed applies
        - retry="all": Retry all plans including successful ones

        DESTROY PLAN HANDLING:
        - Destroy plans require --allow-destroy flag
        - If --allow-destroy is set, requires interactive YES confirmation
        - This prevents accidental destruction of resources
        """
        # Separate list for destroy plans (need special handling)
        destroy_plans: List[TerragruntApply] = []

        # Get the retry option from command-line args
        # Can be: None, "none", "errors", "all"
        retry_option = self.cmd_options.get("retry")

        # =====================================================================
        # GLOB PATTERN MATCHING
        # =====================================================================
        #
        # self.ticketdir.glob("providers/**/*.txt") finds all .txt files
        # under the providers/ subdirectory, recursively.
        #
        # The ** means "any number of directories"
        # So "providers/**/*.txt" matches:
        #   - providers/dev/us-east-1/svc/svc.txt
        #   - providers/prod/eu-west-1/app/app.txt
        #
        # Go equivalent: filepath.Walk() or filepath.Glob()
        # Bash equivalent: find tickets/$TICKET/providers -name "*.txt"
        # =====================================================================

        # Find all plan files in the ticket directory
        for plan_file in self.ticketdir.glob("providers/**/*.txt"):
            # -----------------------------------------------------------------
            # SKIP APPLY RESULT FILES
            # -----------------------------------------------------------------
            # After applying, results are written to <planfile>.apply
            # e.g., my-service.txt.apply
            # We don't want to process these as plans
            # -----------------------------------------------------------------
            if plan_file.suffix == ".apply" or ".txt.apply" in str(plan_file):
                continue

            # -----------------------------------------------------------------
            # EXTRACT PROVIDER PATH
            # -----------------------------------------------------------------
            # plan_file: tickets/INFRA-1234/providers/dev/us-east-1/svc/svc.txt
            # plan_file.parent: tickets/INFRA-1234/providers/dev/us-east-1/svc
            # .relative_to(self.ticketdir): providers/dev/us-east-1/svc
            #
            # This gives us the provider path relative to the ticket directory
            # -----------------------------------------------------------------
            provider = str(plan_file.parent.relative_to(self.ticketdir))

            # -----------------------------------------------------------------
            # CREATE APPLY INSTANCE
            # -----------------------------------------------------------------
            # TerragruntApply handles a single apply operation
            # It knows how to run terragrunt apply and parse the output
            # -----------------------------------------------------------------
            apply_op = TerragruntApply(
                self.project, self.ticket, provider, self.cmd_options
            )

            # -----------------------------------------------------------------
            # CHECK RETRY OPTIONS
            # -----------------------------------------------------------------
            # Determine whether this plan should be applied based on its
            # previous status and the --retry command-line option
            # -----------------------------------------------------------------

            if retry_option in [None, "none"]:
                # Default behavior: only apply plans that haven't been tried

                if apply_op.failed():
                    # Plan was tried before and failed
                    if retry_option is None:
                        # Show warning unless --retry none was explicit
                        warn(f"Plan {apply_op} failed previously; --retry errors is required to reapply")
                    continue  # Skip this plan

                elif apply_op.applied():
                    # Plan was already applied successfully
                    if retry_option is None:
                        # Show warning unless --retry none was explicit
                        warn(f"Plan {apply_op} was applied previously; --retry all is required to reapply")
                    continue  # Skip this plan

            elif retry_option == "errors":
                # Only retry plans that failed before
                if not apply_op.failed():
                    continue  # Skip plans that didn't fail

            elif retry_option == "all":
                # Retry all plans regardless of previous status
                pass  # No filtering needed

            else:
                # Unexpected option value - this shouldn't happen
                raise ValueError(f"Unexpected --retry option: {retry_option}")

            # -----------------------------------------------------------------
            # HANDLE DESTROY PLANS
            # -----------------------------------------------------------------
            # Plans generated with --destroy require special confirmation
            # This prevents accidental deletion of infrastructure
            # -----------------------------------------------------------------

            if apply_op.destroy:
                # This is a destroy plan
                if self.cmd_options.get("allow_destroy"):
                    # User explicitly allowed destroy with --allow-destroy flag
                    destroy_plans.append(apply_op)
                else:
                    # Destroy not allowed - warn and skip
                    warn(f"Plan {apply_op} was generated with --destroy; must be applied with --allow-destroy!")
                    continue

            # Add this plan to the list of plans to apply
            self.plans.append(apply_op)

        # =====================================================================
        # DESTROY PLAN CONFIRMATION
        # =====================================================================
        #
        # If there are destroy plans and --allow-destroy was set, we need
        # interactive confirmation before proceeding. This is a safety measure
        # to prevent accidental infrastructure destruction.
        #
        # The user must type "YES" (all caps) to confirm.
        # =====================================================================

        if destroy_plans and self.cmd_options.get("allow_destroy"):
            warn(f"{len(destroy_plans)} destroy plans found and --allow-destroy is set")

            # Show list of destroy plans
            for plan in destroy_plans:
                info(f"  {plan}")

            # -----------------------------------------------------------------
            # INTERACTIVE CONFIRMATION
            # -----------------------------------------------------------------
            # is_interactive() checks if stdin is connected to a terminal
            # (not piped or redirected). We need real user input for safety.
            #
            # print(..., end="", flush=True) prints without newline and
            # immediately flushes the buffer so prompt appears before input()
            # -----------------------------------------------------------------

            if is_interactive():
                print("Type YES (all caps) to confirm: ", end="", flush=True)
                response = input().strip()  # Read user input and remove whitespace
                if response != "YES":
                    error("Aborted")
                    sys.exit(1)  # Exit with error code
            else:
                # Non-interactive mode (piped input) - can't safely confirm
                error("Interactive confirmation required for destroy plans")
                sys.exit(1)

    # =========================================================================
    # SHOW PLANS METHOD
    # =========================================================================
    #
    # Displays the list of plans that will be applied and records start time.
    # Plans are sorted by account/region for consistent ordering.
    # =========================================================================

    def show_plans(self) -> None:
        """
        Display list of plans that will be applied.

        This method:
        1. Records the start time
        2. Sorts plans by sort_key (account type, then region)
        3. Displays each plan path
        4. Shows total count
        """
        # Record start time for duration calculation later
        self.starttime = datetime.now()
        info(f"Apply started at {self.starttime}")

        if not self.plans:
            # No plans to apply - could happen with filtering or retry options
            info("No matching plans to apply!")
        else:
            # -----------------------------------------------------------------
            # SORT PLANS BY ACCOUNT/REGION
            # -----------------------------------------------------------------
            # Plans are sorted using their sort_key property which returns
            # a tuple like (0, "us-east-1") for dev or (1, "us-east-1") for prod
            #
            # This ensures:
            # - Dev accounts are applied before prod (safer)
            # - Within same account type, sorted by region (consistent)
            #
            # list.sort() with key= is like Go's sort.Slice() with a less function
            # The lambda function extracts the sort key from each plan
            # -----------------------------------------------------------------
            self.plans.sort(key=lambda p: p.sort_key)

            # Display each plan that will be applied
            for plan in self.plans:
                info(f"  {plan}")

            # Show summary count
            info(f"Apply: {self.ticketdir}, {len(self.plans)} providers")

    # =========================================================================
    # CHECK BRANCH METHOD (Production Safety)
    # =========================================================================
    #
    # Performs safety checks before applying production plans.
    # Returns a list of warning messages about potential issues.
    #
    # CHECKS PERFORMED:
    # 1. Are there uncommitted changes?
    # 2. Are we on main/master branch?
    # 3. Is local branch in sync with remote?
    #
    # These checks help prevent applying production changes from:
    # - Uncommitted code (might have local debugging changes)
    # - Feature branches (changes not reviewed/merged)
    # - Out-of-sync branches (might be missing important changes)
    # =========================================================================

    def check_branch(self) -> List[str]:
        """
        Extra safety checks before applying production plans.

        Returns a list of warning messages if there are issues.
        Empty list means all checks passed.

        IMPORTANT: These are warnings, not blockers (unless --force is not set).
        The calling code decides whether to proceed based on these warnings.
        """
        messages: List[str] = []

        # =====================================================================
        # CHECK IF ANY PLANS ARE FOR PRODUCTION
        # =====================================================================
        #
        # any() is a Python built-in that returns True if any element is truthy.
        # It's like Go's: for _, p := range plans { if p.Account == "prod" { return true } }
        #
        # The expression "p.account == 'prod' for p in self.plans" is a
        # generator expression - it lazily evaluates each plan's account.
        # =====================================================================

        if not any(p.account == "prod" for p in self.plans):
            # No production plans - no safety checks needed
            return messages

        # =====================================================================
        # CHECK FOR UNCOMMITTED CHANGES
        # =====================================================================
        #
        # git status --porcelain outputs a machine-readable format:
        # - Empty output = clean working directory
        # - Non-empty output = there are changes
        #
        # subprocess.run() is like os/exec.Command().Run() in Go
        # capture_output=True captures stdout/stderr for inspection
        # text=True returns string instead of bytes
        # =====================================================================

        result = subprocess.run(
            ["git", "status", "--porcelain"],  # Command and arguments as list
            cwd=self.project.root,              # Run in project root directory
            capture_output=True,                # Capture stdout and stderr
            text=True,                          # Return strings, not bytes
        )

        if result.stdout.strip():
            # Non-empty output means there are uncommitted changes
            messages.append(f"You have uncommitted changes in {self.project.root}")

        # =====================================================================
        # CHECK GIT BRANCH STATUS
        # =====================================================================
        #
        # This section determines if we're on main/master and if we're
        # in sync with the remote branch.
        #
        # Git references explained:
        # - @ is shorthand for HEAD (current commit)
        # - @{u} or @{upstream} is the upstream tracking branch
        # - git merge-base finds the common ancestor
        #
        # Sync states:
        # - local == remote: Up to date
        # - local == base:   Behind remote (need to pull)
        # - remote == base:  Ahead of remote (need to push)
        # - neither:         Diverged (need to merge)
        # =====================================================================

        # Get current branch name
        current_branch = self.project.current_git_branch()

        if current_branch in ["master", "main"]:
            # We're on the main branch - check if in sync with remote

            # First, update remote refs to get latest state
            subprocess.run(
                ["git", "remote", "update"],
                cwd=self.project.root,
                capture_output=True,  # Suppress output
            )

            # Get local HEAD commit hash
            result = subprocess.run(
                ["git", "rev-parse", "@"],  # @ = HEAD
                cwd=self.project.root,
                capture_output=True,
                text=True,
            )
            local = result.stdout.strip()

            # Get remote tracking branch commit hash
            result = subprocess.run(
                ["git", "rev-parse", "@{u}"],  # @{u} = upstream
                cwd=self.project.root,
                capture_output=True,
                text=True,
            )
            remote = result.stdout.strip()

            # Get merge base (common ancestor) commit hash
            result = subprocess.run(
                ["git", "merge-base", "@", "@{u}"],
                cwd=self.project.root,
                capture_output=True,
                text=True,
            )
            base = result.stdout.strip()

            # Determine sync state by comparing hashes
            if local == remote:
                pass  # Up to date - no warning needed

            elif local == base:
                # -----------------------------------------------------------------
                # BEHIND REMOTE
                # -----------------------------------------------------------------
                # Local branch is at the merge base, meaning remote has commits
                # that local doesn't have. Need to pull.
                #
                # Git log shows commits in remote that aren't in local
                # -----------------------------------------------------------------
                result = subprocess.run(
                    ["git", "log", f"{local}..{remote}", "--pretty=oneline"],
                    cwd=self.project.root,
                    capture_output=True,
                    text=True,
                )
                # Count commits by counting non-empty lines
                commit_count = len(result.stdout.strip().split("\n"))
                messages.append(f"You are behind origin master|main by {commit_count} commits")

            elif remote == base:
                # -----------------------------------------------------------------
                # AHEAD OF REMOTE
                # -----------------------------------------------------------------
                # Remote is at the merge base, meaning local has commits
                # that remote doesn't have. These changes aren't pushed yet.
                # -----------------------------------------------------------------
                result = subprocess.run(
                    ["git", "log", f"{remote}..{local}", "--pretty=oneline"],
                    cwd=self.project.root,
                    capture_output=True,
                    text=True,
                )
                commit_count = len(result.stdout.strip().split("\n"))
                messages.append(f"You are ahead of origin master|main by {commit_count} commits")

            else:
                # -----------------------------------------------------------------
                # DIVERGED
                # -----------------------------------------------------------------
                # Neither local nor remote is at the merge base.
                # Both have commits the other doesn't have. Need to merge/rebase.
                # -----------------------------------------------------------------
                messages.append("You have diverged from origin master|main")

        else:
            # -----------------------------------------------------------------
            # NOT ON MAIN/MASTER BRANCH
            # -----------------------------------------------------------------
            # Applying production changes from a feature branch is risky
            # because the changes haven't been reviewed/merged
            # -----------------------------------------------------------------
            messages.append(f"You are on branch {current_branch}, not master|main")

        return messages

    # =========================================================================
    # EMPTY CHECK
    # =========================================================================

    def empty(self) -> bool:
        """
        Check if there are no plans to apply.

        Returns True if the plans list is empty.
        """
        return len(self.plans) == 0

    # =========================================================================
    # RUN METHOD (Main Apply Execution)
    # =========================================================================
    #
    # This is the main method that executes all applies in parallel.
    # It uses ThreadPoolExecutor for concurrent execution.
    #
    # COMPARISON TO GO:
    # In Go, you'd use goroutines and sync.WaitGroup:
    #
    #   var wg sync.WaitGroup
    #   semaphore := make(chan struct{}, concurrency)
    #   results := make(chan Result)
    #
    #   for _, plan := range plans {
    #       wg.Add(1)
    #       go func(p *Plan) {
    #           defer wg.Done()
    #           semaphore <- struct{}{}  // Acquire
    #           result := runApply(p)
    #           <-semaphore              // Release
    #           results <- result
    #       }(plan)
    #   }
    #
    # Python's ThreadPoolExecutor handles the semaphore/worker pool internally.
    # =========================================================================

    def run(self) -> None:
        """
        Run all applies in dependency-aware waves, with parallelism within each wave.

        This method:
        1. Parses dependency blocks from each provider's terragrunt.hcl
        2. Groups providers into waves (topological sort):
           - Wave 1: providers with no deps on other providers in this run
           - Wave 2: providers whose deps are all in wave 1
           - Wave N: providers whose deps are all in earlier waves
        3. Runs each wave in parallel (up to --concurrency N threads)
        4. Stops subsequent waves on cancellation or error (if --stop-on-error)

        EXAMPLE with: vpc, security-groups (depends on vpc), rds (depends on vpc):
            Wave 1 (parallel): vpc
            Wave 2 (parallel): security-groups, rds

        If all providers are independent (no dependency blocks), everything
        runs in a single wave — identical to the old behavior.
        """
        # Set up signal handlers and save original handlers for restoration
        original_handlers = self._setup_signal_handlers()

        try:
            concurrency = self.cmd_options.get("concurrency", 2)
            stop_on_error = self.cmd_options.get("stop_on_error", False)

            # -----------------------------------------------------------------
            # BUILD DEPENDENCY-AWARE EXECUTION WAVES
            # -----------------------------------------------------------------
            # Parse dependency { config_path = "..." } blocks from each provider
            dep_map = {}
            for apply_op in self.plans:
                dep_map[apply_op.provider] = _parse_provider_deps(
                    self.project, apply_op.provider
                )

            # Group plans into waves via topological sort
            # Each wave's plans have no unmet dependencies on each other
            waves = _build_waves(self.plans, dep_map)

            # Log wave breakdown if more than one wave (i.e., there are deps)
            if len(waves) > 1:
                info(f"Dependency ordering: {len(waves)} waves")
                for i, wave in enumerate(waves, 1):
                    providers = ", ".join(str(p) for p in wave)
                    info(f"  Wave {i}: {providers}")

            # -----------------------------------------------------------------
            # EXECUTE WAVE BY WAVE
            # -----------------------------------------------------------------
            #
            # Each wave uses a fresh ThreadPoolExecutor.
            # The executor's 'with' block waits for all threads to finish
            # before starting the next wave — this is the key to ordering.
            #
            # Go equivalent:
            #   for _, wave := range waves {
            #       var wg sync.WaitGroup
            #       sem := make(chan struct{}, concurrency)
            #       for _, plan := range wave {
            #           wg.Add(1)
            #           go func(p *Plan) {
            #               defer wg.Done()
            #               sem <- struct{}{}; runApply(p); <-sem
            #           }(plan)
            #       }
            #       wg.Wait()  // Wait for wave to complete before next wave
            #   }
            # -----------------------------------------------------------------

            for wave_num, wave in enumerate(waves, 1):
                # Stop if a previous wave triggered cancellation
                if self._cancel_event.is_set():
                    break

                if len(waves) > 1:
                    info(f"Wave {wave_num}/{len(waves)}: applying {len(wave)} providers")

                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    # Submit all applies in this wave to the thread pool
                    futures = {
                        executor.submit(self._run_single_apply, apply_op): apply_op
                        for apply_op in wave
                    }

                    # Process results as they complete (not in submission order)
                    for future in as_completed(futures):
                        if self._cancel_event.is_set():
                            break

                        apply_op = futures[future]

                        try:
                            future.result()
                        except Exception as e:
                            error(f"{apply_op}: {e}")

                        self._apply_finish(apply_op, stop_on_error)

                        if stop_on_error and apply_op.status == PlanStatus.ERROR:
                            self._cancel_all()
                            break

                # Stop subsequent waves if cancelled or stop-on-error triggered
                if self._cancel_event.is_set():
                    break

        finally:
            # Always restore signal handlers and record end time
            self._restore_signal_handlers(original_handlers)
            self.endtime = datetime.now()

    # =========================================================================
    # PRIVATE HELPER METHODS
    # =========================================================================
    #
    # Methods starting with _ are conventionally "private" in Python.
    # This is just a naming convention - Python doesn't enforce access control.
    # In Go, you'd use lowercase names for unexported functions.
    # =========================================================================

    def _run_single_apply(self, apply_op: TerragruntApply) -> None:
        """
        Run a single apply (called from worker thread).

        This method is executed in a thread pool worker.
        It simply delegates to the TerragruntApply.run() method.

        Args:
            apply_op: The TerragruntApply instance to run
        """
        debug(f"{apply_op}: Starting")
        apply_op.run()  # Execute the apply (handles its own error catching)

    def _apply_finish(self, apply_op: TerragruntApply, stop_on_error: bool) -> None:
        """
        Handle apply completion.

        Logs the result of the apply operation.

        Args:
            apply_op: The completed TerragruntApply instance
            stop_on_error: Whether to stop on first error (used for logging context)
        """
        debug(f"{apply_op}: Finished")

        if apply_op.status == PlanStatus.ERROR:
            # -----------------------------------------------------------------
            # ERROR HANDLING
            # -----------------------------------------------------------------
            # chr(10) is the newline character '\n'
            # We use chr(10) because f-strings don't allow backslashes
            # .join() combines errors list with newlines
            # -----------------------------------------------------------------
            error(f"{apply_op}: {chr(10).join(apply_op.errors)}")
        else:
            # Success or no-change - show summary
            info(f"{apply_op}: {apply_op.summary}")

    # =========================================================================
    # SIGNAL HANDLING METHODS
    # =========================================================================
    #
    # These methods handle Unix signals (SIGINT from Ctrl+C, SIGTERM from kill).
    # They implement graceful shutdown with progressive escalation.
    #
    # COMPARISON TO GO:
    #
    #   sigChan := make(chan os.Signal, 1)
    #   signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
    #
    #   go func() {
    #       for sig := range sigChan {
    #           // Handle signal
    #       }
    #   }()
    #
    # COMPARISON TO BASH:
    #
    #   trap 'handle_signal SIGINT' SIGINT
    #   trap 'handle_signal SIGTERM' SIGTERM
    # =========================================================================

    def _setup_signal_handlers(self) -> Dict[int, Any]:
        """
        Set up signal handlers for graceful shutdown.

        Returns:
            Dictionary mapping signal number to original handler.
            Used to restore handlers later.

        NOTE: Windows doesn't support SIGTERM, so we only set up handlers
        on Unix-like systems (Linux, macOS).
        """
        original_handlers = {}

        # Windows doesn't have SIGTERM, only handle signals on Unix
        if sys.platform != "win32":
            signals = [signal.SIGINT, signal.SIGTERM]
            for sig in signals:
                # signal.signal() returns the previous handler
                # We save it so we can restore it later
                original_handlers[sig] = signal.signal(sig, self._signal_handler)

        return original_handlers

    def _restore_signal_handlers(self, original_handlers: Dict[int, Any]) -> None:
        """
        Restore original signal handlers.

        Args:
            original_handlers: Dictionary from _setup_signal_handlers()
        """
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """
        Handle interrupt signals (SIGINT, SIGTERM).

        Implements progressive shutdown:
        - 1st signal: Graceful shutdown, cancel pending applies
        - 2nd signal: Warning, one more will force exit
        - 3rd signal: Immediate exit with code 1

        Args:
            signum: The signal number (e.g., 2 for SIGINT)
            frame: The current stack frame (unused but required by signal API)
        """
        self._interrupt_count += 1

        # Get human-readable signal name (e.g., "SIGINT")
        signame = signal.Signals(signum).name

        if self._interrupt_count == 1:
            # First interrupt - start graceful shutdown
            warn(f"Caught {signame}, waiting for threads to exit")
            self._cancel_all()
        elif self._interrupt_count == 2:
            # Second interrupt - warn user
            warn(f"Caught {signame}, press Ctrl-C again to exit immediately")
        else:
            # Third interrupt - force exit
            warn(f"Caught {signame}, exiting now")
            sys.exit(1)

    def _cancel_all(self) -> None:
        """
        Signal all applies to cancel.

        Sets the cancel event (for thread pool monitoring) and
        sets the cancel flag on each apply operation.
        """
        # Set the event (thread-safe flag)
        self._cancel_event.set()

        # Set cancel flag on each apply operation
        # The apply operations check this flag and stop early if set
        for apply_op in self.plans:
            apply_op.cancel = True

    # =========================================================================
    # STATUS CHECK METHODS
    # =========================================================================

    def success(self) -> bool:
        """
        Check if apply was successful (no errors).

        Returns True if:
        - No interrupts occurred
        - No applies have ERROR status
        """
        return self._interrupt_count == 0 and not self.plans_by_status(PlanStatus.ERROR)

    def plans_by_status(self, status: Optional[str]) -> List[TerragruntApply]:
        """
        Get applies filtered by status.

        Args:
            status: The PlanStatus value to filter by (e.g., PlanStatus.ERROR)

        Returns:
            List of TerragruntApply instances matching the status

        PYTHON CONCEPT - List Comprehension:
        [p for p in self.plans if p.status == status]

        This creates a new list containing only items that match the condition.

        Go equivalent:
            var result []*TerragruntApply
            for _, p := range plans {
                if p.Status == status {
                    result = append(result, p)
                }
            }
            return result
        """
        return [p for p in self.plans if p.status == status]

    # =========================================================================
    # SUMMARY GENERATION
    # =========================================================================
    #
    # Outputs a formatted summary of all apply results, grouped by status.
    # Shows timing information and resource change counts.
    # =========================================================================

    def generate_summary(self) -> None:
        """
        Output results of apply all.

        Displays:
        1. Total duration
        2. Error plans section
        3. No-change plans section
        4. Successful plans with resource counts
        5. Overall statistics
        6. Final success/failure status
        """
        # Calculate duration in seconds
        # timestamp() returns Unix timestamp as float
        duration = (self.endtime.timestamp() - self.starttime.timestamp()) if self.endtime and self.starttime else 0
        info(f"Apply ran from {self.starttime} to {self.endtime} ({int(duration)} seconds)")

        # -----------------------------------------------------------------
        # ERROR PLANS SECTION
        # -----------------------------------------------------------------
        info(f"\n{ERROR_PLANS_HEADER}\n")
        for apply_op in self.plans_by_status(PlanStatus.ERROR):
            info(apply_op.statusline())

        # -----------------------------------------------------------------
        # NO CHANGE PLANS SECTION
        # -----------------------------------------------------------------
        info(f"\n{NO_CHANGE_PLANS_HEADER}\n")
        for apply_op in self.plans_by_status(PlanStatus.NOCHANGE):
            info(apply_op.statusline())

        # -----------------------------------------------------------------
        # SUCCESSFUL PLANS SECTION WITH COUNTS
        # -----------------------------------------------------------------
        # Count total resources changed across all successful applies

        # Initialize counters for each change type
        # Dict comprehension creates: {"added": 0, "changed": 0, "destroyed": 0}
        counts = {t: 0 for t in CHANGE_TYPES_PAST}

        info(f"\n{SUCCESSFUL_PLANS_HEADER}\n")
        for apply_op in self.plans_by_status(PlanStatus.SUCCESS):
            info(apply_op.statusline())
            # Accumulate counts from each apply
            for change_type, count in apply_op.counts.items():
                counts[change_type] += count

        # -----------------------------------------------------------------
        # OVERALL STATISTICS
        # -----------------------------------------------------------------
        info("\nApply all summary:")

        # Show non-zero resource counts
        for change_type in CHANGE_TYPES_PAST:
            if counts[change_type] > 0:
                # :2d formats as 2-digit number (padded with space)
                info(f"{counts[change_type]:2d} total resources {change_type}")

        # Show plan status counts
        info(f"  {len(self.plans_by_status(PlanStatus.SUCCESS)):2d} successful plans")
        info(f"  {len(self.plans_by_status(PlanStatus.NOCHANGE)):2d} no change plans")
        info(f"  {len(self.plans_by_status(PlanStatus.ERROR)):2d} error plans")

        # -----------------------------------------------------------------
        # FINAL STATUS MESSAGE
        # -----------------------------------------------------------------
        if self.success():
            info(f"Apply finished successfully at {self.endtime}")
        else:
            error(f"Apply finished with errors at {self.endtime}")


# =============================================================================
# MODULE-LEVEL HELPER FUNCTIONS
# =============================================================================


def _parse_provider_deps(project: "TerraformProject", provider: str) -> List[str]:
    """
    Parse dependency config_paths from a provider's terragrunt.hcl.

    Reads the terragrunt.hcl file and extracts all dependency { config_path = "..." }
    declarations, returning them as normalized provider paths relative to project root.

    Args:
        project: The TerraformProject instance (provides root path)
        provider: Provider path relative to project root
                  (e.g., "providers/dev/us-west-2/security-groups")

    Returns:
        List of provider paths this provider depends on.
        Example: ["providers/dev/us-west-2/vpc"]

    HOW IT WORKS:
        HCL dependency blocks look like:
            dependency "vpc" {
                config_path = "../vpc"
                mock_outputs = {       # nested block — handled by brace depth tracking
                    vpc_id = "vpc-mock"
                }
            }

        We track brace depth to correctly handle nested blocks (mock_outputs, etc.)
        and extract only the config_path from top-level dependency blocks.

    Go equivalent:
        func parseProviderDeps(root, provider string) []string {
            // Read and parse terragrunt.hcl, return dep paths
        }

    Bash equivalent:
        grep -A5 'dependency "' terragrunt.hcl | grep config_path | ...
    """
    hcl_file = project.root / provider / "terragrunt.hcl"
    if not hcl_file.exists():
        return []

    deps = []
    in_dependency_block = False
    brace_depth = 0
    config_path_value = None

    try:
        content = hcl_file.read_text()
    except OSError:
        return []

    for line in content.splitlines():
        stripped = line.strip()

        if not in_dependency_block:
            # Look for start of a dependency block: dependency "name" {
            # The { must be on the same line (standard HCL style)
            if re.match(r'dependency\s+"[^"]+"\s*\{', stripped):
                in_dependency_block = True
                brace_depth = 1          # Count the opening { on this line
                config_path_value = None

        else:
            # Count brace depth for nested blocks (e.g., mock_outputs = {...})
            # Each { increments, each } decrements
            brace_depth += stripped.count('{') - stripped.count('}')

            # Extract config_path if present on this line
            cp_match = re.match(r'config_path\s*=\s*"([^"]+)"', stripped)
            if cp_match:
                config_path_value = cp_match.group(1)

            # When brace_depth reaches 0, we've closed the dependency block
            if brace_depth <= 0:
                if config_path_value:
                    try:
                        # Resolve config_path relative to the provider directory.
                        # Example:
                        #   provider = "providers/dev/us-west-2/security-groups"
                        #   config_path = "../vpc"
                        #   abs = project_root/providers/dev/us-west-2/vpc (after resolve)
                        #   rel = "providers/dev/us-west-2/vpc"
                        abs_path = (project.root / provider / config_path_value).resolve()
                        rel_path = str(abs_path.relative_to(project.root.resolve()))
                        deps.append(rel_path)
                        debug(f"  {provider}: dependency -> {rel_path}")
                    except ValueError:
                        # Path resolves outside project root — ignore
                        pass

                in_dependency_block = False
                config_path_value = None

    return deps


def _build_waves(
    plans: List[TerragruntApply],
    dep_map: Dict[str, List[str]],
) -> List[List[TerragruntApply]]:
    """
    Group plans into execution waves using topological sort (Kahn's algorithm).

    Plans in the same wave have no dependencies on each other and can run
    in parallel. Waves must be executed in order.

    Args:
        plans:   List of TerragruntApply instances to execute
        dep_map: Dict mapping provider path -> list of dependency provider paths
                 (from _parse_provider_deps)

    Returns:
        List of waves. Each wave is a list of TerragruntApply instances
        that can safely run in parallel.

    ALGORITHM (Kahn's topological sort, wave variant):
        1. Start with all plans in "remaining"
        2. Each iteration: collect plans whose deps are all "completed" or
           "not in this run" (deps not in remaining are treated as satisfied)
        3. Add that group as a wave, mark them completed
        4. Repeat until remaining is empty

    EXAMPLE:
        plans = [vpc, security-groups, rds, ecs]
        dep_map = {
            "providers/.../security-groups": ["providers/.../vpc"],
            "providers/.../rds":             ["providers/.../vpc"],
            "providers/.../ecs":             ["providers/.../security-groups",
                                              "providers/.../rds"],
        }
        Result:
            Wave 1: [vpc]
            Wave 2: [security-groups, rds]   # both deps on vpc (now completed)
            Wave 3: [ecs]                    # deps on wave 2 (now completed)

    EDGE CASES:
        - No deps: All plans in one wave (identical to old flat behavior)
        - Dep not in this run: Treated as satisfied (applied previously)
        - Circular deps: Detected when no progress is made; fall back to
          running all remaining in one wave (with a warning)

    Go equivalent:
        func buildWaves(plans []*Apply, depMap map[string][]string) [][]*Apply {
            // Kahn's algorithm...
        }
    """
    # Set of all provider paths being applied in this run (for dep resolution)
    plan_providers = {p.provider for p in plans}

    # Map provider path -> TerragruntApply for quick lookup
    provider_to_plan = {p.provider: p for p in plans}

    # Providers still waiting to be assigned to a wave
    remaining = set(plan_providers)

    # Providers that have been assigned to a completed (earlier) wave
    completed: set = set()

    waves = []

    while remaining:
        # Find providers whose dependencies are all satisfied:
        # - dep is in completed: already assigned to an earlier wave, OR
        # - dep is not in plan_providers: not being applied in this run
        #   (was applied in a previous ticket, or has no changes)
        wave = [
            provider_to_plan[p]
            for p in remaining
            if all(
                d in completed or d not in plan_providers
                for d in dep_map.get(p, [])
            )
        ]

        if not wave:
            # No progress possible — circular dependency or missing deps.
            # Fall back to running all remaining plans without strict ordering.
            warn(
                "Could not determine dependency order (circular dependency?) "
                "— running remaining plans without strict ordering"
            )
            wave = [provider_to_plan[p] for p in remaining]
            wave.sort(key=lambda p: p.sort_key)
            waves.append(wave)
            break  # All remaining handled; exit loop

        # Sort within wave for consistent ordering (dev before prod, etc.)
        wave.sort(key=lambda p: p.sort_key)
        waves.append(wave)

        # Mark wave as completed and remove from remaining
        for plan in wave:
            remaining.discard(plan.provider)
            completed.add(plan.provider)

    return waves
