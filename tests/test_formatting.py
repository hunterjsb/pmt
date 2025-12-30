"""Basic unit tests for formatting utilities."""

from io import StringIO

from rich.console import Console

from formatting import console, header, info, section


def test_console_exists():
    """Test that console object is created."""
    assert console is not None
    assert isinstance(console, Console)


def test_header_function():
    """Test header function runs without error."""
    # Create a test console to capture output
    _ = Console(file=StringIO(), force_terminal=True)

    # Should not raise any exceptions
    try:
        header("Test Header")
    except Exception as e:
        assert False, f"header() raised an exception: {e}"


def test_section_function():
    """Test section function runs without error."""
    try:
        section("Test Section")
    except Exception as e:
        assert False, f"section() raised an exception: {e}"


def test_info_function():
    """Test info function runs without error."""
    try:
        info("Test Label", "Test Value")
    except Exception as e:
        assert False, f"info() raised an exception: {e}"


def test_usage_panel_function():
    """Test usage_panel function runs without error."""
    from formatting import usage_panel

    try:
        usage_panel()
    except Exception as e:
        assert False, f"usage_panel() raised an exception: {e}"
