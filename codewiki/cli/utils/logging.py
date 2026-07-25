"""
Logging utilities for CLI with colored output and progress tracking.
"""

from datetime import datetime
from typing import Optional
import click


class CLILogger:
    """Logger for CLI with support for colored output and progress tracking."""

    def __init__(self, verbose: bool = False, quiet: bool = False):
        """
        Initialize the logger.

        Args:
            verbose: Enable verbose output
            quiet: Suppress non-essential output (info/step/success); warnings and
                errors still show. verbose takes precedence over quiet.
        """
        self.verbose = verbose
        self.quiet = quiet and not verbose
        self.start_time = datetime.now()

    @staticmethod
    def _safe(message: str) -> str:
        """Replace surrogate code points (U+D800–U+DFFF) so stdout doesn't crash."""
        return message.encode('utf-8', errors='replace').decode('utf-8')

    def debug(self, message: str):
        """Log debug message (only in verbose mode)."""
        if self.verbose:
            timestamp = datetime.now().strftime("%H:%M:%S")
            click.secho(self._safe(f"[{timestamp}] {message}"), fg="cyan", dim=True)

    def info(self, message: str):
        """Log info message."""
        if not self.quiet:
            click.echo(self._safe(message))

    def success(self, message: str):
        """Log success message in green."""
        if not self.quiet:
            click.secho(self._safe(f"✓ {message}"), fg="green")

    def warning(self, message: str):
        """Log warning message in yellow."""
        click.secho(self._safe(f"⚠️  {message}"), fg="yellow")

    def error(self, message: str):
        """Log error message in red."""
        click.secho(self._safe(f"✗ {message}"), fg="red", err=True)

    def step(self, message: str, step: Optional[int] = None, total: Optional[int] = None):
        """
        Log a processing step.

        Args:
            message: Step description
            step: Current step number
            total: Total number of steps
        """
        if step is not None and total is not None:
            prefix = f"[{step}/{total}]"
        else:
            prefix = "→"

        if not self.quiet:
            click.secho(self._safe(f"{prefix} {message}"), fg="blue", bold=True)
    
    def elapsed_time(self) -> str:
        """Get elapsed time since logger was created."""
        elapsed = datetime.now() - self.start_time
        minutes = int(elapsed.total_seconds() // 60)
        seconds = int(elapsed.total_seconds() % 60)
        
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


def create_logger(verbose: bool = False, quiet: bool = False) -> CLILogger:
    """
    Create and return a CLI logger.

    Args:
        verbose: Enable verbose output
        quiet: Suppress non-essential output (verbose wins)

    Returns:
        Configured CLILogger instance
    """
    return CLILogger(verbose=verbose, quiet=quiet)

