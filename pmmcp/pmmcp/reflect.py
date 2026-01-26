"""Core reflection engine for auto-generating MCP tools from SDK classes."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, get_type_hints

from .serialize import serialize

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _strip_self_from_signature(sig: inspect.Signature) -> inspect.Signature:
    """Remove 'self' parameter and return annotation from a method signature.

    Return annotation is removed because SDK methods return dataclasses that
    pydantic cannot serialize. We convert to dicts via serialize() instead.
    """
    params = [p for name, p in sig.parameters.items() if name != "self"]
    return sig.replace(parameters=params, return_annotation=inspect.Signature.empty)


def _get_return_type_hint(method: Callable) -> type | None:
    """Get the return type hint from a method."""
    try:
        hints = get_type_hints(method)
        return hints.get("return")
    except Exception:
        return None


def register_class_methods(
    mcp: "FastMCP",
    cls: type,
    instance_factory: Callable[[], Any],
    prefix: str = "",
    exclude: set[str] | None = None,
    include: set[str] | None = None,
) -> list[str]:
    """Auto-register all public methods of a class as MCP tools.

    Uses Python introspection to extract method signatures, type hints, and
    docstrings, then dynamically creates MCP tool wrappers.

    Args:
        mcp: FastMCP server instance
        cls: Class to introspect (e.g., Clob, AuthenticatedClob)
        instance_factory: Callable that returns class instance (lazy init)
        prefix: Tool name prefix (e.g., "gamma_" -> "gamma_events")
        exclude: Method names to skip (always skips dunder methods)
        include: If provided, only register these methods

    Returns:
        List of registered tool names
    """
    exclude = exclude or set()
    registered = []

    # Get all methods defined on the class (not inherited dunder methods)
    for name in dir(cls):
        # Skip private/dunder methods
        if name.startswith("_"):
            continue

        # Skip excluded methods
        if name in exclude:
            continue

        # If include list provided, skip methods not in it
        if include is not None and name not in include:
            continue

        # Get the attribute
        attr = getattr(cls, name)

        # Skip non-callable attributes
        if not callable(attr):
            continue

        # Get the actual function (handles staticmethod, classmethod)
        if isinstance(attr, staticmethod):
            method = attr.__func__
        elif isinstance(attr, classmethod):
            continue  # Skip classmethods
        else:
            method = attr

        # Skip if not a function
        if not inspect.isfunction(method):
            continue

        # Extract metadata
        sig = inspect.signature(method)
        doc = inspect.getdoc(method) or f"Call {cls.__name__}.{name}"

        # Build tool name
        tool_name = f"{prefix}{name}" if prefix else name

        # Create wrapper function that calls the instance method
        def make_wrapper(method_name: str, factory: Callable):
            """Create a wrapper to avoid closure issues."""

            def wrapper(**kwargs):
                instance = factory()
                method_func = getattr(instance, method_name)
                result = method_func(**kwargs)
                return serialize(result)

            return wrapper

        wrapper = make_wrapper(name, instance_factory)

        # Set wrapper metadata for FastMCP schema generation
        wrapper.__name__ = tool_name
        wrapper.__doc__ = doc
        wrapper.__signature__ = _strip_self_from_signature(sig)

        # Copy parameter type hints only (exclude 'self' and 'return')
        # We convert return values to dicts via serialize(), so return hints aren't needed
        try:
            hints = get_type_hints(method)
            wrapper.__annotations__ = {
                k: v for k, v in hints.items() if k not in ("self", "return")
            }
        except Exception:
            wrapper.__annotations__ = {}

        # Register with FastMCP
        mcp.tool(name=tool_name, description=doc)(wrapper)
        registered.append(tool_name)

    return registered


def register_function(
    mcp: "FastMCP",
    func: Callable,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """Register a standalone function as an MCP tool.

    Args:
        mcp: FastMCP server instance
        func: Function to register
        name: Tool name (defaults to function name)
        description: Tool description (defaults to docstring)

    Returns:
        Registered tool name
    """
    tool_name = name or func.__name__
    doc = description or inspect.getdoc(func) or f"Call {func.__name__}"

    def wrapper(**kwargs):
        result = func(**kwargs)
        return serialize(result)

    # Copy metadata
    wrapper.__name__ = tool_name
    wrapper.__doc__ = doc
    wrapper.__signature__ = inspect.signature(func)

    try:
        wrapper.__annotations__ = {
            k: v for k, v in get_type_hints(func).items() if k != "return"
        }
    except Exception:
        wrapper.__annotations__ = {}

    mcp.tool(name=tool_name, description=doc)(wrapper)
    return tool_name
