"""
Single Terragrunt apply execution.

This module handles running a single terragrunt apply for one provider:
- Executing the terragrunt apply command
- Parsing and formatting the apply output
- Tracking apply status (success, error, no-change)
- Handling destroy plans with confirmation

PYTHON CONCEPTS FOR GO/BASH DEVELOPERS:
---------------------------------------
1. This module is similar to plan.py but for the "apply" phase
2. Apply operations actually make changes to infrastructure
3. Sort keys use tuples for multi-level sorting (like Go's sort.Slice with multiple criteria)
4. The destroy flag requires explicit --allow-destroy to run
"""

# =============================================================================
# IMPORTS
# =============================================================================

import os           # Operating system interface
import re           # Regular expressions
import json         # JSON encoding/decoding
import subprocess   # Running external commands
from pathlib import Path  # Cross-platform path handling
from typing import Optional, List, Dict, Any, TextIO, TYPE_CHECKING

# Import from our common module
from .common import (
    TFHCL,                  # terragrunt.hcl filename
    TFVARS_MARKER,          # Marker for variables in plan file
    TFTARGET_MARKER,        # Marker for targets in plan file
    DESTROY_MARKER,         # Marker for destroy plans
    CHANGE_TYPES_PAST,      # ["added", "destroyed", "recreated", "changed", "read"]
    PlanStatus,             # Status enum (SUCCESS, ERROR, NOCHANGE)
    debug,                  # Debug logging
    info,                   # Info logging
    warn,                   # Warning logging
    error,                  # Error logging
    is_verbose,             # Check verbose mode
    strip_ansi,             # Remove ANSI color codes
)

# TYPE_CHECKING import to avoid circular imports
if TYPE_CHECKING:
    from .project import TerraformProject


# =============================================================================
# TERRAGRUNTAPPLY CLASS
# =============================================================================

class TerragruntApply:
    """
    Represents a single terragrunt apply operation for one provider.

    This class handles the apply phase, which actually makes changes
    to infrastructure. It:
    1. Reads the plan file to get variables, targets, and destroy flag
    2. Runs terragrunt apply (or destroy)
    3. Parses output to determine success/failure
    4. Tracks what resources were changed

    IMPORTANT SAFETY FEATURES:
    -------------------------
    - Destroy plans require explicit --allow-destroy flag
    - Plans are sorted to apply dev before prod
    - Previous apply results can be checked with applied()/failed()

    Lifecycle:
        1. Create instance from a plan file
        2. Check if it's a destroy plan (requires confirmation)
        3. Call run() to execute the apply
        4. Check status, summary, errors for results

    Example:
        apply = TerragruntApply(project, "INFRA-1234", "providers/dev/svc", options)
        if apply.destroy and not options["allow_destroy"]:
            print("Skipping destroy plan")
        else:
            apply.run()
            if apply.status == PlanStatus.SUCCESS:
                print(f"Applied: {apply.summary}")
    """

    # -------------------------------------------------------------------------
    # CLASS ATTRIBUTES - SORT ORDERING
    # -------------------------------------------------------------------------
    # These define the order in which applies are run.
    # SAFETY: Dev environments are applied first, prod last.
    # This way if something breaks, it breaks in dev first.

    # Account sort order (lower index = runs first)
    ACCOUNT_ORDER = ["dev", "curation", "util", "internal", "prod"]

    # Region sort order (lower index = runs first)
    # Typically you'd want to apply to less critical regions first
    REGION_ORDER = [
        "ap-northeast-1", "ca-central-1", "ap-southeast-2",
        "eu-west-2", "eu-central-1", "us-east-2", "us-west-2", "us-east-1"
    ]

    def __init__(
        self,
        project: "TerraformProject",
        ticket: str,
        provider: str,
        cmd_options: Dict[str, Any],
    ):
        """
        Initialize an apply for a single provider.

        Args:
            project: The TerraformProject instance
            ticket: JIRA ticket number (e.g., "INFRA-1234")
            provider: Provider path (e.g., "providers/dev/us-east-1/my-service")
            cmd_options: Dictionary of command-line options including:
                - allow_destroy: bool - Allow destroy plans to run
                - run_twice: bool - Run apply twice
                - tg_cache: bool - Keep terragrunt cache
                - refresh: bool - Force terraform refresh
        """
        self.project = project
        self.ticket = ticket
        self.provider = provider
        self.cmd_options = cmd_options

        # -------------------------------------------------------------------------
        # OUTPUT PATHS
        # -------------------------------------------------------------------------
        # Apply output files have .apply extension added to the plan file name
        # Plan:  tickets/INFRA-1234/providers/dev/svc/svc.txt
        # Apply: tickets/INFRA-1234/providers/dev/svc/svc.txt.apply

        self.plandir = Path("tickets") / ticket / provider
        self.planfile = self.plandir / f"{Path(provider).name}.txt"          # Plan file to read
        self.outputfile = self.plandir / f"{Path(provider).name}.txt.apply"  # Apply output
        self.outputfile_s = f"{provider}/{Path(provider).name}.txt.apply"    # Short path for display

        # -------------------------------------------------------------------------
        # EXECUTION STATE
        # -------------------------------------------------------------------------
        self.should_run = True          # Whether to execute this apply
        self.cancel = False             # Set to True to cancel/interrupt
        self.pid: Optional[int] = None  # Process ID when running
        self.exitstatus: Optional[int] = None  # Exit code after completion

        # -------------------------------------------------------------------------
        # RESULTS
        # -------------------------------------------------------------------------
        self.status: Optional[str] = None   # PlanStatus value after run
        self.summary: Optional[str] = None  # One-line summary (e.g., "Apply complete!")
        self.errors: List[str] = []         # Error messages if status is ERROR

        # Resource counts from apply output
        # Example: counts["added"] = 5 means 5 resources were added
        # Note: Uses CHANGE_TYPES_PAST (past tense) vs plan's CHANGE_TYPES
        self.counts: Dict[str, int] = {t: 0 for t in CHANGE_TYPES_PAST}

        # -------------------------------------------------------------------------
        # COMMAND OPTIONS (read from plan file)
        # -------------------------------------------------------------------------
        # These are stored in the plan file by TerragruntPlan
        self.destroy = False              # Whether this is a destroy apply
        self.tfvars: Dict[str, str] = {}  # Terraform variables
        self.tftargets: List[str] = []    # Target resources

        # Parse options from the plan file
        self.parse_cmd_options()

        # -------------------------------------------------------------------------
        # OUTPUT PARSING STATE
        # -------------------------------------------------------------------------
        self._in_error = False  # Currently collecting error lines

    # =========================================================================
    # SPECIAL METHODS
    # =========================================================================

    def __str__(self) -> str:
        """
        String representation of the apply (for logging/display).

        Returns:
            Provider path, prefixed with "(Destroy)" if this is a destroy apply
        """
        prefix = "(Destroy) " if self.destroy else ""
        return f"{prefix}{self.provider}"

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def account(self) -> str:
        """
        Get account name (dev, prod, etc.) from provider path.

        Returns:
            Account name extracted from provider path

        Example:
            apply.provider = "providers/dev/us-east-1/my-service"
            apply.account  # Returns: "dev"
        """
        parts = self.provider.split("/")
        return parts[1] if len(parts) > 1 else ""

    @property
    def region(self) -> Optional[str]:
        """
        Extract AWS region from provider path.

        Returns:
            Region string or None if not found
        """
        # Regex pattern for AWS regions
        pattern = re.compile(
            r"/(?P<region>[a-z]{2}-(?:gov-)?(?:(?:north|south|)(?:east|west|)|central)(?<=[a-z])-\d)[-/]"
        )
        match = pattern.search(self.provider)
        return match.group("region") if match else None

    @property
    def sort_key(self) -> tuple:
        """
        Key for sorting applies in safe order (dev before prod).

        Returns a tuple for multi-level sorting:
        1. Account index (dev=0, prod=4)
        2. Account name (for stability)
        3. Region index
        4. Region name (for stability)
        5. Provider path (final tiebreaker)

        Returns:
            Tuple used for sorting

        PYTHON TUPLE SORTING:
        --------------------
        When sorting tuples, Python compares element by element:
        - (0, "dev", 0, "us-east-1", ...) comes before
        - (4, "prod", 0, "us-east-1", ...)

        This is similar to Go's sort.Slice with multiple comparisons:
            sort.Slice(items, func(i, j int) bool {
                if items[i].account != items[j].account {
                    return accountOrder[items[i].account] < accountOrder[items[j].account]
                }
                // ... more comparisons
            })
        """
        # Get account sort position (unknown accounts get 99)
        account_name = self.account
        account_index = (
            self.ACCOUNT_ORDER.index(account_name)
            if account_name in self.ACCOUNT_ORDER
            else 99  # Unknown accounts sort last
        )

        # Get region sort position
        region_name = self.region or ""
        region_index = (
            self.REGION_ORDER.index(region_name)
            if region_name in self.REGION_ORDER
            else 99  # Unknown regions sort last
        )

        # Return tuple for sorting
        # Sorting by tuple compares element by element
        return (account_index, account_name, region_index, region_name, self.provider)

    # =========================================================================
    # COMMAND OPTIONS
    # =========================================================================

    def parse_cmd_options(self) -> None:
        """
        Parse command-line options from the plan file.

        The plan file contains special marker lines at the top:
        - DESTROY_MARKER if it's a destroy plan
        - TFVARS_MARKER followed by JSON-encoded variables
        - TFTARGET_MARKER followed by JSON-encoded targets

        This reads those markers to know how to run the apply.
        """
        if not self.planfile.exists():
            return

        # Reset to defaults
        self.destroy = False
        self.tfvars = {}
        self.tftargets = []

        # Read file and look for marker lines
        with open(self.planfile, "r") as f:
            for line in f:
                if line == DESTROY_MARKER:
                    self.destroy = True
                elif line.startswith(TFVARS_MARKER):
                    try:
                        self.tfvars = json.loads(line[len(TFVARS_MARKER):])
                    except json.JSONDecodeError:
                        pass
                elif line.startswith(TFTARGET_MARKER):
                    try:
                        self.tftargets = json.loads(line[len(TFTARGET_MARKER):])
                    except json.JSONDecodeError:
                        pass

    # =========================================================================
    # STATUS AND DISPLAY
    # =========================================================================

    def statusline(self) -> str:
        """
        Generate a one-line summary of the apply result.

        Returns:
            Status line like: "Apply complete! Resources: 2 added - providers/.../svc.txt.apply"
        """
        parts = []

        if self.status == PlanStatus.ERROR and self.errors:
            parts.append(f"{self.errors[0]} -")

        if self.status == PlanStatus.SUCCESS and self.summary:
            parts.append(f"{self.summary} -")

        parts.append(self.outputfile_s)

        return " ".join(parts)

    def applied(self) -> bool:
        """
        Check if a previous apply attempt exists.

        Used for --retry option to skip already-applied plans.

        Returns:
            True if the apply output file exists
        """
        return self.outputfile.exists()

    def failed(self) -> bool:
        """
        Check if the previous apply attempt failed.

        Used for --retry errors option to only retry failed applies.

        Returns:
            True if apply output exists but doesn't contain success message
        """
        if not self.applied():
            return False

        # Check file content for success messages
        content = self.outputfile.read_text()
        # Both "Apply complete!" and "Destroy complete!" indicate success
        return "Apply complete!" not in content and "Destroy complete!" not in content

    # =========================================================================
    # MAIN RUN METHOD
    # =========================================================================

    def run(self) -> None:
        """
        Run the terragrunt apply and capture output.

        This is the main entry point for executing an apply. It:
        1. Creates the output directory
        2. Opens the output file for writing
        3. Runs terragrunt apply/destroy
        4. Handles errors and cleanup

        After calling run(), check:
        - self.status: SUCCESS, ERROR, or NOCHANGE
        - self.summary: One-line result (e.g., "Apply complete!")
        - self.errors: List of error messages (if ERROR)
        - self.counts: Resource counts by change type
        """
        try:
            # Create output directory
            self.plandir.mkdir(parents=True, exist_ok=True)

            # Open output file for writing
            with open(self.outputfile, "w") as output:
                # Check if cancelled before starting
                if self.cancel:
                    err = "Not run due to previous error"
                    self.status = PlanStatus.ERROR
                    self.errors.append(err)
                    output.write(f"\nError: {err}\n")
                    return

                # Run the actual terragrunt command
                self._run_terragrunt(output)

        except Exception as e:
            # Catch any unexpected errors
            self.status = PlanStatus.ERROR
            self.errors.append(str(e))
            import traceback
            self.errors.extend(traceback.format_exc().split("\n"))
            error(f"{self}: {e}")

        finally:
            # Always clean up
            self._stop_terragrunt()
            # Terragrunt copies .terraform.lock.hcl back to the provider directory
            # after init (intended for VCS), but we want it in /tmp/terragrunt_cache.
            # Delete it here so provider directories stay clean.
            lock_file = self.project.root / self.provider / ".terraform.lock.hcl"
            if lock_file.exists():
                debug(f"Removing .terraform.lock.hcl from {self.provider}")
                lock_file.unlink()

    def _run_terragrunt(self, output: TextIO) -> None:
        """
        Execute the terragrunt apply command and parse output.

        Args:
            output: File object to write apply output to

        This method:
        1. Builds the appropriate command (apply or destroy)
        2. Optionally clears terragrunt cache
        3. Optionally runs terragrunt refresh
        4. Runs terragrunt apply/destroy (terragrunt auto-inits if needed)
        5. Parses output line by line

        IMPORTANT: Destroy plans require --allow-destroy flag!
        """
        # -------------------------------------------------------------------------
        # BUILD COMMAND
        # -------------------------------------------------------------------------
        # Determine if this is apply or destroy
        if not self.destroy:
            # Normal apply
            tf_cmd = ["terragrunt", "apply", "-input=false", "-no-color", "-auto-approve"]
        elif self.cmd_options.get("allow_destroy"):
            # Destroy with explicit permission
            tf_cmd = ["terragrunt", "destroy", "-input=false", "-no-color", "-auto-approve"]
        else:
            # Destroy without permission - error!
            raise RuntimeError(
                "confirmation required to run destroy plans with terragrunt_apply_all!"
            )

        # Add terraform variables
        for key, value in self.tfvars.items():
            tf_cmd.extend(["-var", f"{key}={value}"])

        # Add target resources
        for target in self.tftargets:
            tf_cmd.append(f"-target={target}")

        # -------------------------------------------------------------------------
        # PREPARE EXECUTION
        # -------------------------------------------------------------------------
        provider_path = self.project.root / self.provider

        # Build shell command sequence
        commands = []

        # Optionally clear terragrunt cache
        if not self.cmd_options.get("tg_cache", True):
            cache_dir = self._get_terragrunt_cache()
            if cache_dir:
                commands.append(f"rm -rf {_shell_quote(cache_dir)}")

        # Optional refresh before apply
        if self.cmd_options.get("refresh"):
            commands.append("terragrunt refresh -input=false -no-color")

        # Run twice option (some resources need two applies)
        if self.cmd_options.get("run_twice"):
            commands.append(" ".join(_shell_quote(c) for c in tf_cmd))

        # Main apply command
        commands.append(" ".join(_shell_quote(c) for c in tf_cmd))

        # Join commands with && (only continue if previous succeeded)
        full_cmd = " && ".join(commands)

        # -------------------------------------------------------------------------
        # EXECUTE COMMAND
        # -------------------------------------------------------------------------
        # Redirect terragrunt cache to /tmp so it never pollutes provider directories.
        # Note: TERRAGRUNT_DOWNLOAD_DIR is the old name (deprecated), TG_DOWNLOAD_DIR is current.
        env = os.environ.copy()
        env.setdefault("TG_DOWNLOAD_DIR", "/tmp/terragrunt_cache")

        process = subprocess.Popen(
            full_cmd,
            shell=True,
            cwd=provider_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
        )

        self.pid = process.pid

        # Parse output line by line
        if process.stdout:
            self._parse_terragrunt_output(process.stdout, output)

        # Wait for completion
        process.wait()
        self.pid = None
        self.exitstatus = process.returncode

        # -------------------------------------------------------------------------
        # CHECK RESULT
        # -------------------------------------------------------------------------
        if self.exitstatus != 0:
            err = f"terragrunt exited with status {self.exitstatus}"
            self.status = PlanStatus.ERROR
            if not self.errors:
                self.errors.append(err)
            output.write(f"\nError: {err}\n")
            output.flush()
        elif self.summary is None:
            # Apply completed but we couldn't parse the result
            err = "Could not determine terragrunt result"
            self.status = PlanStatus.ERROR
            self.errors.append(err)
            output.write(f"\nError: {err}\n")
            output.flush()

    def _parse_terragrunt_output(self, input_stream: TextIO, output: Optional[TextIO] = None) -> None:
        """
        Parse terragrunt output stream line by line.

        Args:
            input_stream: Stream to read terragrunt output from
            output: Optional file to write output to
        """
        self._in_error = False

        for line in input_stream:
            # Check for cancellation
            if self.cancel:
                self.status = PlanStatus.ERROR
                msg = "Interrupted by signal"
                self.errors.append(msg)
                if output:
                    output.write(f"Error: {msg}\n")
                    output.flush()
                self._stop_terragrunt()
                return

            # Print to console if verbose
            if is_verbose():
                print(line, end="")

            # Clean up line
            line = line.rstrip("\n\r")
            line = strip_ansi(line)

            # Strip terragrunt output prefix
            # Terragrunt prefixes lines with timestamp and stream info:
            #   "23:50:15.534 STDOUT terraform: Apply complete! ..."
            tg_prefix_match = re.match(
                r"^\d{2}:\d{2}:\d{2}\.\d+ (?:STDOUT|STDERR) (?:terraform|tofu|terragrunt): (.*)$",
                line,
            )
            if tg_prefix_match:
                line = tg_prefix_match.group(1)

            # -------------------------------------------------------------------------
            # ERROR DETECTION
            # -------------------------------------------------------------------------
            if re.match(r"^Error:|^Usage:|^There are some problems with the configuration", line):
                self.status = PlanStatus.ERROR
                if line != "Error: ":
                    self.errors.append(line)
                self._in_error = True

            elif "TERRAFORM CRASH" in line:
                self.status = PlanStatus.ERROR
                self.errors.append(line)
                self._in_error = True
                self._stop_terragrunt()

            elif self._in_error:
                # Continue collecting error lines
                self.errors.append(line)

            else:
                # Normal output processing
                self._handle_terragrunt_output_line(line, output)

            # Write all output to file (unlike plan, we keep everything)
            if output:
                output.write(line + "\n")
                output.flush()  # Flush after each line so file is readable while apply is running

    def _handle_terragrunt_output_line(self, line: str, output: Optional[TextIO]) -> None:
        """
        Handle a single line of normal terragrunt output.

        This method detects the apply/destroy completion lines and
        extracts resource counts.

        Args:
            line: Single line of terragrunt output
            output: Optional file for writing output

        Example completion lines:
            "Apply complete! Resources: 0 added, 11 changed, 0 destroyed."
            "Destroy complete! Resources: 5 destroyed."
        """
        # Detect completion lines
        if line.startswith("Apply complete!") or line.startswith("Destroy complete!"):
            self.summary = line

            # Parse resource counts
            # Format: "N added", "N destroyed", "N changed"
            for change_type in CHANGE_TYPES_PAST:
                match = re.search(rf"(\d+) {change_type}", line)
                if match:
                    self.counts[change_type] = int(match.group(1))

            # Determine status based on what changed
            if "Resources: 0 added, 0 changed, 0 destroyed" in line:
                # No actual changes made
                self.status = PlanStatus.NOCHANGE
            else:
                self.status = PlanStatus.SUCCESS

    def _get_terragrunt_cache(self) -> Optional[str]:
        """
        Get the terragrunt cache directory path for this provider.

        Returns:
            Path to cache directory, or None if not found
        """
        try:
            provider_path = self.project.root / self.provider
            result = subprocess.run(
                ["terragrunt", "terragrunt-info"],
                cwd=provider_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return None

            tg_info = json.loads(result.stdout)
            dl_dir = tg_info.get("DownloadDir")
            wk_dir = tg_info.get("WorkingDir")

            if not dl_dir or not wk_dir:
                return None

            # Extract cache subdirectory
            pattern = re.compile(rf"^{re.escape(dl_dir)}/([^/]+/[^/]+)/")
            match = pattern.match(wk_dir)
            if match:
                cache_dir = os.path.join(dl_dir, match.group(1))
                if os.path.isdir(cache_dir):
                    debug(f"{self.provider}: Removing terragrunt cache {cache_dir}")
                    return cache_dir

        except Exception as e:
            error(f"{self.provider}: Error clearing terragrunt cache: {e}")

        return None

    def _stop_terragrunt(self) -> None:
        """
        Stop the terragrunt process if it's still running.

        Sends SIGTERM signal to gracefully terminate the process.
        """
        if self.pid is not None:
            try:
                import signal
                os.kill(self.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass  # Process already dead
            self.pid = None


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _shell_quote(s: str) -> str:
    """
    Quote a string for safe use in shell commands.

    Args:
        s: String to quote

    Returns:
        Safely quoted string for shell use
    """
    if not s:
        return "''"

    # If string contains special characters, wrap in single quotes
    if re.search(r"[^a-zA-Z0-9_\-./=]", s):
        return "'" + s.replace("'", "'\"'\"'") + "'"

    return s
