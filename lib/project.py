"""
Project detection and git integration for Terragrunt Tools.

This module handles:
- Detecting Terraform/Terragrunt project structure from git
- Finding the project root directory
- Parsing JIRA ticket numbers from arguments or branch names
- Resolving relative paths within the project
- Finding all terragrunt provider directories

PYTHON CONCEPTS FOR GO/BASH DEVELOPERS:
---------------------------------------
1. Classes - Python's way to organize code (like structs with methods in Go)
2. @property decorator - Makes a method act like a read-only attribute
3. Generator functions - Use 'yield' to return values one at a time (lazy evaluation)
4. subprocess module - Runs external commands (like os/exec in Go or $() in Bash)
5. Optional[X] - Type hint meaning "X or None" (like nullable types)
6. Tuple[X, Y] - Type hint for a tuple (fixed-size ordered collection)
"""

# =============================================================================
# IMPORTS
# =============================================================================

import os          # Operating system interface
import re          # Regular expressions
import subprocess  # Running external commands (git, terraform)
from pathlib import Path  # Cross-platform path handling
from typing import Optional, Tuple, List, Generator

# Import from our common module (the . means "from current package")
# In Python, a folder with __init__.py is a "package"
from .common import TFHCL, debug, warn, error


# =============================================================================
# TERRAFORMPROJECT CLASS
# =============================================================================

class TerraformProject:
    """
    Represents a Terraform/Terragrunt project.

    This class detects the project structure based on git repository information
    and provides utilities for working with the project's file structure.

    PYTHON CLASS CONCEPTS:
    ----------------------
    - __init__: Constructor method, called when creating new instance
    - self: Reference to the instance (like 'this' in other languages)
    - Instance attributes: Variables unique to each instance (self.name, self.root)
    - Class attributes: Variables shared by all instances (JIRA_PREFIXES)
    - @property: Makes a method act like a read-only attribute
    - __str__: String representation (called by str() and print())

    Example Usage:
        project = TerraformProject()  # Detect project from current directory
        print(project.name)            # Project name from git remote
        print(project.root)            # Project root path
        print(project.current_git_branch())  # Current branch name

    Project Structure Expected:
        project-root/
        ├── .git/                 # Git repository
        ├── modules/              # Terraform modules
        │   └── my-service/
        │       └── *.tf
        ├── providers/            # Terragrunt configurations
        │   ├── dev/
        │   │   └── us-east-1/
        │   │       └── my-service/
        │   │           └── terragrunt.hcl
        │   └── prod/
        │       └── us-east-1/
        │           └── my-service/
        │               └── terragrunt.hcl
        └── tickets/              # Plan outputs (created by scripts)
    """

    # -------------------------------------------------------------------------
    # CLASS ATTRIBUTES (shared by all instances)
    # -------------------------------------------------------------------------

    # JIRA ticket prefixes to recognize in branch names and arguments
    # Example: "INFRA-1234"
    # This is a class attribute - same for all TerraformProject instances
    JIRA_PREFIXES = ["INFRA"]

    # -------------------------------------------------------------------------
    # CONSTRUCTOR
    # -------------------------------------------------------------------------

    def __init__(self, directory: Optional[str] = None):
        """
        Initialize the project from the given directory.

        The constructor:
        1. Finds the git repository root
        2. Gets the git remote URL
        3. Detects terraform version
        4. Configures project-specific settings

        Args:
            directory: Starting directory to search from.
                      Defaults to current working directory if not specified.

        Raises:
            RuntimeError: If not in a git repository or unsupported project

        Example:
            # Detect from current directory
            project = TerraformProject()

            # Detect from a specific directory
            project = TerraformProject("/path/to/terraform/project")

        Python Notes:
            - Optional[str] = None means the parameter is optional with default None
            - Path.cwd() returns current working directory as a Path object
            - Instance attributes are set with self.attribute_name = value
        """
        # Convert directory to Path object, default to current working directory
        # The "if directory else" pattern is a ternary expression:
        # value_if_true if condition else value_if_false
        self.directory = Path(directory) if directory else Path.cwd()

        # Get the git repository root directory
        # This is where the .git folder is located
        self.root = self._get_git_root()
        if not self.root:
            raise RuntimeError("Could not determine git repository root")

        # Get the git remote URL (used to identify the project)
        # If no remote is configured (local-only repo), we'll use the directory name instead
        self.remote = self._get_git_remote()

        # Get terraform executable path
        # TERRAGRUNT_TFPATH environment variable overrides the default "terraform"
        # os.environ.get() returns the env var value or default if not set
        self.tf_exe = os.environ.get("TERRAGRUNT_TFPATH", "terraform")
        self.tf_version = self._get_tf_version()

        # Project configuration (set by _configure_project)
        self.name: str = ""         # Project name extracted from git remote
        self.dir_prefix: str = ""   # Prefix for paths if terraform is in a subdirectory

        # Configure project settings based on git remote
        self._configure_project()

        # Build JIRA ticket regex pattern
        # "|".join() creates a regex OR pattern: "INFRA"
        self.jira = "|".join(self.JIRA_PREFIXES)

    # -------------------------------------------------------------------------
    # PRIVATE METHODS (start with underscore by convention)
    # -------------------------------------------------------------------------
    # In Python, there's no true "private" - underscore prefix is a convention
    # meaning "internal use only, don't call from outside the class"

    def _get_git_root(self) -> Optional[Path]:
        """
        Get the git repository root directory.

        Runs: git rev-parse --show-toplevel

        Returns:
            Path to git root, or None if not in a git repository

        Python subprocess Notes:
            - subprocess.run() executes an external command
            - capture_output=True captures stdout and stderr
            - text=True returns strings instead of bytes
            - check=True raises CalledProcessError if command fails
            - result.stdout contains the command output
            - .strip() removes leading/trailing whitespace
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],  # Command as list of arguments
                cwd=self.directory,          # Working directory for the command
                capture_output=True,         # Capture stdout and stderr
                text=True,                   # Return strings, not bytes
                check=True,                  # Raise exception on non-zero exit code
            )
            # stdout contains the path, strip whitespace and convert to Path
            return Path(result.stdout.strip())
        except subprocess.CalledProcessError:
            # Command failed (not in a git repo)
            return None

    def _get_git_remote(self) -> Optional[str]:
        """
        Get the git remote URL.

        Runs: git remote -v

        Returns:
            First remote URL found, or None if no remotes configured

        Example git remote -v output:
            origin  git@github.com:org/project.git (fetch)
            origin  git@github.com:org/project.git (push)

        We parse the second column (the URL) from the first line.
        """
        try:
            result = subprocess.run(
                ["git", "remote", "-v"],
                cwd=self.directory,
                capture_output=True,
                text=True,
                check=True,
            )
            # Split output into lines
            lines = result.stdout.strip().split("\n")

            # Parse first line if it exists
            if lines and lines[0]:
                # Split by whitespace: ["origin", "url", "(fetch)"]
                parts = lines[0].split()
                # Return the URL (second part)
                return parts[1] if len(parts) > 1 else None
            return None
        except subprocess.CalledProcessError:
            return None

    def _get_tf_version(self) -> str:
        """
        Get the terraform version.

        Runs: terraform -version

        Returns:
            Version string (e.g., "1.5.0") or "unknown" if not found

        Example terraform -version output:
            Terraform v1.5.0
            on darwin_amd64
        """
        try:
            result = subprocess.run(
                [self.tf_exe, "-version"],
                capture_output=True,
                text=True,
                check=True,
            )
            # Parse version from first line: "Terraform v1.5.0"
            first_line = result.stdout.split("\n")[0]
            parts = first_line.split()
            if len(parts) >= 2:
                # Remove leading 'v' from version
                return parts[1].lstrip("v")
            return "unknown"
        except (subprocess.CalledProcessError, FileNotFoundError):
            # terraform not found or failed
            return "unknown"

    def _configure_project(self) -> None:
        """
        Configure project settings based on git remote URL.

        This method:
        1. Extracts the project name from the git remote URL
        2. Adjusts the root path if terraform is in a subdirectory

        The git remote URL can be in different formats:
        - SSH:   git@github.com:org/project.git
        - HTTPS: https://github.com/org/project.git

        We extract "project" from either format.

        Python Notes:
            - -> None means the function doesn't return anything (like void)
            - re.search() finds a pattern anywhere in the string
            - The regex r"[:/]([^/]+?)(?:\\.git)?$" captures the project name
            - for...else: the else block runs if loop completes without break
        """
        remote = self.remote

        if remote:
            # Extract project name from remote URL using regex
            # Pattern explanation:
            #   [:/]      - Either : (SSH format) or / (HTTPS format)
            #   ([^/]+?)  - Capture group: one or more non-slash characters (project name)
            #   (?:\.git)?  - Optional non-capturing group for .git suffix
            #   $         - End of string
            match = re.search(r"[:/]([^/]+?)(?:\.git)?$", remote)
            if match:
                self.name = match.group(1)  # .group(1) gets first capture group
            else:
                self.name = "unknown"
        else:
            # No remote configured (local-only repo)
            # Use the git root directory name as the project name
            # e.g., /Users/pankaj/devops/my-org -> "my-org"
            self.name = self.root.name
            debug(f"No git remote found, using directory name: {self.name}")

        # Check if terraform code is in a subdirectory
        # Some repositories have structure like:
        #   repo-root/
        #   └── terraform/    (or infra/, eks-infra/, etc.)
        #       └── providers/
        #           └── ...
        #
        # We need to adjust root to point to the terraform directory.
        #
        # First check well-known subdirectory names, then auto-detect
        # any immediate subdirectory that contains a "providers" folder.
        #
        # Auto-detection handles repos like:
        #   my-org/my-terraform/providers/...
        #   my-repo/infra/providers/...
        well_known = ["terraform", "terraform/eks-infra", "eks-infra"]

        # Also scan all immediate subdirectories for a providers/ folder
        # This handles repos where terraform lives in a named subdirectory
        auto_detected = []
        if self.root.is_dir():
            for child in sorted(self.root.iterdir()):
                if child.is_dir() and child.name not in (".git", "tickets", "modules", "providers"):
                    if (child / "providers").is_dir():
                        auto_detected.append(child.name)

        # Try well-known names first, then auto-detected ones
        for subdir in well_known + auto_detected:
            subpath = self.root / subdir
            # Check if this subdirectory has a "providers" folder
            if subpath.is_dir() and (subpath / "providers").is_dir():
                self.root = subpath
                self.dir_prefix = f"{subdir}/"
                break  # Stop searching once found
        else:
            # This else belongs to the for loop
            # It runs only if the loop completes without hitting 'break'
            # This means terraform is at the root level (no subdirectory)
            self.dir_prefix = ""

    # -------------------------------------------------------------------------
    # SPECIAL METHODS (dunder methods - double underscore)
    # -------------------------------------------------------------------------

    def __str__(self) -> str:
        """
        Return string representation of the project.

        This is called when you do str(project) or print(project).

        Returns:
            The project name

        Example:
            project = TerraformProject()
            print(project)  # Prints the project name
        """
        return self.name

    # -------------------------------------------------------------------------
    # PROPERTIES
    # -------------------------------------------------------------------------

    @property
    def ticket_regex(self) -> re.Pattern:
        """
        Return regex pattern to recognize ticket names.

        The @property decorator makes this method act like a read-only attribute.
        Instead of calling project.ticket_regex(), you use project.ticket_regex

        Returns:
            Compiled regex pattern that matches:
            - "test" (for testing)
            - JIRA tickets like "INFRA-1234"

        Example:
            if project.ticket_regex.match("INFRA-1234"):
                print("Valid ticket!")

        Regex Explanation:
            ^test        - Matches "test" at start of string
            |            - OR
            ^(?:INFRA)   - Matches INFRA at start (non-capturing group)
            -\\d+        - Followed by dash and one or more digits
        """
        # (?:...) is a non-capturing group
        # \d+ matches one or more digits
        return re.compile(f"^test|^(?:{self.jira})-\\d+")

    # -------------------------------------------------------------------------
    # PUBLIC METHODS
    # -------------------------------------------------------------------------

    def current_git_branch(self) -> str:
        """
        Get the current git branch name.

        Runs: git status --porcelain -b

        Returns:
            Current branch name, or empty string if not on a branch

        Example git status --porcelain -b output:
            ## main...origin/main
            ## feature-branch...origin/feature-branch [ahead 2]
            ## detached-head (no branch info)

        We parse the branch name from after "## " and before "..."
        """
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "-b"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=True,
            )
            # Get first line: "## branch...origin/branch"
            first_line = result.stdout.split("\n")[0]
            parts = first_line.split()

            if len(parts) >= 2:
                branch = parts[1]  # e.g., "main...origin/main"
                # Remove remote tracking info (everything after ...)
                return re.sub(r"\.\.\.origin.*", "", branch)
            return ""
        except subprocess.CalledProcessError:
            return ""

    def relative_path(self, path: str) -> str:
        """
        Convert a path to be relative to the terraform directory.

        Args:
            path: Absolute or relative path to convert

        Returns:
            Path relative to project root, or absolute path if outside project

        Example:
            project.root = Path("/home/user/terraform")

            project.relative_path("/home/user/terraform/modules/app")
            # Returns: "modules/app"

            project.relative_path("modules/app")
            # Returns: "modules/app"

            project.relative_path("/other/path")
            # Returns: "/other/path" (unchanged, outside project)

        Python Notes:
            - Path.resolve() returns absolute path, resolving symlinks
            - Path.relative_to() returns path relative to another path
            - ValueError is raised if path is not under the base path
        """
        # Convert to absolute path
        abs_path = Path(path).resolve()
        try:
            # Try to make it relative to project root
            return str(abs_path.relative_to(self.root))
        except ValueError:
            # Path is not relative to root, return as-is
            return str(abs_path)

    def parse_argument(self, arg: str) -> Tuple[Optional[str], List[str]]:
        """
        Determine if an argument refers to a ticket, provider, module, or variables file.

        This method takes a command-line argument and figures out what it represents:
        - A JIRA ticket number (e.g., "INFRA-1234")
        - A module directory (e.g., "modules/my-service")
        - A provider directory (e.g., "providers/dev/us-east-1/my-service")
        - A variables file (e.g., "providers/.../vars.tfvars")

        Args:
            arg: Input string (ticket number, path, etc.)

        Returns:
            Tuple of (type, list of values) where type is one of:
            - "ticket": [ticket number]
            - "module": [module directory]
            - "provider": [one or more provider directories]
            - "variables": [variables file path]
            - None: [original input] if could not be matched

        Examples:
            project.parse_argument("INFRA-1234")
            # Returns: ("ticket", ["INFRA-1234"])

            project.parse_argument("modules/my-service")
            # Returns: ("module", ["modules/my-service"])

            project.parse_argument("providers/dev")
            # Returns: ("provider", ["providers/dev/us-east-1/svc1", "providers/dev/us-east-1/svc2", ...])

        Python Tuple Notes:
            - Tuples are immutable ordered collections
            - Created with parentheses: (value1, value2)
            - Accessed by index: result[0], result[1]
            - Can be unpacked: arg_type, paths = parse_argument(arg)
        """
        # Check if it matches a ticket pattern (e.g., "INFRA-1234")
        if self.ticket_regex.match(arg):
            # Split on "/" in case of "INFRA-1234/something" and take first part
            return ("ticket", [arg.split("/")[0]])

        # Check if it's a file or directory that exists
        arg_path = Path(arg)
        if not arg_path.exists():
            # Also try relative to project root
            arg_path = self.root / arg
            if not arg_path.exists():
                debug(f"parse_argument: '{arg}' not found (tried cwd and root {self.root})")
                return (None, [arg])  # Not found, return as-is

        # Convert to relative path
        rel = self.relative_path(str(arg_path))
        debug(f"parse_argument: '{arg}' -> path={arg_path}, rel='{rel}', root={self.root}")

        # Check for a variables file (.tfvars)
        if arg_path.is_file() and str(arg).endswith(".tfvars"):
            rel_parts = rel.split("/")
            if rel_parts[0] == "providers":
                return ("variables", [rel])

        # If it's a file, use its parent directory
        if arg_path.is_file():
            arg_path = arg_path.parent
            rel = self.relative_path(str(arg_path))

        # Parse the path to determine type
        # Split into top-level directory and rest of path
        parts = rel.split("/", 1)  # Split only on first "/"
        if len(parts) < 2:
            return (None, [arg])  # Not enough path components

        topdir, subdir = parts[0], parts[1]

        # Handle different top-level directories
        if topdir == "tickets":
            # Extract ticket name from path like "tickets/INFRA-1234/..."
            ticket_name = subdir.split("/")[0]
            if self.ticket_regex.match(ticket_name):
                return ("ticket", [ticket_name])

        elif topdir == "modules":
            # Check if directory contains .tf files (is a valid terraform module)
            # list() converts the generator to a list to check if it's non-empty
            if list(arg_path.glob("*.tf")):
                return ("module", [rel])

        elif topdir == "providers":
            # Find all provider directories under this path
            providers = list(self.each_provider(rel))
            debug(f"parse_argument: topdir='providers', each_provider('{rel}') -> {providers}")
            if providers:
                return ("provider", providers)

        # Could not determine type
        return (None, [arg])

    def each_provider(self, directory: str = "providers") -> Generator[str, None, None]:
        """
        Find all terragrunt providers under the given path.

        A "provider" is a directory containing a terragrunt.hcl file.
        This method searches recursively under the specified directory.

        Args:
            directory: Relative path to search (defaults to "providers")

        Yields:
            Provider paths relative to project root

        Example:
            for provider in project.each_provider("providers/dev"):
                print(provider)
            # Output:
            # providers/dev/us-east-1/service-a
            # providers/dev/us-east-1/service-b
            # providers/dev/us-west-2/service-a

        PYTHON GENERATOR NOTES:
        ----------------------
        This function is a "generator" - it uses 'yield' instead of 'return'.
        Generators produce values lazily (one at a time) instead of building
        a full list in memory.

        - yield: Returns a value and pauses the function
        - Calling the function returns a generator object, not the results
        - Each iteration of a for loop calls the generator to get next value
        - More memory efficient than returning a full list

        Similar to Go channels for producing values:
            func eachProvider(dir string) <-chan string {
                ch := make(chan string)
                go func() {
                    // find providers and send to ch
                    close(ch)
                }()
                return ch
            }

        In Bash, this would be like a function that outputs lines:
            each_provider() {
                find "$1" -name "terragrunt.hcl" -exec dirname {} \\;
            }
        """
        # Build full search path
        search_path = self.root / directory

        # Check if path exists
        if not search_path.exists():
            return  # Empty generator (no values to yield)

        # Recursively find all terragrunt.hcl files
        # .rglob() is recursive glob - like "find . -name 'pattern'"
        for hcl_file in search_path.rglob(TFHCL):
            # Get the directory containing the terragrunt.hcl file
            provider = hcl_file.parent

            # Convert to relative path
            rel = self.relative_path(str(provider))

            # Skip terragrunt cache directories
            # .terragrunt-cache is an internal directory created by terragrunt
            # that mirrors the provider structure - we don't want to plan those
            if ".terragrunt-cache" in rel:
                continue

            # Skip top-level terragrunt.hcl files
            # Valid providers have at least 3 levels: providers/env/region/name
            # or providers/env/name
            if len(rel.split("/")) >= 3:
                yield rel  # Yield (return) this value and continue looking


# =============================================================================
# STANDALONE FUNCTIONS
# =============================================================================
# Functions that work with TerraformProject but aren't part of the class

def find_project_diffs(project: TerraformProject, branch: str) -> List[str]:
    """
    Find list of changes between current branch and given branch.

    This is used by the --from-branch option to automatically plan
    only the modules/providers that have changed.

    Args:
        project: The TerraformProject instance
        branch: Branch to compare against (e.g., "main", "origin/main")

    Returns:
        List of affected module/provider/variable paths

    How it works:
        1. Runs git diff to find changed files between branches
        2. Parses the diff output to extract file paths
        3. Converts file paths to module/provider/variable paths
        4. Removes duplicates and sorts the results

    Example:
        diffs = find_project_diffs(project, "main")
        # Returns: ["modules/my-service", "providers/dev/us-east-1/my-service"]

    Git Diff Output Format:
        diff --git a/modules/svc/main.tf b/modules/svc/main.tf
        --- a/modules/svc/main.tf
        +++ b/modules/svc/main.tf
        @@ -1,3 +1,4 @@
         ...changes...

        We look for lines starting with "---" or "+++" to find changed files.
    """
    # List to collect changed paths
    diffs: List[str] = []

    try:
        # Run git diff to compare current branch with specified branch
        # Only look at changes in modules and providers directories
        result = subprocess.run(
            ["git", "diff", branch, "--", "modules", "providers"],
            cwd=project.root,
            capture_output=True,
            text=True,
            check=True,
        )

        # Build regex pattern to match diff file lines
        # Format: "--- a/path/to/file" or "+++ b/path/to/file"
        #
        # re.escape() escapes special regex characters in dir_prefix
        # This is important if dir_prefix contains characters like . or /
        pattern = re.compile(
            rf"^(?:---|\+\+\+) [ab]/{re.escape(project.dir_prefix)}(.+)$"
        )

        # Parse each line of the diff output
        for line in result.stdout.split("\n"):
            match = pattern.match(line)
            if match:
                # Extract the file path from the match
                file_path = match.group(1)
                full_path = project.root / file_path

                # Only process if file still exists (wasn't deleted)
                if full_path.is_file():
                    # Use parse_argument to categorize the path
                    arg_type, paths = project.parse_argument(file_path)
                    if arg_type in ["module", "provider", "variables"]:
                        # extend() adds all items from paths to diffs
                        diffs.extend(paths)

        # Remove duplicates and sort
        # set() removes duplicates (sets only have unique values)
        # sorted() returns a new sorted list
        diffs = sorted(set(diffs))

    except subprocess.CalledProcessError as e:
        error(f"Failed to get git diff: {e}")

    return diffs
