"""
Colored logging configuration for CodeWiki.

This module provides a custom logging formatter with colored output for better readability.

Color Scheme:
    - DEBUG: Cyan (dim) - Development and debugging information
    - INFO: Green - Normal operational messages
    - WARNING: Yellow - Warning messages that need attention
    - ERROR: Red - Error messages
    - CRITICAL: Bright Red - Critical issues requiring immediate attention
    
    Additional Colors:
    - Timestamp: Blue
    - Module Name: Magenta
    
Usage:
    from codewiki.src.be.dependency_analyzer.utils.logging_config import setup_logging
    
    # Setup colored logging for the entire application
    setup_logging(level=logging.INFO)
    
    # Or setup for a specific module
    logger = setup_module_logging('my_module', level=logging.DEBUG)
"""

import logging
import os
import sys
from colorama import Fore, Style, init

# Initialize colorama for cross-platform colored terminal output
init(autoreset=True)


LOG_DIR = "/root/share/logs/codewiki"
LOG_FILE_NAME = "codewiki.log"
FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(filename)s:%(funcName)s:%(lineno)d - %(message)s"

# Local fallback log location used when the privileged LOG_DIR is not writable.
# Honored in this order: CODEWIKI_LOG_FILE env var > ./codewiki.log (cwd).
LOCAL_LOG_FILE_NAME = "codewiki.log"


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colored output for better readability.
    
    This formatter adds colors to different log levels and components:
    - Log levels are colored based on severity
    - Timestamps are shown in blue
    - Module names are shown in magenta
    - Messages are shown in the default terminal color
    """
    
    # Define colors for different log levels
    COLORS = {
        'DEBUG': Fore.BLUE,
        'INFO': Fore.CYAN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT,
    }
    
    # Define colors for different components
    COMPONENT_COLORS = {
        'timestamp': Fore.BLUE,
        'module': Fore.MAGENTA,
        'reset': Style.RESET_ALL,
    }
    
    def format(self, record):
        """Format log record with colors."""
        # Get the color for this log level
        level_color = self.COLORS.get(record.levelname, '')
        
        # Format timestamp
        timestamp = self.formatTime(record, '%H:%M:%S')
        colored_timestamp = f"{self.COMPONENT_COLORS['timestamp']}[{timestamp}]{self.COMPONENT_COLORS['reset']}"
        
        # Format log level with color
        colored_level = f"{level_color}{record.levelname:8}{self.COMPONENT_COLORS['reset']}"

        location = f"{self.COMPONENT_COLORS['module']}{record.filename}:{record.funcName}:{record.lineno}{self.COMPONENT_COLORS['reset']}"

        # Format the message with the same color as the log level
        message = record.getMessage()
        colored_message = f"{level_color}{message}{self.COMPONENT_COLORS['reset']}"

        log_line = f"{colored_timestamp} {colored_level} {location} {colored_message}"
        
        # Handle exceptions
        if record.exc_info:
            log_line += "\n" + self.formatException(record.exc_info)
        
        return log_line


def setup_logging(level=logging.INFO):
    """
    Set up logging configuration with colored output.
    
    Args:
        level: Logging level (default: logging.INFO)
    """
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # Set colored formatter
    colored_formatter = ColoredFormatter()
    console_handler.setFormatter(colored_formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Add our console handler
    root_logger.addHandler(console_handler)

    file_handler = _build_file_handler()
    if file_handler is not None:
        root_logger.addHandler(file_handler)


def _build_file_handler():
    """Create a file handler for persistent logging.

    Tries the privileged LOG_DIR first; on any failure (no root perms,
    read-only fs, etc.) falls back to a local codewiki.log so logs are never
    silently lost. Returns None only if even the local file cannot be created.
    """
    def _make(path: str):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
        return handler

    # 1. Privileged central log dir.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        return _make(os.path.join(LOG_DIR, LOG_FILE_NAME))
    except (OSError, PermissionError):
        pass

    # 2. Env override / local fallback.
    local_path = os.environ.get("CODEWIKI_LOG_FILE") or os.path.join(
        os.getcwd(), LOCAL_LOG_FILE_NAME
    )
    try:
        local_dir = os.path.dirname(os.path.abspath(local_path))
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)
        return _make(local_path)
    except (OSError, PermissionError) as e:
        sys.stderr.write(
            f"[logging_config] Could not create file handler at {local_path}: {e}\n"
        )
        return None


def setup_cli_logging(level=logging.INFO):
    """Configure the ``codewiki`` package logger so its own logs print by default.

    This attaches a colored console handler to the ``codewiki`` logger only —
    the root logger is left untouched, so third-party libraries (httpx, openai,
    pydantic_ai, ...) stay quiet.  Covers ``codewiki.cli.*``; the backend
    ``codewiki.src.be`` logger keeps its own (richer) configuration but, when
    that hasn't been set up yet, propagates up to this handler as a fallback.

    Idempotent: re-calling (e.g. after --verbose/--quiet) replaces the prior
    CLI handler without duplicating output.
    """
    pkg_logger = logging.getLogger("codewiki")
    pkg_logger.setLevel(level)
    # Drop only our own previously-attached CLI handler.
    pkg_logger.handlers = [
        h for h in pkg_logger.handlers if not getattr(h, "_codewiki_cli", False)
    ]
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(ColoredFormatter())
    handler._codewiki_cli = True
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False
    return pkg_logger


def setup_module_logging(module_name: str, level=logging.INFO):
    """
    Set up logging for a specific module with colored output.
    
    Args:
        module_name: Name of the module to configure logging for
        level: Logging level (default: logging.INFO)
    """
    logger = logging.getLogger(module_name)
    logger.setLevel(level)
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    # Set colored formatter
    colored_formatter = ColoredFormatter()
    console_handler.setFormatter(colored_formatter)
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Add console handler
    logger.addHandler(console_handler)
    
    # Prevent propagation to avoid duplicate logs
    logger.propagate = False
    
    return logger


