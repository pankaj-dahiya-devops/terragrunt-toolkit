"""
Parallel Terragrunt plan execution orchestrator.

This module handles running multiple terragrunt plans in parallel:
- Managing a collection of TerragruntPlan instances
- Running plans concurrently using ThreadPoolExecutor
- Aggregating results and generating consolidated summary reports
- Signal handling for graceful shutdown (Ctrl+C)
- Filtering and limiting which plans to run

PYTHON CONCEPTS FOR GO/BASH DEVELOPERS:
---------------------------------------
1. ThreadPoolExecutor - Thread pool for parallel execution (like goroutines + WaitGroup)
2. threading.Event - Thread-safe flag for signaling between threads
3. as_completed() - Yields futures as they complete (like select on channels)
4. Signal handling - Catching Ctrl+C and other signals
5. Context managers (with statement) - Automatic resource cleanup
6. List comprehensions - [x for x in items if condition]
"""

# =============================================================================
# IMPORTS
# =============================================================================

import os           # Operating system interface
import sys          # System-specific parameters (sys.argv, sys.exit)
import json         # JSON encoding/decoding
import shlex        # Shell lexical analysis (quoting)
import shutil       # High-level file operations (rmtree)
import signal       # Signal handling (SIGINT, SIGTERM)
import threading    # Thread synchronization primitives
from pathlib import Path  # Cross-platform path handling
from datetime import datetime  # Date/time handling
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING

# concurrent.futures provides high-level interface for async execution
# ThreadPoolExecutor: Pool of worker threads
# as_completed: Iterator that yields futures as they complete
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import from our common module
from .common import (
    CHANGE_TYPES,              # ["add", "destroy", "recreate", "change", "read"]
    REPORT_FILE,               # "plan_all_report.txt"
    ERROR_PLANS_HEADER,        # Section header for error plans
    NO_CHANGE_PLANS_HEADER,    # Section header for no-change plans
    SUCCESSFUL_PLANS_HEADER,   # Section header for successful plans
    SKIPPED_PLANS_HEADER,      # Section header for skipped plans
    PLAN_SUMMARIES_HEADER,     # Section header for individual summaries
    PlanStatus,                # Status enum
    ReplanType,                # Replan type enum
    debug,                     # Debug logging
    info,                      # Info logging
    warn,                      # Warning logging
    error,                     # Error logging
    is_interactive,            # Check if running in terminal
)
from .plan import TerragruntPlan  # Single plan execution

# TYPE_CHECKING import to avoid circular imports
if TYPE_CHECKING:
    from .project import TerraformProject


# =============================================================================
# TERRAGRUNTPLANALL CLASS
# =============================================================================

class TerragruntPlanAll:
    """
    Orchestrates running multiple terragrunt plans in parallel.

    This class manages the entire plan-all workflow:
    1. Collect providers to plan (from arguments, modules, or previous runs)
    2. Apply filters to narrow down the list
    3. Run plans in parallel using a thread pool
    4. Handle interrupts gracefully
    5. Generate a consolidated summary report

    PARALLEL EXECUTION CONCEPTS:
    ---------------------------
    Unlike Go's goroutines, Python has the Global Interpreter Lock (GIL) which
    prevents true parallel Python code execution. However, for I/O-bound tasks
    like running external commands (terragrunt), threads work well because:
    - The GIL is released during I/O operations
    - subprocess calls run in separate processes
    - We're mostly waiting for terragrunt, not doing CPU work

    ThreadPoolExecutor is similar to:
    - Go: A pool of goroutines with a WaitGroup
    - Bash: Running commands with & and using wait

    Example:
        plan_all = TerragruntPlanAll(project, "INFRA-1234", options)
        plan_all.find_providers(["providers/dev/us-east-1/svc"])
        plan_all.show_plans()
        plan_all.run()
        plan_all.generate_summary()
    """

    def __init__(
        self,
        project: "TerraformProject",
        ticket: str,
        cmd_options: Dict[str, Any],
    ):
        """
        Initialize the plan-all operation.

        Args:
            project: The TerraformProject instance
            ticket: JIRA ticket number (e.g., "INFRA-1234") for organizing output
            cmd_options: Dictionary of command-line options including:
                - concurrency: int - Number of parallel plans
                - stop_on_error: bool - Stop after first failure
                - replan: str - Replan type (all/changes/errors/none)
                - And all options passed to individual TerragruntPlan instances
        """
        self.project = project
        self.ticket = ticket
        self.cmd_options = cmd_options

        # -------------------------------------------------------------------------
        # OUTPUT PATHS
        # -------------------------------------------------------------------------
        # All output is organized under tickets/{ticket}/
        self.ticketdir = Path("tickets") / ticket
        self.summaryfile = self.ticketdir / REPORT_FILE  # plan_all_report.txt

        # -------------------------------------------------------------------------
        # PLANS COLLECTION
        # -------------------------------------------------------------------------
        # List of all TerragruntPlan instances to potentially run
        self.plans: List[TerragruntPlan] = []

        # -------------------------------------------------------------------------
        # INTERRUPT HANDLING
        # -------------------------------------------------------------------------
        # Track interrupt signals for graceful shutdown
        self._interrupt_count = 0

        # threading.Event is a thread-safe boolean flag
        # Used to signal all threads to stop
        # Similar to context.Context cancellation in Go
        self._cancel_event = threading.Event()

        # -------------------------------------------------------------------------
        # TIMING
        # -------------------------------------------------------------------------
        self.starttime: Optional[datetime] = None
        self.endtime: Optional[datetime] = None

        # Store original command line for summary report
        # sys.argv is like os.Args in Go
        self.orig_argv: List[str] = sys.argv.copy()

    def __str__(self) -> str:
        """String representation (the ticket number)."""
        return self.ticket

    # =========================================================================
    # PLAN MANAGEMENT
    # =========================================================================

    def plan_for_provider(self, provider: str, add_if_new: bool = True) -> TerragruntPlan:
        """
        Find an existing plan for a provider, or create a new one.

        This ensures we don't create duplicate plans for the same provider.

        Args:
            provider: Provider path (e.g., "providers/dev/us-east-1/svc")
            add_if_new: If True, automatically add new plans to self.plans

        Returns:
            The TerragruntPlan instance (existing or newly created)

        Example:
            # Get or create a plan
            plan = plan_all.plan_for_provider("providers/dev/us-east-1/svc")
            plan.should_run = True
        """
        # Look for existing plan with matching provider
        for plan in self.plans:
            if plan.provider == provider:
                return plan

        # Create new plan instance
        plan = TerragruntPlan(self.project, self.ticket, provider, self.cmd_options)

        # Optionally add to the plans list
        if add_if_new:
            self.plans.append(plan)

        return plan

    def load_providers(self) -> None:
        """
        Load providers from a previously run plan summary file.

        This is used for the --replan option to resume or retry previous plans.
        The summary file contains sections listing which plans had which status.

        File format parsed:
            ERROR PLANS:
            providers/dev/us-east-1/svc/svc.txt
            ...
            No change plans:
            providers/dev/us-west-2/svc/svc.txt
            ...
            Successful plans:
            providers/prod/us-east-1/svc/svc.txt
            ...
        """
        info(f"Reading previous plan summary for {self}")

        # Check if summary file exists
        if not self.summaryfile.exists():
            return

        status = None  # Track which section we're in

        with open(self.summaryfile, "r") as f:
            for line in f:
                line = line.rstrip("\n")

                # Detect section headers to determine status
                if line in [ERROR_PLANS_HEADER, SKIPPED_PLANS_HEADER]:
                    status = PlanStatus.ERROR
                elif line == NO_CHANGE_PLANS_HEADER:
                    status = PlanStatus.NOCHANGE
                elif line == SUCCESSFUL_PLANS_HEADER:
                    status = PlanStatus.SUCCESS
                elif line == PLAN_SUMMARIES_HEADER:
                    # Stop at individual summaries section
                    break

                # Parse provider lines (contain "providers/")
                if status and "providers/" in line:
                    # Extract provider path from the line
                    # Line format: "providers/dev/us-east-1/svc/svc.txt"
                    match_start = line.find("providers/")
                    if match_start >= 0:
                        provider_part = line[match_start:]
                        # Get directory path (remove filename)
                        provider = str(Path(provider_part).parent)

                        # Skip if provider no longer exists
                        if not Path(provider).is_dir():
                            warn(f"{provider} no longer exists, skipping")
                            continue

                        # Get or create plan for this provider
                        plan = self.plan_for_provider(provider)
                        plan.status = status
                        plan.replan = True
                        plan.parse_cmd_options()  # Read options from old plan file

                        # Determine if this plan should run based on --replan type
                        replan_type = self.cmd_options.get("replan")
                        if replan_type == ReplanType.ALL:
                            # Rerun all plans
                            plan.should_run = True
                        elif replan_type == ReplanType.CHANGES:
                            # Only rerun plans that had changes
                            plan.should_run = status != PlanStatus.NOCHANGE
                        elif replan_type == ReplanType.ERRORS:
                            # Only rerun failed plans
                            plan.should_run = status == PlanStatus.ERROR
                        elif replan_type == ReplanType.NONE:
                            # Don't rerun any, just regenerate summary
                            plan.should_run = False
                        else:
                            plan.should_run = False

    def find_providers(self, providers: List[str]) -> None:
        """
        Add plans for explicitly specified provider paths.

        Args:
            providers: List of provider paths to plan
                      (e.g., ["providers/dev/us-east-1/svc", "providers/prod/us-east-1/svc"])
        """
        for provider in providers:
            plan = self.plan_for_provider(provider)
            plan.should_run = True
            if not plan.replan:
                plan.replan = False
            plan.reset_cmd_options()  # Apply current command options

    def find_modules(self, modules: List[str]) -> None:
        """
        Find all providers that depend on the specified modules.

        When a module is changed, all providers using that module need replanning.
        This scans all providers and checks their module dependencies.

        Args:
            modules: List of module paths (e.g., ["modules/my-service"])

        Example:
            # User changed modules/my-service
            # This finds all providers that use it
            plan_all.find_modules(["modules/my-service"])
        """
        if not modules:
            return

        debug(f"find_modules: searching for providers that depend on {modules}")

        # Iterate through all providers in the project
        provider_count = 0
        for provider in self.project.each_provider():
            provider_count += 1
            # Create plan (don't add yet - only add if matches)
            plan = self.plan_for_provider(provider, add_if_new=False)
            plan.parse_dependencies()

            debug(f"  {provider}: modules={plan.modules}")

            # Check if provider depends on any of the requested modules
            # set() & set() is set intersection (elements in both)
            intersect = set(modules) & set(plan.modules)
            if intersect:
                plan.should_run = True
                plan.replan = False
                plan.reset_cmd_options()
                # Add to plans list if not already there
                if plan not in self.plans:
                    self.plans.append(plan)
                debug(f"  -> Adding {plan} (depends on {list(intersect)})")

        debug(f"find_modules: scanned {provider_count} providers, found {len(self.plans)} matches")

    def find_variables(self, variables: List[str]) -> None:
        """
        Find all providers that depend on the specified variable files.

        When a .tfvars file is changed, providers using it need replanning.

        Args:
            variables: List of variable file paths
        """
        if not variables:
            return

        for provider in self.project.each_provider():
            plan = self.plan_for_provider(provider, add_if_new=False)
            plan.parse_dependencies()

            # Check if provider depends on any of the requested variable files
            intersect = set(variables) & set(plan.vars)
            if intersect:
                plan.should_run = True
                plan.replan = False
                plan.reset_cmd_options()
                if plan not in self.plans:
                    self.plans.append(plan)
                debug(f"Adding {plan} (depends on {list(intersect)})")

    # =========================================================================
    # FILTERING
    # =========================================================================

    def apply_filters(
        self,
        filters: List["re.Pattern"],
        exclude_filters: List["re.Pattern"],
        limit_per_module: Optional[int],
    ) -> None:
        """
        Apply include/exclude filters and per-module limits to the plan list.

        This narrows down which plans will actually run based on:
        - Include filters: Provider must match ALL patterns (AND logic)
        - Exclude filters: Provider must NOT match ANY pattern (OR logic)
        - Limit per module: Maximum N plans per top-level module

        Args:
            filters: List of compiled regex patterns that must all match
            exclude_filters: List of compiled regex patterns that must not match
            limit_per_module: Optional maximum plans per top-level module

        Example:
            # Only plan dev providers in us-east-1
            import re
            filters = [re.compile(r"/dev/"), re.compile(r"us-east-1")]
            plan_all.apply_filters(filters, [], None)
        """
        import re

        # Nothing to do if no filters specified
        if not filters and not exclude_filters and limit_per_module is None:
            return

        # -------------------------------------------------------------------------
        # APPLY INCLUDE/EXCLUDE FILTERS
        # -------------------------------------------------------------------------
        filtered = []
        for plan in self.plans:
            plan.parse_dependencies()

            # Check include filters - ALL must match
            # all() returns True if all items are True
            if filters and not all(plan.matches_filter(f) for f in filters):
                debug(f"Removing {plan} (does not match all filters)")
                continue

            # Check exclude filters - NONE must match
            # any() returns True if any item is True
            if exclude_filters and any(plan.matches_filter(f) for f in exclude_filters):
                debug(f"Removing {plan} (matches one or more exclude filters)")
                continue

            filtered.append(plan)

        # -------------------------------------------------------------------------
        # APPLY LIMIT PER MODULE
        # -------------------------------------------------------------------------
        if limit_per_module:
            # Group plans by their first module dependency
            by_module: Dict[str, List[TerragruntPlan]] = {}
            for plan in filtered:
                first_module = plan.modules[0] if plan.modules else ""
                if first_module not in by_module:
                    by_module[first_module] = []
                by_module[first_module].append(plan)

            # Take only the first N plans from each group
            # [:N] is slice notation - takes first N items
            limited = []
            for module_plans in by_module.values():
                limited.extend(module_plans[:limit_per_module])
            filtered = limited

        # -------------------------------------------------------------------------
        # UPDATE PLANS LIST
        # -------------------------------------------------------------------------
        # For replans, we keep the plan but mark should_run=False (soft delete)
        # This preserves the plan status for the summary report
        new_plans = []
        for plan in self.plans:
            if plan.replan:
                # Keep replan plans, but update should_run
                if plan not in filtered:
                    plan.should_run = False
                new_plans.append(plan)
            elif plan in filtered:
                # Keep non-replan plans only if they passed filters
                new_plans.append(plan)

        self.plans = new_plans

    # =========================================================================
    # DISPLAY
    # =========================================================================

    def show_plans(self) -> None:
        """
        Display the list of plans that will be run.

        Shows each provider with its status, sorted by provider path.
        """
        self.starttime = datetime.now()
        info(f"Plan started at {self.starttime}")

        # shlex.quote() safely quotes arguments for shell display
        info(f"Command line: {' '.join(shlex.quote(a) for a in self.orig_argv)}")

        if not self.plans:
            info("No matching plans to run!")
        else:
            # Sort plans alphabetically by provider path
            # key= specifies what to sort by (lambda creates an anonymous function)
            self.plans.sort(key=lambda p: p.sort_key)

            # Display each plan
            for plan in self.plans:
                tag = "(Skipping) " if not plan.should_run else ""
                info(f"  {tag}{plan}")

            # Show totals
            skipped = sum(1 for p in self.plans if not p.should_run)
            if skipped == 0:
                info(f"Plan: {self.ticketdir}, {len(self.plans)} providers")
            else:
                running = len(self.plans) - skipped
                info(f"Plan: {self.ticketdir}, {running} providers + {skipped} skipped")

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def clean(self) -> None:
        """
        Remove all contents from the ticket directory.

        Used with --clean option to start fresh.
        """
        if self.ticketdir.exists():
            for item in self.ticketdir.iterdir():
                if item.is_dir():
                    # shutil.rmtree removes directory and all contents
                    shutil.rmtree(item)
                else:
                    # unlink() removes a file
                    item.unlink()

    def empty(self) -> bool:
        """
        Check if there are no plans to run.

        Returns:
            True if the plans list is empty
        """
        return len(self.plans) == 0

    # =========================================================================
    # MAIN RUN METHOD
    # =========================================================================

    def run(self) -> None:
        """
        Run all plans in parallel using ThreadPoolExecutor.

        This is the main execution method that:
        1. Sets up signal handlers for graceful shutdown
        2. Creates a thread pool with specified concurrency
        3. Submits all plans to the pool
        4. Waits for completion, handling results as they come in
        5. Restores signal handlers

        THREADPOOLEXECUTOR EXPLAINED:
        ----------------------------
        ThreadPoolExecutor manages a pool of worker threads:

            with ThreadPoolExecutor(max_workers=5) as executor:
                # Submit work items - returns Future objects
                future = executor.submit(function, arg1, arg2)

                # Get result (blocks until complete)
                result = future.result()

        The 'with' statement ensures threads are cleaned up when done.

        as_completed() is an iterator that yields futures as they complete,
        regardless of the order they were submitted. Similar to Go's:

            for result := range results {
                // handle result
            }
        """
        # Check terraform configuration for potential issues
        self._check_terraform_rc()

        # Create ticket directory
        self.ticketdir.mkdir(parents=True, exist_ok=True)

        # Set up signal handlers (Ctrl+C, kill)
        original_handlers = self._setup_signal_handlers()

        try:
            # Get only plans that should actually run
            plans_to_run = [p for p in self.plans if p.should_run]

            # Re-parse output from skipped plans (for summary report)
            for plan in self.plans:
                if not plan.should_run and plan.outputfile.exists():
                    plan.parse_output()

            # Get configuration
            concurrency = self.cmd_options.get("concurrency", 5)
            stop_on_error = self.cmd_options.get("stop_on_error", False)

            # -------------------------------------------------------------------------
            # RUN PLANS IN PARALLEL
            # -------------------------------------------------------------------------
            # ThreadPoolExecutor context manager handles thread lifecycle
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                # Submit all plans to the thread pool
                # This is a dict comprehension: {key: value for item in items}
                # Keys are Future objects, values are the plan that future represents
                futures = {
                    executor.submit(self._run_single_plan, plan): plan
                    for plan in plans_to_run
                }

                # Process results as they complete
                # as_completed() yields futures in completion order, not submission order
                for future in as_completed(futures):
                    # Check if we've been cancelled
                    if self._cancel_event.is_set():
                        break

                    # Get the plan associated with this future
                    plan = futures[future]

                    try:
                        # Get result (or exception) from the future
                        future.result()
                    except Exception as e:
                        error(f"{plan}: {e}")

                    # Handle completion (logging, etc.)
                    self._plan_finish(plan, stop_on_error)

                    # Stop all remaining plans if this one failed and --stop-on-error is set
                    if stop_on_error and plan.status == PlanStatus.ERROR:
                        self._cancel_all()
                        break

        finally:
            # Always restore signal handlers and record end time
            self._restore_signal_handlers(original_handlers)
            self.endtime = datetime.now()

    def _run_single_plan(self, plan: TerragruntPlan) -> None:
        """
        Run a single plan (called from a worker thread).

        Args:
            plan: The TerragruntPlan instance to run

        This is the function submitted to ThreadPoolExecutor.
        It runs in a separate thread from the main thread.
        """
        debug(f"{plan}: Starting")
        plan.run()              # Execute terragrunt plan
        plan.generate_summary() # Generate summary for report

    def _plan_finish(self, plan: TerragruntPlan, stop_on_error: bool) -> None:
        """
        Handle plan completion (logging).

        Args:
            plan: The completed plan
            stop_on_error: Whether --stop-on-error was set
        """
        debug(f"{plan}: Finished")
        if plan.status == PlanStatus.ERROR:
            # chr(10) is newline character - used because f-strings can't contain backslash
            error(f"{plan}: {chr(10).join(plan.errors)}")
        else:
            info(f"{plan}: {plan.summary}")

    # =========================================================================
    # SIGNAL HANDLING
    # =========================================================================

    def _setup_signal_handlers(self) -> Dict[int, Any]:
        """
        Set up signal handlers for graceful shutdown on Ctrl+C.

        Returns:
            Dictionary mapping signal numbers to their original handlers
            (so we can restore them later)

        SIGNAL HANDLING EXPLAINED:
        -------------------------
        Signals are asynchronous notifications sent to a process:
        - SIGINT (2): Sent when user presses Ctrl+C
        - SIGTERM (15): Sent by 'kill' command (graceful termination request)

        signal.signal(signum, handler) registers a function to be called
        when that signal is received. The handler function receives:
        - signum: The signal number
        - frame: The current stack frame (usually ignored)

        Windows doesn't support all Unix signals, so we check the platform.

        Similar to Go's:
            c := make(chan os.Signal, 1)
            signal.Notify(c, os.Interrupt, syscall.SIGTERM)
            go func() {
                <-c
                // handle signal
            }()
        """
        original_handlers = {}

        # Only set up signal handlers on Unix-like systems (not Windows)
        if sys.platform != "win32":
            signals = [signal.SIGINT, signal.SIGTERM]

            for sig in signals:
                # signal.signal returns the old handler
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
        Handle interrupt signals (Ctrl+C, kill).

        Args:
            signum: Signal number
            frame: Stack frame (unused)

        Implements a 3-strike system:
        1. First Ctrl+C: Gracefully stop all plans
        2. Second Ctrl+C: Warning, one more to force exit
        3. Third Ctrl+C: Force immediate exit

        This allows running terragrunt processes to finish cleanly.
        """
        self._interrupt_count += 1

        # signal.Signals(signum).name gets human-readable name like "SIGINT"
        signame = signal.Signals(signum).name

        if self._interrupt_count == 1:
            # First interrupt: start graceful shutdown
            warn(f"Caught {signame}, waiting for threads to exit")
            self._cancel_all()
        elif self._interrupt_count == 2:
            # Second interrupt: warn about force exit
            warn(f"Caught {signame}, press Ctrl-C again to exit immediately")
        else:
            # Third interrupt: force exit
            warn(f"Caught {signame}, exiting now")
            sys.exit(1)

    def _cancel_all(self) -> None:
        """
        Signal all plans to cancel their execution.

        Sets the cancel event and marks each plan for cancellation.
        Running plans will check their cancel flag and stop gracefully.
        """
        self._cancel_event.set()  # Set the threading.Event flag
        for plan in self.plans:
            plan.cancel = True    # Signal each plan to stop

    # =========================================================================
    # CONFIGURATION CHECKS
    # =========================================================================

    def _check_terraform_rc(self) -> None:
        """
        Check for recommended terraform configuration.

        Warns if plugin_cache_dir is configured without
        plugin_cache_may_break_dependency_lock_file = true,
        which can cause "text file busy" errors when running multiple
        terragrunt operations in parallel.
        """
        trc_path = Path.home() / ".terraformrc"
        if not trc_path.exists():
            return

        try:
            content = trc_path.read_text()

            # Warn if using shared plugin cache without the workaround
            if (
                self.cmd_options.get("concurrency", 5) > 1
                and len(self.plans) > 1
                and "plugin_cache_dir" in content
                and "plugin_cache_may_break_dependency_lock_file" not in content
            ):
                warn(
                    "Shared plugin cache dir is unreliable when running multiple "
                    "operations in parallel (e.g., 'text file busy' errors)"
                )
                warn(f"It is highly recommended to add the following line to {trc_path}:")
                warn("plugin_cache_may_break_dependency_lock_file = true")

        except Exception as e:
            warn(f"check_terraform_rc: {e}")

    # =========================================================================
    # STATUS CHECKING
    # =========================================================================

    def success(self) -> bool:
        """
        Check if the plan-all operation was successful.

        Returns:
            True if no interrupts occurred and no plans had errors
        """
        return self._interrupt_count == 0 and not self.plans_by_status(PlanStatus.ERROR)

    def plans_by_status(self, status: Optional[str]) -> List[TerragruntPlan]:
        """
        Get all plans with a specific status.

        Args:
            status: PlanStatus value to filter by

        Returns:
            List of plans matching that status

        Example:
            error_plans = plan_all.plans_by_status(PlanStatus.ERROR)
        """
        # List comprehension: [item for item in items if condition]
        return [p for p in self.plans if p.status == status]

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def prune_empty(self) -> None:
        """
        Remove plan output files for plans with no changes.

        By default, we don't keep plan files that show no changes.
        This keeps the ticket directory clean.
        """
        for plan in self.plans:
            should_prune = (
                # Plan ran with no changes and no special options
                (plan.should_run and plan.status == PlanStatus.NOCHANGE and not plan.has_cmd_options())
                # Or plan was discarded (e.g., by --tags-only filter)
                or plan.status == PlanStatus.DISCARDED
            )
            if should_prune:
                plan.prune()  # Remove output files

    # =========================================================================
    # SUMMARY GENERATION
    # =========================================================================

    def generate_summary(self) -> None:
        """
        Generate the consolidated summary report file.

        Creates a report file (plan_all_report.txt) containing:
        - Timing information
        - Command line used
        - Lists of plans by status (error, no-change, success)
        - Individual plan summaries
        - Overall statistics

        This report is used for:
        - Human review of changes
        - --replan option to resume/retry
        - Record of what was planned
        """
        info(f"Generating summary file {self.summaryfile}")

        # -------------------------------------------------------------------------
        # AGGREGATE STATISTICS
        # -------------------------------------------------------------------------
        # Count total resources by change type
        counts = {t: 0 for t in CHANGE_TYPES}

        # Track resources by type for detailed breakdown
        resources: Dict[str, Dict[str, int]] = {t: {} for t in CHANGE_TYPES}

        # Sum up from all successful plans
        for plan in self.plans_by_status(PlanStatus.SUCCESS):
            for change_type in CHANGE_TYPES:
                for resource, count in plan.resources[change_type].items():
                    if resource not in resources[change_type]:
                        resources[change_type][resource] = 0
                    resources[change_type][resource] += count
                    counts[change_type] += count

        # -------------------------------------------------------------------------
        # WRITE REPORT
        # -------------------------------------------------------------------------
        with open(self.summaryfile, "w") as summary:
            def tee(s: str = "") -> None:
                """
                Write to both the summary file and stdout.

                This is a closure (function inside function) that captures
                the 'summary' file object from the enclosing scope.

                In Go, this would be:
                    tee := func(s string) {
                        summary.WriteString(s + "\n")
                        fmt.Println(s)
                    }
                """
                summary.write(s + "\n")
                info(s)

            # -------------------------------------------------------------------------
            # HEADER
            # -------------------------------------------------------------------------
            duration = 0
            if self.endtime and self.starttime:
                duration = self.endtime.timestamp() - self.starttime.timestamp()
            tee(f"Plan ran from {self.starttime} to {self.endtime} ({int(duration)} seconds)")
            tee(f"Command line: {' '.join(shlex.quote(a) for a in self.orig_argv)}")

            # -------------------------------------------------------------------------
            # PLANS BY STATUS
            # -------------------------------------------------------------------------
            # Error plans
            tee(f"\n{ERROR_PLANS_HEADER}\n")
            for plan in self.plans_by_status(PlanStatus.ERROR):
                tee(plan.statusline())

            # No change plans
            tee(f"\n{NO_CHANGE_PLANS_HEADER}\n")
            for plan in self.plans_by_status(PlanStatus.NOCHANGE):
                tee(plan.statusline())

            # Successful plans (with changes)
            tee(f"\n{SUCCESSFUL_PLANS_HEADER}\n")
            for plan in self.plans_by_status(PlanStatus.SUCCESS):
                tee(plan.statusline())

            # Skipped plans (from --replan)
            skipped = [p for p in self.plans if not p.should_run]
            if skipped:
                tee(f"\n{SKIPPED_PLANS_HEADER}\n")
                for plan in skipped:
                    tee(plan.outputfile_s)

            # -------------------------------------------------------------------------
            # INDIVIDUAL SUMMARIES
            # -------------------------------------------------------------------------
            tee(f"\n{PLAN_SUMMARIES_HEADER}\n")
            for plan in self.plans:
                # Only show details for plans that ran
                if plan.status not in [PlanStatus.SUCCESS, PlanStatus.ERROR]:
                    continue

                tee(plan.outputfile_s)
                plan_summary = plan.generate_summary()
                if not plan_summary:
                    tee("  (Summary file not found)")
                else:
                    for line in plan_summary:
                        tee(f"  {line}")
                tee("")  # Blank line between plans
                print()  # Extra newline to stdout

            # -------------------------------------------------------------------------
            # OVERALL STATISTICS
            # -------------------------------------------------------------------------
            tee("Plan all summary:")
            for change_type in CHANGE_TYPES:
                if counts[change_type] > 0:
                    # :2d means format as integer with minimum width 2
                    tee(f"-- {counts[change_type]:2d} total resources to {change_type}")
                    # Show breakdown by resource type
                    for resource in sorted(resources[change_type].keys()):
                        count = resources[change_type][resource]
                        tee(f"      {count:2d} {resource}")

            # Plan counts
            tee(f"  {len(self.plans_by_status(PlanStatus.SUCCESS)):2d} successful plans")
            tee(f"  {len(self.plans_by_status(PlanStatus.NOCHANGE)):2d} no change plans")
            tee(f"  {len(self.plans_by_status(PlanStatus.ERROR)):2d} error plans")

            # Skipped plans
            skipped_replan = [p for p in self.plans if not p.should_run]
            if skipped_replan:
                tee(f"  {len(skipped_replan):2d} plans skipped due to --replan")

            # Discarded plans (from --tags-only)
            discarded = self.plans_by_status(PlanStatus.DISCARDED)
            if discarded:
                tee(f"  {len(discarded):2d} plans discarded due to --tags-only")

            # -------------------------------------------------------------------------
            # FINAL STATUS
            # -------------------------------------------------------------------------
            if self.success():
                info(f"Plan finished successfully at {self.endtime}")
                summary.write("Plan was successful!\n")
            else:
                error(f"Plan finished with errors at {self.endtime}")
                summary.write("Plan had errors\n")
