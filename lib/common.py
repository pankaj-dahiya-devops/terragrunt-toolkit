"""
Common utilities, constants, and logging functions for Terragrunt Tools.

This module provides shared functionality used across all terragrunt scripts:
- Logging functions (debug, info, warn, error)
- Constants for parsing terragrunt/terraform output
- Utility functions for file/path operations
- Status enums for tracking plan/apply states

PYTHON CONCEPTS FOR GO/BASH DEVELOPERS:
---------------------------------------
1. `from typing import ...` - Type hints (like Go's type system, but optional in Python)
2. `global` keyword - Accesses module-level variables from within functions
3. `re.compile()` - Pre-compiles regex for better performance (like Go's regexp.MustCompile)
4. Classes without methods are used as "enums" (Python doesn't have built-in enums in older versions)
"""

# =============================================================================
# IMPORTS
# =============================================================================
# In Python, imports are similar to Go's import statements.
# Standard library imports come first, then third-party, then local imports.

import os      # Operating system interface (like os package in Go)
import sys     # System-specific parameters (like os.Args, os.Stdin in Go)
import re      # Regular expressions (like regexp package in Go)
import json    # JSON encoding/decoding (like encoding/json in Go)

# pathlib.Path is Python's modern way to handle file paths
# It's cross-platform (works on Windows, Linux, macOS) - similar to filepath package in Go
from pathlib import Path

# Type hints - these are optional but help with code documentation and IDE support
# Similar to Go's type system but not enforced at runtime
# Optional[str] means "str or None" (like *string in Go that can be nil)
# List[str] is a list of strings (like []string in Go)
# Dict[str, Any] is a map with string keys and any value type (like map[string]interface{} in Go)
from typing import Optional, List, Dict, Any


# =============================================================================
# CONSTANTS
# =============================================================================
# In Python, constants are just regular variables by convention written in UPPERCASE.
# Unlike Go's `const`, Python doesn't enforce immutability - it's just a naming convention.

# The types of changes terraform can make to resources
# Used when parsing plan output to categorize what will happen
CHANGE_TYPES = ["add", "destroy", "recreate", "change", "read"]

# Past tense versions for reporting (e.g., "3 resources added")
CHANGE_TYPES_PAST = ["added", "destroyed", "recreated", "changed", "read"]

# The standard terragrunt configuration filename
# Similar to how Go projects have go.mod, terragrunt projects have terragrunt.hcl
TFHCL = "terragrunt.hcl"

# =============================================================================
# MARKERS FOR PLAN FILE PARSING
# =============================================================================
# These strings are written to plan files to store metadata.
# When we read a plan file later, we look for these markers to extract the info.

# Marker indicating terraform variables passed via command line
TFVARS_MARKER = "Variables from command line: "

# Marker indicating terraform targets (specific resources to plan)
TFTARGET_MARKER = "Targets from command line: "

# Marker indicating this is a destroy plan (will delete resources)
DESTROY_MARKER = "THIS IS A DESTROY PLAN!!!\n"

# =============================================================================
# REPORT FILE HEADERS
# =============================================================================
# These are section headers used in the summary report file.
# The report file provides a quick overview of all plans that were run.

ERROR_PLANS_HEADER = "ERROR PLANS:"           # Plans that failed with errors
NO_CHANGE_PLANS_HEADER = "No change plans:"   # Plans with no infrastructure changes
SUCCESSFUL_PLANS_HEADER = "Successful plans:" # Plans that succeeded with changes
REPORT_FILE = "plan_all_report.txt"           # Default report filename
SKIPPED_PLANS_HEADER = "Plans skipped due to previous errors:"  # Plans not run due to --stop-on-error
PLAN_SUMMARIES_HEADER = "Individual plan summaries:"  # Detailed breakdown per plan

# =============================================================================
# REGEX PATTERNS
# =============================================================================
# Pre-compiled regular expressions for parsing terraform/terragrunt output.
#
# re.compile() pre-compiles the regex pattern for better performance when
# the same pattern is used multiple times. Similar to regexp.MustCompile() in Go.
#
# In Python regex:
# - r"..." is a "raw string" - backslashes are literal (no need to escape them)
# - ^ matches start of line, $ matches end of line
# - \s matches whitespace, \w matches word characters
# - (.+?) is a non-greedy capture group (captures as few chars as possible)
# - (?:...) is a non-capturing group (groups but doesn't capture)

# Matches terraform change symbols at the start of a line:
#   ~ = update in-place
#   - = destroy
#   + = create
#   -/+ or +/- = recreate (destroy then create)
#   <= = read (data source)
CHANGE_SYMBOL_REGEX = re.compile(r"^\s*(~|-|\+|-/\+|\+/-|<=) \w")

# Matches terraform's human-readable change descriptions like:
#   "# aws_instance.example will be destroyed"
#   "# aws_s3_bucket.mybucket will be created"
CHANGE_TEXT_REGEX = re.compile(
    r"^  # (.+?) (will be updated in-place|will be destroyed|will be created|"
    r"(?:is tainted, so )?must be replaced|will be read during apply|has been changed)$"
)

# Matches the 'source' attribute in terragrunt.hcl files
# Example: source = "../modules/my-module"
SOURCE_REGEX = re.compile(r'^\s*source\s*=\s*([\'"])(.+)\1')

# Matches -var-file arguments in terraform commands
# Example: "-var-file=/path/to/vars.tfvars"
VAR_FILE_REGEX = re.compile(r'([\'"])-var-file=(.+)\1')

# Matches read_terragrunt_config() function calls in HCL files
# Example: read_terragrunt_config("../common.hcl")
HCL_FILE_REGEX = re.compile(r'read_terragrunt_config\(([\'"])(.+)\1\)')

# Matches the start of terraform's plan output section
TF_START_PLAN_REGEX = re.compile(r"Terraform will perform the following actions:")

# Matches lines that indicate the end of the terraform plan section
# Multiple patterns are joined with | (OR) to catch different scenarios
TF_END_PLAN_REGEX = re.compile(
    r"Note: You didn't |Saved the plan to:|Warning: Values? for undeclared variable|"
    r"You can apply this plan|Saved the plan to:|^-{70}"
)

# =============================================================================
# GLOBAL STATE
# =============================================================================
# Module-level variables that track global state.
# In Python, variables defined at module level (outside any function) are "global".
# The underscore prefix (_verbose) is a convention meaning "private/internal use".

# Whether verbose/debug output is enabled
_verbose = False

# Whether we're running in an interactive terminal (vs piped/redirected)
# sys.stdin.isatty() returns True if stdin is connected to a terminal
# This is used to determine if we should prompt for user input
_interactive = sys.stdin.isatty() and sys.stdout.isatty()

# =============================================================================
# ENVIRONMENT CLEANUP
# =============================================================================
# Remove AWS environment variables that might interfere with terragrunt.
# This ensures terragrunt uses its own credential chain (AWS profiles, IAM roles, etc.)
# rather than potentially stale credentials from environment variables.
#
# os.environ is a dict-like object containing all environment variables.
# .pop(key, None) removes the key if it exists, returns None if it doesn't
# (the None default prevents KeyError if the variable doesn't exist)

for env_var in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"]:
    os.environ.pop(env_var, None)


# =============================================================================
# LOGGING FUNCTIONS
# =============================================================================
# These functions provide consistent logging output across all scripts.
# Similar to Go's log package or bash's echo with prefixes.

def set_verbose(value: bool) -> None:
    """
    Enable or disable verbose/debug mode.

    Args:
        value: True to enable verbose output, False to disable

    Note:
        The 'global' keyword is needed to modify a module-level variable
        from within a function. Without it, Python would create a new
        local variable instead of modifying the global one.

        This is different from Go where you can directly modify package-level
        variables, or Bash where all variables are global by default.
    """
    global _verbose
    _verbose = value


def is_verbose() -> bool:
    """
    Check if verbose mode is currently enabled.

    Returns:
        True if verbose mode is enabled, False otherwise
    """
    return _verbose


def is_interactive() -> bool:
    """
    Check if the script is running in an interactive terminal.

    Returns:
        True if both stdin and stdout are connected to a terminal (TTY),
        False if either is piped or redirected

    Use this to decide whether to:
    - Prompt for user input (only if interactive)
    - Show progress bars or spinners (only if interactive)
    - Require --confirm flags (when not interactive)
    """
    return _interactive


def debug(message: str) -> None:
    """
    Print a debug message (only if verbose mode is enabled).

    Args:
        message: The debug message to print

    Example:
        debug("Processing file: config.tf")
        # Output (only if verbose): DEBUG: Processing file: config.tf
    """
    if _verbose:
        # f"..." is an f-string (formatted string literal)
        # Variables inside {} are automatically converted to strings
        # Similar to fmt.Sprintf("DEBUG: %s", message) in Go
        print(f"DEBUG: {message}")


def info(message: str) -> None:
    """
    Print an informational message.

    Args:
        message: The info message to print

    Note:
        If message starts with newline, prints newline first then message.
        This helps with formatting output sections.

    Example:
        info("Starting plan...")
        # Output: INFO: Starting plan...

        info("\\nPhase 2: Applying changes")
        # Output: (blank line)
        #         INFO: Phase 2: Applying changes
    """
    # str() converts any type to string (handles edge cases where message isn't a string)
    message = str(message)

    # Don't print anything for empty messages
    if not message:
        return

    # Handle messages that start with a newline character
    # This provides a visual separator in the output
    if message.startswith("\n"):
        print()  # Print blank line first
        print(f"INFO: {message[1:]}")  # Print message without the leading \n
    else:
        print(f"INFO: {message}")


def warn(message: str) -> None:
    """
    Print a warning message.

    Args:
        message: The warning message to print

    Example:
        warn("Branch is not up to date with remote")
        # Output: WARNING: Branch is not up to date with remote
    """
    print(f"WARNING: {message}")


def error(message: str) -> None:
    """
    Print an error message.

    Args:
        message: The error message to print

    Example:
        error("Failed to run terragrunt: command not found")
        # Output: ERROR: Failed to run terragrunt: command not found
    """
    print(f"ERROR: {message}")


# =============================================================================
# FILE/DIRECTORY UTILITY FUNCTIONS
# =============================================================================

def empty_dir(path: Path) -> bool:
    """
    Check if a directory exists and is empty.

    Args:
        path: Path object representing the directory to check

    Returns:
        True if the directory exists and contains no files/subdirectories,
        False otherwise (including if path doesn't exist or isn't a directory)

    Example:
        if empty_dir(Path("/tmp/output")):
            print("Directory is empty, nothing to process")

    Python Notes:
        - path.is_dir() returns True if path exists and is a directory
        - path.iterdir() returns an iterator over directory contents
        - any() returns True if the iterator yields at least one item
        - not any(...) means "iterator is empty"
    """
    if not path.is_dir():
        return False
    # any(path.iterdir()) is True if directory has any contents
    # We negate it to return True when directory IS empty
    return not any(path.iterdir())


def leaf_dir(path: Path) -> bool:
    """
    Check if a directory is a "leaf" (has no subdirectories, ignoring hidden ones).

    In our project structure, leaf directories are the actual terragrunt modules
    that contain terragrunt.hcl files. Non-leaf directories are organizational
    (like providers/dev/ which contains region directories).

    Args:
        path: Path object representing the directory to check

    Returns:
        True if directory has no visible subdirectories (may have files),
        False if it has subdirectories or isn't a valid directory

    Example:
        providers/
        ├── dev/              # NOT a leaf (has subdirs)
        │   └── us-east-1/    # NOT a leaf (has subdirs)
        │       └── service/  # IS a leaf (only has files like terragrunt.hcl)
        │           └── terragrunt.hcl
    """
    if not path.is_dir():
        return False

    # Check each item in the directory
    for item in path.iterdir():
        # Skip hidden files/directories (names starting with .)
        # This ignores .terraform, .terragrunt-cache, etc.
        if not item.name.startswith(".") and item.is_dir():
            return False  # Found a non-hidden subdirectory, so not a leaf

    return True  # No subdirectories found, this is a leaf


def in_path(exe: str) -> bool:
    """
    Check if an executable program is available in the system PATH.

    Similar to `which` command in bash or `exec.LookPath` in Go.

    Args:
        exe: Name of the executable to find (e.g., "terragrunt", "terraform")

    Returns:
        True if the executable is found and is executable, False otherwise

    Example:
        if not in_path("terragrunt"):
            error("terragrunt not found in PATH")
            sys.exit(1)

    Python Notes:
        - os.environ.get("PATH", "") gets PATH or empty string if not set
        - os.pathsep is the path separator (: on Unix, ; on Windows)
        - os.access(path, os.X_OK) checks if file is executable
    """
    # Get the PATH environment variable and split into list of directories
    # os.pathsep is ':' on Unix/macOS or ';' on Windows
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    # Check each directory in PATH
    for dir_path in path_dirs:
        exe_path = Path(dir_path) / exe  # / operator joins paths (like os.path.join)
        # Check if file exists and is executable
        if exe_path.is_file() and os.access(exe_path, os.X_OK):
            return True

    return False


def parse_json_safe(text: str, default: Any = None) -> Any:
    """
    Safely parse a JSON string, returning a default value on error.

    Unlike json.loads() which raises an exception on invalid JSON,
    this function catches the exception and returns a default value.

    Args:
        text: JSON string to parse
        default: Value to return if parsing fails (default: None)

    Returns:
        Parsed JSON data (dict, list, etc.) on success,
        or the default value on failure

    Example:
        data = parse_json_safe('{"key": "value"}')  # Returns {'key': 'value'}
        data = parse_json_safe('invalid json')       # Returns None
        data = parse_json_safe('invalid', {})        # Returns {}

    Python Notes:
        - try/except is Python's exception handling (like try/catch in other languages)
        - json.JSONDecodeError is raised when JSON parsing fails
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def strip_ansi(text: str) -> str:
    """
    Remove ANSI escape codes (color codes) from text.

    Terminal programs use ANSI escape codes to display colored text.
    When saving output to files or processing it programmatically,
    we often want to remove these codes.

    Args:
        text: String potentially containing ANSI escape codes

    Returns:
        String with all ANSI escape codes removed

    Example:
        colored = "\\x1b[31mRed Text\\x1b[0m"  # Red colored text
        plain = strip_ansi(colored)            # Returns "Red Text"

    Regex Explanation:
        \\x1b  - Escape character (ASCII 27)
        \\[    - Literal opening bracket
        \\d+   - One or more digits
        (;\\d+)* - Optionally more semicolon-digit pairs
        m      - Literal 'm' (terminates the escape sequence)
    """
    # Compile regex pattern for ANSI escape sequences
    ansi_pattern = re.compile(r"\x1b\[\d+(;\d+)*m")
    # Replace all matches with empty string
    return ansi_pattern.sub("", text)


def short_resource(resource: str) -> str:
    """
    Get a shortened version of a Terraform resource name for summary display.

    Terraform resource names can be very long with module prefixes:
        module.networking.module.vpc.aws_subnet.private[0]

    This function extracts just the meaningful part for display:
        aws_subnet

    Args:
        resource: Full terraform resource address

    Returns:
        Shortened resource identifier

    Example:
        short_resource("module.app.aws_instance.web[0]")
        # Returns: "aws_instance"

        short_resource("module.base.null_resource.setup")
        # Returns: "null_resource.setup" (keeps name for null_resource)
    """
    # Remove array indices like [0], ["key"], etc.
    # re.sub(pattern, replacement, string) replaces all matches
    # .split(".") splits the string into a list on "."
    r = re.sub(r"\[.+?\]", "", resource).split(".")

    # Skip "module" prefixes - these are organizational, not meaningful
    # Module paths look like: module.name.module.name.resource_type.resource_name
    # We keep skipping pairs until we hit the actual resource
    while len(r) > 0 and r[0] == "module":
        # Skip the "module" keyword and the module name (2 elements)
        r = r[2:] if len(r) > 2 else []

    # If nothing left, return original
    if len(r) < 2:
        return resource

    # For certain resource types, keep both type and name for clarity
    # null_resource, template_file, external are generic - need the name
    if r[-2] in ["null_resource", "template_file", "external"]:
        return f"{r[-2]}.{r[-1]}"

    # For regular resources, just return the resource type
    # e.g., "aws_instance" from "aws_instance.web"
    return r[-2] if r[-2] else resource


# =============================================================================
# STATUS "ENUMS"
# =============================================================================
# Python doesn't have a built-in enum type in older versions (3.4 added enum module).
# For simplicity and compatibility, we use classes with class attributes as constants.
# This is similar to using const blocks in Go:
#
#   const (
#       StatusSuccess = "success"
#       StatusError   = "error"
#   )

class PlanStatus:
    """
    Status values for terraform plans.

    Used to track the outcome of each plan operation:
    - SUCCESS: Plan completed, infrastructure changes detected
    - ERROR: Plan failed with an error
    - NOCHANGE: Plan completed, no changes needed
    - DISCARDED: Plan was removed (e.g., pruned empty plan)

    Example:
        plan.status = PlanStatus.SUCCESS
        if plan.status == PlanStatus.ERROR:
            handle_error()
    """
    SUCCESS = "success"      # Plan ran successfully with changes
    ERROR = "error"          # Plan failed
    NOCHANGE = "nochange"    # Plan ran but no changes detected
    DISCARDED = "discarded"  # Plan was discarded/pruned


class ReplanType:
    """
    Options for which plans to rerun when using --replan flag.

    When resuming a previous planning session, you can choose to:
    - ALL: Rerun all plans from scratch
    - CHANGES: Only rerun plans that had changes (skip no-change plans)
    - ERRORS: Only rerun plans that failed (retry errors)
    - NONE: Don't rerun any plans, just regenerate the summary report

    Example:
        if args.replan == ReplanType.ERRORS:
            plans = [p for p in plans if p.status == PlanStatus.ERROR]

    Class Attributes:
        VALID_TYPES: List of all valid values (used for argument validation)
    """
    ALL = "all"           # Rerun everything
    CHANGES = "changes"   # Rerun only plans that had changes
    ERRORS = "errors"     # Rerun only failed plans
    NONE = "none"         # Don't rerun, just regenerate summary

    # List of valid values for argument validation
    # Used by argparse to validate --replan argument
    VALID_TYPES = [ALL, CHANGES, ERRORS, NONE]
