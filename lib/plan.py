"""
Single Terragrunt plan execution.

This module handles running a single terragrunt plan for one provider:
- Executing the terragrunt plan command
- Parsing and formatting the plan output
- Tracking plan status (success, error, no-change)
- Masking sensitive information
- Managing plan dependencies (modules, variables)

PYTHON CONCEPTS FOR GO/BASH DEVELOPERS:
---------------------------------------
1. TYPE_CHECKING - Used for type hints that would cause circular imports
2. TextIO - Type hint for file-like objects (like io.Writer in Go)
3. Dict comprehensions - {k: v for k in items} creates a dict in one line
4. subprocess.Popen - Lower-level process control (vs subprocess.run)
5. Context managers - 'with open() as f:' ensures file is closed properly
"""

# =============================================================================
# IMPORTS
# =============================================================================

import os           # Operating system interface
import re           # Regular expressions
import json         # JSON encoding/decoding
import shutil       # High-level file operations
import hashlib      # Hash functions (MD5, SHA, etc.)
import subprocess   # Running external commands
import zlib         # Compression (not used in this version, kept for compatibility)
from pathlib import Path  # Cross-platform path handling
from typing import Optional, List, Dict, Any, TextIO, TYPE_CHECKING

# Import constants and utilities from our common module
from .common import (
    TFHCL,                    # terragrunt.hcl filename
    TFVARS_MARKER,            # Marker for variables in plan file
    TFTARGET_MARKER,          # Marker for targets in plan file
    DESTROY_MARKER,           # Marker for destroy plans
    CHANGE_TYPES,             # ["add", "destroy", "recreate", "change", "read"]
    CHANGE_TEXT_REGEX,        # Regex for parsing change descriptions
    CHANGE_SYMBOL_REGEX,      # Regex for parsing change symbols
    TF_START_PLAN_REGEX,      # Regex for plan section start
    TF_END_PLAN_REGEX,        # Regex for plan section end
    SOURCE_REGEX,             # Regex for source attribute
    VAR_FILE_REGEX,           # Regex for var-file arguments
    HCL_FILE_REGEX,           # Regex for HCL file references
    PlanStatus,               # Status enum (SUCCESS, ERROR, NOCHANGE, DISCARDED)
    debug,                    # Debug logging
    info,                     # Info logging
    warn,                     # Warning logging
    error,                    # Error logging
    is_verbose,               # Check verbose mode
    strip_ansi,               # Remove ANSI color codes
    short_resource,           # Shorten resource names
    empty_dir,                # Check if directory is empty
)

# TYPE_CHECKING is a special constant that is True only during type checking
# (like when your IDE analyzes the code), but False at runtime.
# This is used to avoid circular imports while still having type hints.
#
# Circular import problem:
#   - plan.py imports project.py for TerraformProject type hint
#   - project.py might import plan.py
#   - This would cause an import error at runtime
#
# Solution:
#   - Only import during type checking, not at runtime
#   - Use string quotes around the type: "TerraformProject"
if TYPE_CHECKING:
    from .project import TerraformProject


# =============================================================================
# TERRAGRUNTPLAN CLASS
# =============================================================================

class TerragruntPlan:
    """
    Represents a single terragrunt plan operation for one provider.

    A "provider" in our context is a directory containing a terragrunt.hcl file,
    typically representing a specific service in a specific environment/region.
    Example: providers/dev/us-east-1/my-service

    This class handles:
    1. Building the terragrunt command
    2. Running the command and capturing output
    3. Parsing the output to extract:
       - Status (success, error, no-change)
       - Resource changes (add, destroy, change, etc.)
       - Error messages
    4. Writing results to a plan file for later review/apply

    Lifecycle:
        1. Create instance with provider path and options
        2. Call run() to execute the plan
        3. Check status, summary, changes, errors for results
        4. Optionally call prune() to clean up if no changes

    Example:
        plan = TerragruntPlan(project, "INFRA-1234", "providers/dev/us-east-1/svc", options)
        plan.run()
        if plan.status == PlanStatus.SUCCESS:
            print(f"Changes: {plan.summary}")
        elif plan.status == PlanStatus.ERROR:
            print(f"Error: {plan.errors}")
    """

    def __init__(
        self,
        project: "TerraformProject",  # Quoted because of TYPE_CHECKING import
        ticket: str,
        provider: str,
        cmd_options: Dict[str, Any],
    ):
        """
        Initialize a plan for a single provider.

        Args:
            project: The TerraformProject instance (provides paths, git info)
            ticket: JIRA ticket number (e.g., "INFRA-1234")
            provider: Provider path relative to project root
                     (e.g., "providers/dev/us-east-1/my-service")
            cmd_options: Dictionary of command-line options including:
                - destroy: bool - Generate destroy plan
                - tfvars: dict - Terraform variables to pass
                - tftargets: list - Specific resources to target
                - tg_cache: bool - Keep terragrunt cache
                - refresh: bool - Force terraform refresh
                - tags_only: bool - Only keep tag-change plans
        """
        # Store references
        self.project = project      # TerraformProject instance
        self.ticket = ticket        # JIRA ticket number
        self.provider = provider    # Provider path (e.g., "providers/dev/us-east-1/svc")
        self.cmd_options = cmd_options  # Command-line options dictionary

        # -------------------------------------------------------------------------
        # OUTPUT PATHS
        # -------------------------------------------------------------------------
        # Plan output is organized by ticket:
        #   tickets/INFRA-1234/providers/dev/us-east-1/my-service/my-service.txt

        # Directory for plan output
        # Path() creates a path object, / operator joins paths
        self.plandir = Path("tickets") / ticket / provider

        # Plan output file (text file with terragrunt output)
        # Path.name gets just the filename part (e.g., "my-service" from full path)
        self.planfile = self.plandir / f"{Path(provider).name}.txt"

        # For plans, output file is the same as plan file
        # (Apply uses .apply extension, so they differ in apply.py)
        self.outputfile = self.planfile

        # Short version of output path for display
        self.outputfile_s = f"{provider}/{Path(provider).name}.txt"

        # -------------------------------------------------------------------------
        # PLAN BINARY PATH
        # -------------------------------------------------------------------------
        # Terraform plan can save a binary plan file for later apply.
        # We use a hash of the provider path to avoid filename conflicts.

        # MD5 hash of provider path for unique temp filename
        # hashlib.md5(...).hexdigest() returns 32-char hex string
        provider_hash = hashlib.md5(provider.encode()).hexdigest()
        self.plan_bin = Path("/tmp") / f"tfplan-{provider_hash}.bin"

        # -------------------------------------------------------------------------
        # PROVIDER CONFIG
        # -------------------------------------------------------------------------
        # Path to the terragrunt.hcl file for this provider
        self.provider_file = Path(provider) / TFHCL

        # -------------------------------------------------------------------------
        # EXECUTION STATE
        # -------------------------------------------------------------------------
        self.should_run = True          # Whether to execute this plan
        self.cancel = False             # Set to True to cancel/interrupt plan
        self.replan = False             # Whether this is a replan
        self.pid: Optional[int] = None  # Process ID when running
        self.exitstatus: Optional[int] = None  # Exit code after completion

        # -------------------------------------------------------------------------
        # RESULTS
        # -------------------------------------------------------------------------
        self.status: Optional[str] = None   # PlanStatus value after run
        self.summary: Optional[str] = None  # One-line summary (e.g., "Plan: 2 to add")
        self.errors: List[str] = []         # Error messages if status is ERROR
        self.changes: List[str] = []        # List of resource changes

        # -------------------------------------------------------------------------
        # RESOURCE TRACKING
        # -------------------------------------------------------------------------
        # Track resources by change type and resource type
        # Example: resources["add"]["aws_instance"] = 3 means 3 instances to add
        #
        # Dict comprehension: {key: value for item in iterable}
        # This creates: {"add": {}, "destroy": {}, "recreate": {}, ...}
        self.resources: Dict[str, Dict[str, int]] = {t: {} for t in CHANGE_TYPES}

        # Simple counts per change type
        # Example: counts["add"] = 5 means 5 resources to add
        self.counts: Dict[str, int] = {t: 0 for t in CHANGE_TYPES}

        # -------------------------------------------------------------------------
        # LAZY-LOADED DEPENDENCIES
        # -------------------------------------------------------------------------
        # These are parsed from terragrunt.hcl on first access.
        # Using None means "not yet loaded" vs [] meaning "loaded but empty"
        self._modules: Optional[List[str]] = None  # Module dependencies
        self._vars: Optional[List[str]] = None     # Variable file dependencies

        # -------------------------------------------------------------------------
        # INITIALIZE COMMAND OPTIONS
        # -------------------------------------------------------------------------
        # Copy options from cmd_options dict to instance attributes
        self.reset_cmd_options()

        # -------------------------------------------------------------------------
        # OUTPUT PARSING STATE
        # -------------------------------------------------------------------------
        # These track state while parsing terragrunt output line by line
        self._in_plan = False           # Currently in plan output section
        self._in_error = False          # Currently collecting error lines
        self._tg_dir: Optional[str] = None  # Terragrunt working directory

    # =========================================================================
    # SPECIAL METHODS
    # =========================================================================

    def __str__(self) -> str:
        """
        String representation of the plan (for logging/display).

        Returns:
            Provider path, prefixed with "(Destroy)" if this is a destroy plan

        Example:
            str(plan)  # Returns: "(Destroy) providers/dev/us-east-1/svc"
        """
        prefix = "(Destroy) " if self.destroy else ""
        return f"{prefix}{self.provider}"

    # =========================================================================
    # PROPERTIES (lazy-loaded attributes)
    # =========================================================================

    @property
    def modules(self) -> List[str]:
        """
        Get module dependencies (parsed lazily on first access).

        This property uses "lazy loading" - the dependencies are only parsed
        from the terragrunt.hcl file when first accessed, not during __init__.

        Returns:
            List of module paths that this provider depends on

        Python Property Notes:
            - @property makes this method callable like an attribute
            - plan.modules instead of plan.modules()
            - The first access triggers parse_dependencies()
        """
        if self._modules is None:
            self.parse_dependencies()
        return self._modules or []  # Return empty list if still None

    @property
    def vars(self) -> List[str]:
        """
        Get variable file dependencies (parsed lazily on first access).

        Returns:
            List of .tfvars file paths that this provider uses
        """
        if self._vars is None:
            self.parse_dependencies()
        return self._vars or []

    @property
    def account(self) -> str:
        """
        Get account name (dev, prod, etc.) from provider path.

        Provider paths follow the structure: providers/{account}/{region}/{service}
        This extracts the {account} part.

        Returns:
            Account name (e.g., "dev", "prod") or empty string if not found

        Example:
            plan.provider = "providers/dev/us-east-1/my-service"
            plan.account  # Returns: "dev"
        """
        parts = self.provider.split("/")
        # Index 0 is "providers", index 1 is account
        return parts[1] if len(parts) > 1 else ""

    @property
    def region(self) -> Optional[str]:
        """
        Extract AWS region from provider path.

        Matches AWS region patterns like:
        - us-east-1, us-west-2 (standard regions)
        - eu-central-1 (European regions)
        - us-gov-west-1 (GovCloud regions)

        Returns:
            Region string or None if not found

        Example:
            plan.provider = "providers/dev/us-east-1/my-service"
            plan.region  # Returns: "us-east-1"
        """
        # Regex pattern for AWS regions
        # (?P<region>...) is a named capture group
        pattern = re.compile(
            r"/(?P<region>[a-z]{2}-(?:gov-)?(?:(?:north|south|)(?:east|west|)|central)(?<=[a-z])-\d)[-/]"
        )
        match = pattern.search(self.provider)
        return match.group("region") if match else None

    @property
    def sort_key(self) -> str:
        """
        Key for sorting plans (used to order plan execution).

        Returns:
            The provider path (plans are sorted alphabetically by provider)
        """
        return self.provider

    # =========================================================================
    # COMMAND OPTIONS
    # =========================================================================

    def has_cmd_options(self) -> bool:
        """
        Check if plan has special command-line options that should be preserved.

        Used during replan to determine if we should keep the plan even if
        it has no changes (because the options are significant).

        Returns:
            True if this is a destroy plan or has custom variables
        """
        return self.destroy or bool(self.tfvars)

    def reset_cmd_options(self) -> None:
        """
        Reset command options from the cmd_options dictionary.

        This copies values from cmd_options dict to instance attributes.
        Called during __init__ and when resetting for replan.
        """
        self.destroy = self.cmd_options.get("destroy", False)
        self.tfvars = self.cmd_options.get("tfvars", {}).copy()  # Copy to avoid mutation
        self.tftargets = self.cmd_options.get("tftargets", []).copy()

    def parse_cmd_options(self) -> None:
        """
        Parse command-line options from a previously saved plan file.

        When replanning, we read the old plan file to extract options like:
        - Whether it was a destroy plan
        - What variables were passed
        - What targets were specified

        These options are stored as special marker lines at the top of the file.
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
                    # Parse JSON after the marker
                    try:
                        self.tfvars = json.loads(line[len(TFVARS_MARKER):])
                    except json.JSONDecodeError:
                        pass  # Ignore invalid JSON
                elif line.startswith(TFTARGET_MARKER):
                    try:
                        self.tftargets = json.loads(line[len(TFTARGET_MARKER):])
                    except json.JSONDecodeError:
                        pass

    # =========================================================================
    # DEPENDENCY PARSING
    # =========================================================================

    def parse_dependencies(self) -> None:
        """
        Parse module and variable file dependencies from the provider's terragrunt.hcl.

        Dependencies are important for:
        - Determining which plans need to rerun when a module changes
        - Understanding the relationship between providers and modules

        This parses the terragrunt.hcl file looking for:
        - source = "..." lines (module dependencies)
        - read_terragrunt_config("...") calls (HCL dependencies)
        - -var-file=... references (variable file dependencies)
        """
        # Only parse once
        if self._modules is not None:
            return

        self._modules = []
        self._vars = []

        # Build full path to provider file
        provider_file = self.project.root / self.provider_file
        if not provider_file.exists():
            debug(f"  parse_dependencies: file not found: {provider_file}")
            return

        debug(f"  parse_dependencies: reading {provider_file}")

        # Read and parse the file line by line
        with open(provider_file, "r") as f:
            for line in f:
                # Check for source attribute (module dependency)
                match = SOURCE_REGEX.search(line)
                if match:
                    # Clean up source path (remove double slashes)
                    source_path = match.group(2).replace("//", "/")
                    # Expand terragrunt path functions
                    expanded = self._parse_tf_path(source_path)
                    # Resolve to absolute path then back to relative
                    dep = self.project.relative_path(
                        str((self.project.root / self.provider / expanded).resolve())
                    )
                    debug(f"  parse_dependencies: source='{source_path}' -> dep='{dep}'")
                    # Recursively parse module dependencies
                    self._parse_module_dependencies(dep)

                # Check for variable files (both var-file and HCL includes)
                for regex in [VAR_FILE_REGEX, HCL_FILE_REGEX]:
                    match = regex.search(line)
                    if match:
                        var_path = self._parse_tf_path(match.group(2))
                        self._vars.append(self.project.relative_path(var_path))

    def _parse_tf_path(self, path: str) -> str:
        """
        Convert terragrunt path expressions to actual file paths.

        Terragrunt uses special functions in paths:
        - ${get_terragrunt_dir()} - Current terragrunt directory
        - ${get_parent_terragrunt_dir()} - Parent terragrunt directory
        - ${get_repo_root()} - Git repository root directory

        Args:
            path: Path string potentially containing terragrunt functions

        Returns:
            Path with functions replaced by actual directory paths

        Example:
            "${get_repo_root()}/modules/svc"
            becomes
            "/full/path/to/project-root/modules/svc"
        """
        def replace_func(m):
            """
            Replacement function for re.sub().

            Args:
                m: Match object from regex

            Returns:
                Replacement string

            Python Note:
                re.sub() can take a function as the replacement argument.
                The function receives the match object and returns the replacement.
            """
            func_name = m.group(1)
            if func_name == "get_terragrunt_dir":
                # Current provider directory
                return str((self.project.root / self.provider).resolve())
            elif func_name == "get_parent_terragrunt_dir":
                # Parent account directory (e.g., providers/dev)
                return str((self.project.root / "providers" / self.account).resolve())
            elif func_name == "get_repo_root":
                # Git repository root — same as project root
                return str(self.project.root.resolve())
            return m.group(0)  # Return original if unknown function

        # Pattern to match ${function_name()}
        pattern = re.compile(r"\$\{(get_terragrunt_dir|get_parent_terragrunt_dir|get_repo_root)\(\)\}")
        return pattern.sub(replace_func, path)

    def _parse_module_dependencies(self, module: str) -> None:
        """
        Recursively parse module dependencies.

        Modules can reference other modules via 'source' attribute.
        This function follows those references to build a complete dependency list.

        Args:
            module: Relative path to a module directory

        Example dependency chain:
            providers/dev/svc references modules/svc
            modules/svc references modules/common
            Result: _modules = ["modules/svc", "modules/common"]
        """
        # Avoid infinite loops from circular dependencies
        if module in self._modules:
            return

        # Add this module to the list
        self._modules.append(module)

        # Look for nested module sources in .tf files
        module_path = self.project.root / module
        if not module_path.exists():
            return

        # Search all .tf files in the module
        for tf_file in module_path.glob("*.tf"):
            with open(tf_file, "r") as f:
                for line in f:
                    match = SOURCE_REGEX.search(line)
                    if match:
                        # Found another source reference, recurse
                        dep = self.project.relative_path(
                            str((module_path / match.group(2)).resolve())
                        )
                        self._parse_module_dependencies(dep)

    # =========================================================================
    # FILTERING
    # =========================================================================

    def matches_filter(self, pattern: re.Pattern) -> bool:
        """
        Check if this plan matches a filter pattern.

        Used for --filter and --exclude-filter options to include/exclude
        specific providers from the plan run.

        Args:
            pattern: Compiled regex pattern to match against

        Returns:
            True if provider path or any variable file matches the pattern

        Example:
            pattern = re.compile(r"/dev/")
            plan.matches_filter(pattern)  # True if provider is in dev
        """
        # Check provider path
        if pattern.search(self.provider):
            return True
        # Check variable file paths
        # any() returns True if any item in the iterable is True
        return any(pattern.search(v) for v in self.vars)

    # =========================================================================
    # STATUS AND DISPLAY
    # =========================================================================

    def statusline(self) -> str:
        """
        Generate a one-line summary of the plan result.

        Used for progress display and final summary.

        Returns:
            Status line like: "Plan: 2 to add - providers/dev/us-east-1/svc/svc.txt"
                          or: "(Not re-run) providers/dev/us-east-1/svc/svc.txt"
        """
        parts = []

        if not self.should_run:
            parts.append("(Not re-run)")

        if self.status == PlanStatus.ERROR and self.errors:
            # Show first error
            parts.append(f"{self.errors[0]} -")

        if self.status == PlanStatus.SUCCESS and self.summary:
            # Show plan summary
            parts.append(f"{self.summary} -")

        # Always include output file path
        parts.append(self.outputfile_s)

        return " ".join(parts)

    # =========================================================================
    # MAIN RUN METHOD
    # =========================================================================

    def run(self) -> None:
        """
        Run the terragrunt plan and capture output.

        This is the main entry point for executing a plan. It:
        1. Creates the output directory
        2. Opens the output file for writing
        3. Runs terragrunt and captures output
        4. Handles errors and cleanup

        After calling run(), check:
        - self.status: SUCCESS, ERROR, or NOCHANGE
        - self.summary: One-line result summary
        - self.errors: List of error messages (if ERROR)
        - self.changes: List of resource changes (if SUCCESS)

        Python Exception Handling:
            try:
                # Code that might raise exception
            except ExceptionType as e:
                # Handle exception
            finally:
                # Always runs, even if exception
        """
        try:
            # Create output directory (parents=True creates parent dirs too)
            # exist_ok=True doesn't error if directory exists
            self.plandir.mkdir(parents=True, exist_ok=True)

            # Open output file for writing
            # 'with' statement ensures file is closed even if exception occurs
            with open(self.outputfile, "w") as output:
                # Check if cancelled before even starting
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
            # Get full stack trace for debugging
            import traceback
            self.errors.extend(traceback.format_exc().split("\n"))
            error(f"{self}: {e}")

        finally:
            # Always clean up, even if exception
            self._stop_terragrunt()  # Kill process if still running
            self._post_run()         # Post-processing
            # Terragrunt copies .terraform.lock.hcl back to the provider directory
            # after init (intended for VCS), but we want it in /tmp/terragrunt_cache.
            # Delete it here so provider directories stay clean.
            lock_file = self.project.root / self.provider / ".terraform.lock.hcl"
            if lock_file.exists():
                debug(f"Removing .terraform.lock.hcl from {self.provider}")
                lock_file.unlink()

    def _run_terragrunt(self, output: TextIO) -> None:
        """
        Execute the terragrunt plan command and parse output.

        Args:
            output: File object to write plan output to

        This method:
        1. Builds the terragrunt command with all options
        2. Optionally clears the terragrunt cache
        3. Runs terragrunt init (and optionally refresh)
        4. Runs terragrunt plan
        5. Parses output line by line

        Python TextIO:
            TextIO is a type hint for file-like objects that handle text.
            Any object with read() and write() methods for text qualifies.
            This includes open() results, sys.stdout, StringIO, etc.
        """
        # -------------------------------------------------------------------------
        # BUILD COMMAND
        # -------------------------------------------------------------------------
        # Start with base terragrunt plan command
        tf_cmd = ["terragrunt", "plan", "-input=false", "-no-color"]

        # Save plan to binary file (for later apply)
        tf_cmd.append(f"-out={self.plan_bin}")

        # Add destroy flag if this is a destroy plan
        if self.destroy:
            tf_cmd.append("-destroy")

        # Add terraform variables
        # dict.items() returns (key, value) pairs
        for key, value in self.tfvars.items():
            tf_cmd.extend(["-var", f"{key}={value}"])

        # Add target resources (for targeted plans)
        for target in self.tftargets:
            tf_cmd.append(f"-target={target}")

        # -------------------------------------------------------------------------
        # PREPARE EXECUTION
        # -------------------------------------------------------------------------
        # Get full path to provider directory
        provider_path = self.project.root / self.provider

        # Initialize output parsing state
        self._init_terragrunt_parse(output)

        # Copy current environment
        env = os.environ.copy()

        # Redirect terragrunt cache to /tmp so it never pollutes provider directories.
        # By default, terragrunt creates .terragrunt-cache and .terraform.lock.hcl
        # inside the provider directory. TG_DOWNLOAD_DIR overrides that.
        # Terragrunt automatically creates provider-specific subdirectories under
        # this path, so parallel plans won't conflict with each other.
        # Note: TERRAGRUNT_DOWNLOAD_DIR is the old name (deprecated), TG_DOWNLOAD_DIR is current.
        env.setdefault("TG_DOWNLOAD_DIR", "/tmp/terragrunt_cache")

        # -------------------------------------------------------------------------
        # BUILD SHELL COMMAND SEQUENCE
        # -------------------------------------------------------------------------
        # We chain multiple commands with && (run next only if previous succeeds)
        commands = []

        # Optionally clear terragrunt cache
        if not self.cmd_options.get("tg_cache", True):
            cache_dir = self._get_terragrunt_cache()
            if cache_dir:
                commands.append(f"rm -rf {_shell_quote(cache_dir)}")

        # Always run init first
        commands.append("terragrunt init -input=false -no-color")

        # Optional refresh before plan
        if self.cmd_options.get("refresh"):
            commands.append("terragrunt refresh -input=false -no-color")

        # Main plan command (quote each argument for shell safety)
        commands.append(" ".join(_shell_quote(c) for c in tf_cmd))

        # Join commands with && (only continue if previous succeeded)
        full_cmd = " && ".join(commands)

        # -------------------------------------------------------------------------
        # EXECUTE COMMAND
        # -------------------------------------------------------------------------
        # subprocess.Popen gives us more control than subprocess.run():
        # - We can read output line by line as it's produced
        # - We can access the process ID (pid)
        # - We can kill the process if needed
        #
        # Popen arguments:
        #   shell=True: Run as shell command (needed for && chaining)
        #   cwd: Working directory
        #   stdout=PIPE: Capture stdout
        #   stderr=STDOUT: Redirect stderr to stdout (combine them)
        #   stdin=DEVNULL: Don't wait for input
        #   text=True: Return strings instead of bytes
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

        # Store process ID for potential cancellation
        self.pid = process.pid

        # -------------------------------------------------------------------------
        # PARSE OUTPUT
        # -------------------------------------------------------------------------
        # Read and parse output line by line
        if process.stdout:
            self._parse_terragrunt_output(process.stdout, output)

        # Wait for process to complete
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
        elif self.summary is None:
            # Plan completed but we couldn't parse the result
            err = "Could not determine terragrunt result"
            self.status = PlanStatus.ERROR
            self.errors.append(err)
            output.write(f"\nError: {err}\n")

    def _init_terragrunt_parse(self, output: TextIO) -> None:
        """
        Initialize parsing state and write header to output file.

        Args:
            output: File to write header to

        Writes special marker lines at the top of the plan file:
        - DESTROY_MARKER if this is a destroy plan
        - TFVARS_MARKER with JSON-encoded variables
        - TFTARGET_MARKER with JSON-encoded targets

        These markers allow us to reconstruct the command options during replan.
        """
        self._in_plan = False

        # Write command options as markers for replan
        if self.destroy:
            output.write(DESTROY_MARKER)
        if self.tfvars:
            output.write(f"{TFVARS_MARKER}{json.dumps(self.tfvars)}\n")
        if self.tftargets:
            output.write(f"{TFTARGET_MARKER}{json.dumps(self.tftargets)}\n")

    def _parse_terragrunt_output(self, input_stream: TextIO, output: Optional[TextIO] = None) -> None:
        """
        Parse terragrunt output stream line by line.

        This is the main output parsing loop. It:
        1. Reads each line from terragrunt's output
        2. Detects errors, status, and resource changes
        3. Optionally writes to output file

        Args:
            input_stream: Stream to read terragrunt output from
            output: Optional file to write filtered output to
        """
        self._in_error = False

        # Read line by line
        for line in input_stream:
            # Check for cancellation signal
            if self.cancel:
                self.status = PlanStatus.ERROR
                msg = "Interrupted by signal"
                self.errors.append(msg)
                if output:
                    output.write(f"Error: {msg}\n")
                self._stop_terragrunt()
                return

            # Print to console if verbose mode
            if is_verbose():
                print(line, end="")  # Line already has newline

            # Clean up line
            line = line.rstrip("\n\r")  # Remove trailing newlines
            line = strip_ansi(line)     # Remove color codes

            # Strip terragrunt output prefix
            # Terragrunt prefixes lines with timestamp and stream info:
            #   "23:50:15.534 STDOUT terraform: Plan: 27 to add..."
            #   "23:50:15.534 STDERR tofu: Error: ..."
            # We need to extract just the actual content after the prefix.
            tg_prefix_match = re.match(
                r"^\d{2}:\d{2}:\d{2}\.\d+ (?:STDOUT|STDERR) (?:terraform|tofu|terragrunt): (.*)$",
                line,
            )
            if tg_prefix_match:
                line = tg_prefix_match.group(1)

            # Detect working directory
            match = re.search(r"Setting working directory to (.+)", line)
            if match:
                self._tg_dir = match.group(1)

            # -------------------------------------------------------------------------
            # ERROR DETECTION
            # -------------------------------------------------------------------------
            if re.match(r"^Error:|^Usage:|^There are some problems with the configuration", line):
                self.status = PlanStatus.ERROR
                if line != "Error: ":  # Ignore empty error prefix
                    self.errors.append(line)
                self._in_error = True

            elif "TERRAFORM CRASH" in line:
                # Terraform crashed - serious error
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

            # Write to output file
            if output and (self._in_error or self._line_printable(line)):
                output.write(line + "\n")

    def _handle_terragrunt_output_line(self, line: str, output: Optional[TextIO]) -> None:
        """
        Handle a single line of normal (non-error) terragrunt output.

        This method detects and parses:
        - Plan summary lines ("Plan: 2 to add, 1 to change")
        - Plan section start/end markers
        - Individual resource change lines
        - No-change messages
        - Output-only changes

        Args:
            line: Single line of terragrunt output
            output: Optional file for writing output
        """
        # -------------------------------------------------------------------------
        # PLAN SUMMARY LINE
        # -------------------------------------------------------------------------
        # Example: "Plan: 2 to add, 1 to change, 0 to destroy."
        if line.startswith("Plan: "):
            self.summary = line
            # Extract counts for each change type
            for change_type in CHANGE_TYPES:
                # Match patterns like "2 to add", "1 to destroy"
                match = re.search(rf"(\d+) to {change_type}", line)
                if match:
                    self.counts[change_type] = int(match.group(1))

        # -------------------------------------------------------------------------
        # PLAN SECTION MARKERS
        # -------------------------------------------------------------------------
        elif line == "Terraform will perform the following actions:":
            self.status = PlanStatus.SUCCESS
            self._in_plan = True

        elif line == "Note: Objects have changed outside of Terraform":
            self.status = PlanStatus.SUCCESS
            self.summary = line

        elif line in [
            "No changes. Infrastructure is up-to-date.",
            "No changes. Your infrastructure matches the configuration.",
        ]:
            self.status = PlanStatus.NOCHANGE
            self.summary = line
            self._in_plan = True

        # -------------------------------------------------------------------------
        # RESOURCE CHANGES
        # -------------------------------------------------------------------------
        elif self._in_plan:
            # Match resource change lines like:
            # "  # aws_instance.web will be created"
            match = CHANGE_TEXT_REGEX.match(line)
            if match:
                resource = match.group(1)      # e.g., "aws_instance.web"
                action_text = match.group(2)   # e.g., "will be created"

                # Determine operation and display symbol
                if action_text in ["will be updated in-place", "has been changed"]:
                    operation, symbol = "change", "~"
                elif action_text == "will be destroyed":
                    operation, symbol = "destroy", "-"
                elif action_text == "will be created":
                    operation, symbol = "add", "+"
                elif "must be replaced" in action_text:
                    operation, symbol = "recreate", "-/+"
                elif action_text == "will be read during apply":
                    operation, symbol = "read", "<="
                else:
                    operation, symbol = None, None

                if operation and symbol:
                    # Add to changes list
                    self.changes.append(f"{symbol} {resource}")

                    # Count by resource type
                    short_res = short_resource(resource)
                    if short_res not in self.resources[operation]:
                        self.resources[operation][short_res] = 0
                    self.resources[operation][short_res] += 1

        # -------------------------------------------------------------------------
        # OUTPUT-ONLY CHANGES
        # -------------------------------------------------------------------------
        elif line == "Changes to Outputs:":
            if self.status is None:
                self.status = PlanStatus.SUCCESS
            if self.summary is None:
                self.summary = line
            self._in_plan = True

    def _line_printable(self, line: str) -> bool:
        """
        Determine if a line should be included in the plan output file.

        We only want to keep the actual plan details, not all the init/setup output.
        This tracks when we're in the plan section and returns True for those lines.

        Args:
            line: Line to check

        Returns:
            True if line should be written to output file
        """
        if TF_START_PLAN_REGEX.search(line):
            self._in_plan = True
        elif TF_END_PLAN_REGEX.search(line):
            self._in_plan = False
        return self._in_plan

    def _get_terragrunt_cache(self) -> Optional[str]:
        """
        Get the terragrunt cache directory path for this provider.

        Terragrunt caches downloaded modules and providers. Sometimes we need
        to clear this cache to ensure fresh downloads.

        Returns:
            Path to cache directory, or None if not found

        Uses 'terragrunt terragrunt-info' command to get cache info.
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

            # Parse JSON output
            tg_info = json.loads(result.stdout)
            dl_dir = tg_info.get("DownloadDir")  # e.g., /tmp/.terragrunt-cache
            wk_dir = tg_info.get("WorkingDir")   # e.g., /tmp/.terragrunt-cache/abc123/xyz

            if not dl_dir or not wk_dir:
                return None

            # Extract cache subdirectory (abc123/xyz from the example)
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
        Used when cancelling a plan or handling errors.

        Python Signal Notes:
            - os.kill(pid, signal) sends a signal to a process
            - signal.SIGTERM is a termination request
            - OSError/ProcessLookupError raised if process doesn't exist
        """
        if self.pid is not None:
            try:
                import signal
                os.kill(self.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass  # Process already dead
            self.pid = None

    def _post_run(self) -> None:
        """
        Post-processing after plan completes.

        Currently handles --tags-only filtering:
        If the plan only has tag changes (no actual infrastructure changes),
        and --tags-only is set, mark the plan as DISCARDED.
        """
        # Handle --tags-only filtering
        if (
            self.status == PlanStatus.SUCCESS
            and self.cmd_options.get("tags_only")
            and self.plan_bin.exists()
        ):
            try:
                provider_path = self.project.root / self.provider
                # Get plan in JSON format for detailed analysis
                result = subprocess.run(
                    ["terragrunt", "show", "-json", str(self.plan_bin)],
                    cwd=provider_path,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    plan_json = json.loads(result.stdout)
                    resource_changes = plan_json.get("resource_changes", [])
                    # Check if ALL changes are tag-only
                    # all() returns True if all items are True
                    if not all(self._is_tag_only_change(res) for res in resource_changes):
                        self.status = PlanStatus.DISCARDED
            except Exception as e:
                error(f"{self}: Error checking tags-only: {e}")

    def _is_tag_only_change(self, resource: Dict[str, Any]) -> bool:
        """
        Check if a resource change only affects tags (no real infrastructure change).

        Args:
            resource: Resource change dictionary from terraform plan JSON

        Returns:
            True if the change only affects tags or is a no-op
        """
        # Skip data sources
        if resource.get("mode") != "managed":
            return True

        change = resource.get("change", {})
        actions = change.get("actions", [])

        # No-op means no change
        if actions == ["no-op"]:
            return True

        # Only consider updates (not create/delete)
        if actions != ["update"]:
            return False

        # Compare before and after states
        # ** unpacks dict, so {**a, **b} merges dicts
        before = {**change.get("before", {}), **change.get("before_sensitive", {})}
        after = {
            **change.get("after", {}),
            **change.get("after_sensitive", {}),
            **change.get("after_unknown", {}),
        }

        # Find all keys that differ
        all_keys = set(before.keys()) | set(after.keys())  # | is set union
        diffs = [k for k in all_keys if before.get(k) != after.get(k)]

        # Remove tag-related keys for AWS resources
        if resource.get("type", "").startswith("aws_"):
            for tag_key in ["tag", "tags", "tags_all"]:
                if tag_key in diffs:
                    diffs.remove(tag_key)

        # If no non-tag differences remain, it's tag-only
        return len(diffs) == 0

    # =========================================================================
    # OUTPUT PARSING (for reading existing plan files)
    # =========================================================================

    def parse_output(self) -> None:
        """
        Parse a previously written plan output file.

        Used during replan to read the status and results from an existing
        plan file without re-running the plan.
        """
        if self.outputfile.exists():
            with open(self.outputfile, "r") as f:
                self._parse_terragrunt_output(f)

    # =========================================================================
    # SUMMARY GENERATION
    # =========================================================================

    def generate_summary(self) -> List[str]:
        """
        Generate summary lines for this plan (for the report file).

        Returns:
            List of summary lines showing either errors or plan changes

        Example output for success:
            ["PLAN SUMMARY:", "+ aws_instance.web", "~ aws_security_group.web",
             "RESULT:", "Plan: 1 to add, 1 to change, 0 to destroy."]

        Example output for error:
            ["ERRORS:", "Error: Invalid provider configuration", ...]
        """
        buffer = []
        if self.status == PlanStatus.ERROR:
            buffer.append("ERRORS:")
            buffer.extend(self.errors)
        else:
            buffer.append("PLAN SUMMARY:")
            buffer.extend(self.changes)
            buffer.append("RESULT:")
            if self.summary:
                buffer.append(self.summary)
        return buffer

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def prune(self) -> None:
        """
        Remove plan output files and empty parent directories.

        Used to clean up no-change plans when --keep-empty is not set.
        This keeps the tickets directory clean and only shows meaningful plans.
        """
        # Remove the output file
        if self.outputfile.exists():
            self.outputfile.unlink()  # unlink() deletes a file

        # Remove empty parent directories
        # Walk up the directory tree and remove empty dirs
        d = self.plandir
        while d != Path(".") and d.exists() and empty_dir(d):
            d.rmdir()      # Remove empty directory
            d = d.parent   # Move to parent directory


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

    Shell quoting is tricky because special characters can cause:
    - Command injection (security issue)
    - Unexpected word splitting
    - Glob expansion

    Examples:
        _shell_quote("hello")         # Returns: hello
        _shell_quote("hello world")   # Returns: 'hello world'
        _shell_quote("it's me")       # Returns: 'it'"'"'s me' (escaped single quote)
    """
    if not s:
        return "''"  # Empty string becomes ''

    # If string contains special characters, wrap in single quotes
    # Special chars: anything that's not alphanumeric, underscore, dash, dot, slash, or equals
    if re.search(r"[^a-zA-Z0-9_\-./=]", s):
        # Single quotes escape everything except single quotes themselves
        # To include a single quote, we: end quotes, add escaped quote, start quotes
        # 'it'"'"'s me' = 'it' + "'" + 's me' = it's me
        return "'" + s.replace("'", "'\"'\"'") + "'"

    return s
