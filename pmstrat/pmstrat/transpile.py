"""Python to Rust transpiler for pmstrat strategies."""

import ast
import inspect
import re
import textwrap
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Any

from .dsl import get_strategy_meta, StrategyMeta


def param_to_rust(name: str, value: Any) -> tuple[str, str]:
    """Convert a Python parameter value to Rust type and literal.

    This is a shared helper used by both RustCodeGen and RustTestGenerator.

    Args:
        name: Parameter name (unused, but kept for consistency)
        value: Python value to convert

    Returns:
        Tuple of (rust_type, rust_value)
    """
    if isinstance(value, Decimal):
        return ("Decimal", f"dec!({value})")
    elif isinstance(value, bool):
        return ("bool", "true" if value else "false")
    elif isinstance(value, int):
        return ("i64", str(value))
    elif isinstance(value, float):
        return ("f64", str(value))
    elif isinstance(value, str):
        return ("&str", f'"{value}"')
    elif isinstance(value, (list, tuple)):
        if not value:
            return ("&[&str]", "&[]")
        # Infer type from first element
        first = value[0]
        if isinstance(first, str):
            items = ", ".join(f'"{v}"' for v in value)
            return ("&[&str]", f"&[{items}]")
        elif isinstance(first, Decimal):
            items = ", ".join(f"dec!({v})" for v in value)
            return ("&[Decimal]", f"&[{items}]")
        elif isinstance(first, (int, float)):
            items = ", ".join(str(v) for v in value)
            elem_type = "i64" if isinstance(first, int) else "f64"
            return (f"&[{elem_type}]", f"&[{items}]")
        else:
            # Fallback: treat as strings
            items = ", ".join(f'"{v}"' for v in value)
            return ("&[&str]", f"&[{items}]")
    else:
        # Fallback: convert to string
        return ("&str", f'"{value}"')


class TranspileError(Exception):
    """Error raised when transpilation fails due to unsupported patterns."""

    def __init__(self, message: str, node: ast.AST | None = None, hint: str | None = None):
        self.node = node
        self.hint = hint
        self.lineno = getattr(node, 'lineno', None) if node else None

        full_message = message
        if self.lineno:
            full_message = f"Line {self.lineno}: {message}"
        if hint:
            full_message += f"\n  Hint: {hint}"

        super().__init__(full_message)


@dataclass
class ValidationError:
    """A single validation error."""
    message: str
    lineno: int | None
    hint: str | None = None

    def __str__(self) -> str:
        prefix = f"Line {self.lineno}: " if self.lineno else ""
        result = f"{prefix}{self.message}"
        if self.hint:
            result += f"\n    Hint: {self.hint}"
        return result


class StrategyValidator(ast.NodeVisitor):
    """Validates Python strategy code for transpiler compatibility.

    Catches unsupported patterns early with clear error messages.
    """

    # Patterns that are not supported
    UNSUPPORTED_BUILTINS = {
        'min', 'max', 'abs', 'sum', 'len', 'range', 'enumerate', 'zip',
        'map', 'filter', 'sorted', 'reversed', 'list', 'dict', 'set',
        'print', 'input', 'open', 'eval', 'exec',
    }

    def __init__(self, strategy_name: str):
        self.strategy_name = strategy_name
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []
        self._in_function = False
        self._function_name: str | None = None

    def validate(self, source: str) -> tuple[list[ValidationError], list[ValidationError]]:
        """Validate source code and return (errors, warnings)."""
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            self.errors.append(ValidationError(
                f"Syntax error: {e.msg}",
                e.lineno,
            ))
            return self.errors, self.warnings

        self.visit(tree)
        return self.errors, self.warnings

    def visit_Global(self, node: ast.Global):
        """global statements are not supported."""
        self.errors.append(ValidationError(
            f"'global' statement not supported: {', '.join(node.names)}",
            node.lineno,
            "Use strategy struct fields for state, not module-level variables. "
            "Example: Add state to the @strategy decorator's params dict.",
        ))
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal):
        """nonlocal statements are not supported."""
        self.errors.append(ValidationError(
            f"'nonlocal' statement not supported: {', '.join(node.names)}",
            node.lineno,
            "Avoid closures with mutable state. Use strategy params instead.",
        ))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Track function context and check for nested functions."""
        if self._in_function:
            self.errors.append(ValidationError(
                f"Nested function '{node.name}' not supported",
                node.lineno,
                "Move helper functions to module level or inline the logic.",
            ))
        else:
            old_in_function = self._in_function
            old_function_name = self._function_name
            self._in_function = True
            self._function_name = node.name
            self.generic_visit(node)
            self._in_function = old_in_function
            self._function_name = old_function_name

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """async functions are not supported."""
        self.errors.append(ValidationError(
            f"Async function '{node.name}' not supported",
            node.lineno,
            "Strategies must be synchronous. The engine handles async I/O.",
        ))

    def visit_ClassDef(self, node: ast.ClassDef):
        """Classes are not supported in strategy code."""
        self.errors.append(ValidationError(
            f"Class '{node.name}' not supported",
            node.lineno,
            "Use plain functions with the @strategy decorator.",
        ))

    def visit_Import(self, node: ast.Import):
        """Check for unsupported imports."""
        for alias in node.names:
            if alias.name not in ('decimal', 'pmstrat', 'datetime'):
                self.warnings.append(ValidationError(
                    f"Import '{alias.name}' may not be supported",
                    node.lineno,
                    "Only 'decimal', 'datetime', and 'pmstrat' imports are fully supported.",
                ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Check for unsupported imports."""
        allowed_modules = {
            'decimal', 'pmstrat', 'datetime',
            '..dsl', '..signal', '..context', '.dsl', '.signal', '.context',
        }
        if node.module and not any(node.module.startswith(m.lstrip('.')) or node.module == m
                                    for m in allowed_modules):
            self.warnings.append(ValidationError(
                f"Import from '{node.module}' may not be supported",
                node.lineno,
                "Only 'decimal', 'datetime', and 'pmstrat' imports are fully supported.",
            ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        """Check for unsupported function calls."""
        # Check for unsupported builtins
        if isinstance(node.func, ast.Name):
            if node.func.id in self.UNSUPPORTED_BUILTINS:
                self.errors.append(ValidationError(
                    f"Built-in '{node.func.id}()' not supported",
                    node.lineno,
                    self._get_builtin_hint(node.func.id),
                ))

        # Check for list comprehensions used as arguments (they're often problematic)
        for arg in node.args:
            if isinstance(arg, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                self.errors.append(ValidationError(
                    "Comprehensions as function arguments not supported",
                    node.lineno,
                    "Use explicit loops instead of comprehensions.",
                ))

        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp):
        """List comprehensions are not supported."""
        self.errors.append(ValidationError(
            "List comprehension not supported",
            node.lineno,
            "Use explicit for loops: `for x in items: result.append(...)`",
        ))

    def visit_SetComp(self, node: ast.SetComp):
        """Set comprehensions are not supported."""
        self.errors.append(ValidationError(
            "Set comprehension not supported",
            node.lineno,
            "Use explicit for loops instead.",
        ))

    def visit_DictComp(self, node: ast.DictComp):
        """Dict comprehensions are not supported."""
        self.errors.append(ValidationError(
            "Dict comprehension not supported",
            node.lineno,
            "Use explicit for loops instead.",
        ))

    def visit_GeneratorExp(self, node: ast.GeneratorExp):
        """Generator expressions are not supported."""
        self.errors.append(ValidationError(
            "Generator expression not supported",
            node.lineno,
            "Use explicit for loops instead.",
        ))

    def visit_Lambda(self, node: ast.Lambda):
        """Lambda functions are not supported."""
        self.errors.append(ValidationError(
            "Lambda function not supported",
            node.lineno,
            "Define a named function instead or inline the logic.",
        ))

    def visit_With(self, node: ast.With):
        """with statements are not supported."""
        self.errors.append(ValidationError(
            "'with' statement not supported",
            node.lineno,
            "Context managers don't translate to Rust. Use explicit setup/cleanup.",
        ))

    def visit_Try(self, node: ast.Try):
        """try/except is not supported."""
        self.errors.append(ValidationError(
            "try/except not supported",
            node.lineno,
            "Use explicit None checks: `if value is None: return [Hold()]`",
        ))

    def visit_Raise(self, node: ast.Raise):
        """raise is not supported."""
        self.errors.append(ValidationError(
            "'raise' not supported",
            node.lineno,
            "Return Hold() or Shutdown() signals instead of raising exceptions.",
        ))

    def visit_Assert(self, node: ast.Assert):
        """assert is not supported."""
        self.warnings.append(ValidationError(
            "'assert' statement will be ignored",
            node.lineno,
            "Assertions are skipped during transpilation.",
        ))

    def visit_Yield(self, node: ast.Yield):
        """yield is not supported."""
        self.errors.append(ValidationError(
            "'yield' not supported",
            node.lineno,
            "Return a list of signals instead of using generators.",
        ))

    def visit_YieldFrom(self, node: ast.YieldFrom):
        """yield from is not supported."""
        self.errors.append(ValidationError(
            "'yield from' not supported",
            node.lineno,
            "Return a list of signals instead of using generators.",
        ))

    def visit_Match(self, node: ast.Match):
        """match/case is not supported (Python 3.10+)."""
        self.errors.append(ValidationError(
            "'match' statement not supported",
            node.lineno,
            "Use if/elif/else chains instead.",
        ))

    def _get_builtin_hint(self, name: str) -> str:
        """Get a helpful hint for replacing a builtin."""
        hints = {
            'min': "Use explicit comparison: `if a < b: result = a else: result = b`",
            'max': "Use explicit comparison: `if a > b: result = a else: result = b`",
            'abs': "Use explicit comparison: `if x < 0: x = -x`",
            'sum': "Use a loop: `total = 0; for x in items: total = total + x`",
            'len': "Track length manually or use a different approach",
            'range': "Use while loops or iterate over collections directly",
            'print': "Remove debug prints before transpiling",
            'sorted': "Sorting is not supported; pre-sort data if needed",
        }
        return hints.get(name, "This builtin is not supported in the DSL")


def validate_strategy(func: Callable) -> tuple[list[ValidationError], list[ValidationError]]:
    """Validate a strategy function for transpiler compatibility.

    Args:
        func: The @strategy decorated function

    Returns:
        Tuple of (errors, warnings)
    """
    meta = get_strategy_meta(func)
    if meta is None:
        return [ValidationError("Function is not decorated with @strategy", None)], []

    source = inspect.getsource(func)
    # Dedent the source to handle nested functions
    source = textwrap.dedent(source)

    validator = StrategyValidator(meta.name)
    return validator.validate(source)


@dataclass
class TranspileResult:
    """Result of transpiling a strategy."""
    rust_code: str
    strategy_name: str
    struct_name: str
    tokens: List[str]


@dataclass
class MatchUnwrap:
    """Synthetic AST node representing an Option unwrap via match.

    Represents the pattern:
        let var_name = match option_expr {
            Some(v) => v,
            None => return return_value,  // or continue
        };
    """
    var_name: str
    option_expr: ast.expr
    return_value: ast.expr | None  # None means 'continue' instead of return
    is_continue: bool = False


class RustCodeGen:
    """Generates Rust code from Python AST."""

    # Expressions that return Option types
    OPTION_EXPRESSIONS = {
        "ctx.book", "ctx.position", "ctx.mid",
    }
    # Attributes on OrderBook that are Option<&Level> (method calls that need .price extraction)
    OPTION_LEVEL_ATTRS = {"best_bid", "best_ask"}
    # Attributes on OrderBook that are Option<Decimal> (method calls)
    OPTION_DECIMAL_ATTRS = {"mid_price", "spread", "spread_bps", "imbalance"}
    # Attributes on OrderBook that are Decimal (method calls, non-Option)
    DECIMAL_METHOD_ATTRS = {"ask_size", "bid_size", "bid_depth", "ask_depth"}
    # Attributes on MarketInfo that are Option types
    MARKET_OPTION_ATTRS = {"end_date", "hours_until_expiry", "liquidity"}
    # Attributes on MarketInfo that are String types (need .clone())
    MARKET_STRING_ATTRS = {"question", "outcome", "slug"}

    def __init__(self, meta: StrategyMeta):
        self.meta = meta
        self.struct_name = self._to_pascal_case(meta.name)
        self.indent_level = 0
        # Track variables that hold Option values (not yet unwrapped)
        self.option_vars: set[str] = set()
        # Track variables that were unwrapped from Options
        self.unwrapped_vars: set[str] = set()
        # Track variables that need to be mutable
        self.mutable_vars: set[str] = set()
        # Track variables that have been declared (to avoid duplicate let)
        self.declared_vars: set[str] = set()
        # Track variables that are String type (vs &str)
        # Variables assigned with .to_string() are String
        # Variables assigned from constants are &str
        self.string_vars: set[str] = set()
        # Get param names (these are &str constants)
        self.param_names: set[str] = set(meta.params.keys()) if meta.params else set()
        # Track integer params (for counter variable detection)
        self.int_params: set[str] = set()
        if meta.params:
            for name, value in meta.params.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self.int_params.add(name)
        # Track variables that should be integers (compared against int params)
        self.int_vars: set[str] = set()

    def _to_pascal_case(self, name: str) -> str:
        """Convert snake_case to PascalCase."""
        return ''.join(word.capitalize() for word in name.split('_'))

    def _indent(self) -> str:
        return "    " * self.indent_level

    def _generate_constants(self) -> str:
        """Generate Rust constants from strategy params."""
        if not self.meta.params:
            return ""

        lines = ["// Strategy parameters (generated from Python params)"]

        for name, value in self.meta.params.items():
            rust_type, rust_value = self._param_to_rust(name, value)
            lines.append(f"const {name}: {rust_type} = {rust_value};")

        return "\n".join(lines) + "\n\n"

    def _param_to_rust(self, name: str, value: Any) -> tuple[str, str]:
        """Convert a Python parameter value to Rust type and literal."""
        return param_to_rust(name, value)

    def generate(self) -> str:
        """Generate complete Rust module for the strategy."""
        # Get source and parse
        source = inspect.getsource(self.meta.on_tick)
        # Dedent to handle indented functions
        source = textwrap.dedent(source)
        tree = ast.parse(source)
        func_def = tree.body[0]

        # Generate on_tick body
        on_tick_body = self._gen_function_body(func_def.body)

        # Build the complete Rust code
        tokens_array = ", ".join(f'"{t}".to_string()' for t in self.meta.tokens)

        # Generate constants from params
        constants = self._generate_constants()

        return f'''//! Auto-generated from Python strategy: {self.meta.name}
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{{Signal, Strategy, StrategyContext, Urgency}};
use crate::position::Fill;
#[allow(unused_imports)]
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

{constants}pub struct {self.struct_name} {{
    id: String,
    tokens: Vec<String>,
}}

impl {self.struct_name} {{
    pub fn new() -> Self {{
        Self {{
            id: "{self.meta.name}".to_string(),
            tokens: vec![{tokens_array}],
        }}
    }}
}}

impl Default for {self.struct_name} {{
    fn default() -> Self {{
        Self::new()
    }}
}}

impl Strategy for {self.struct_name} {{
    fn id(&self) -> &str {{
        &self.id
    }}

    fn subscriptions(&self) -> Vec<String> {{
        self.tokens.clone()
    }}

    fn on_tick(&mut self, ctx: &StrategyContext) -> Vec<Signal> {{
{on_tick_body}
    }}

    fn on_fill(&mut self, _fill: &Fill) {{}}
    fn on_shutdown(&mut self) {{}}
}}
'''

    def _gen_function_body(self, stmts: List[ast.stmt]) -> str:
        """Generate Rust code for a list of statements.

        Performs pattern matching to detect Option unwrapping patterns like:
            x = ctx.book(token)
            if x is None:
                return signals
        And converts them to proper Rust match expressions.
        """
        self.indent_level = 2  # Start at 2 for method body
        lines = []

        # Scan for variables that need to be mutable
        self._scan_mutability(stmts)

        # Scan for variables that should be integers (compared to int params)
        self._scan_int_vars(stmts)

        # Pre-process to combine assign + None check patterns
        processed_stmts = self._preprocess_option_patterns(stmts)

        for stmt in processed_stmts:
            # Skip docstrings
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            lines.append(self._gen_stmt(stmt))
        return "\n".join(lines)

    def _scan_int_vars(self, stmts: List[ast.stmt]) -> None:
        """Scan for variables that should be integers based on comparisons with int params."""
        for stmt in stmts:
            self._scan_stmt_int_vars(stmt)

    def _scan_stmt_int_vars(self, stmt: ast.stmt) -> None:
        """Recursively scan a statement for integer variable patterns."""
        if isinstance(stmt, ast.If):
            # Check if condition compares a variable to an int param
            self._check_compare_for_int_vars(stmt.test)
            for s in stmt.body:
                self._scan_stmt_int_vars(s)
            for s in stmt.orelse:
                self._scan_stmt_int_vars(s)
        elif isinstance(stmt, ast.For):
            for s in stmt.body:
                self._scan_stmt_int_vars(s)
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Compare):
                self._check_compare_for_int_vars(stmt.value)

    def _check_compare_for_int_vars(self, expr: ast.expr) -> None:
        """Check a comparison expression for variables compared to int params."""
        if not isinstance(expr, ast.Compare):
            return
        # Check left side against comparators
        if isinstance(expr.left, ast.Name):
            var_name = expr.left.id
            for comp in expr.comparators:
                if isinstance(comp, ast.Name) and comp.id in self.int_params:
                    self.int_vars.add(var_name)
        # Check comparators against left side
        for comp in expr.comparators:
            if isinstance(comp, ast.Name):
                if isinstance(expr.left, ast.Name) and expr.left.id in self.int_params:
                    self.int_vars.add(comp.id)

    def _scan_mutability(self, stmts: List[ast.stmt]) -> None:
        """Scan statements to find variables that need to be mutable.

        A variable needs `mut` if:
        - It has .push()/.append()/.pop() called on it
        - It's used with augmented assignment (+=, -=, etc.)
        - It's assigned multiple times (reassigned)
        """
        # Track first assignments to detect reassignment
        assigned_vars: set[str] = set()
        for stmt in stmts:
            self._scan_stmt_mutability(stmt, assigned_vars)

    def _scan_stmt_mutability(self, stmt: ast.stmt, assigned_vars: set[str]) -> None:
        """Recursively scan a statement for mutability requirements."""
        if isinstance(stmt, ast.Expr):
            # Check for method calls like x.push(), x.append()
            if isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Attribute):
                    if call.func.attr in ("push", "append", "pop", "clear", "extend"):
                        # The object being mutated
                        if isinstance(call.func.value, ast.Name):
                            self.mutable_vars.add(call.func.value.id)

        elif isinstance(stmt, ast.AugAssign):
            # x += y means x needs to be mutable
            if isinstance(stmt.target, ast.Name):
                self.mutable_vars.add(stmt.target.id)

        elif isinstance(stmt, ast.Assign):
            # Check for reassignment
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                var_name = stmt.targets[0].id
                if var_name in assigned_vars:
                    # This is a reassignment - needs mut
                    self.mutable_vars.add(var_name)
                else:
                    assigned_vars.add(var_name)

        elif isinstance(stmt, ast.AnnAssign):
            # Annotated assignment - check for reassignment
            if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                var_name = stmt.target.id
                if var_name in assigned_vars:
                    self.mutable_vars.add(var_name)
                else:
                    assigned_vars.add(var_name)

        elif isinstance(stmt, ast.If):
            for s in stmt.body:
                self._scan_stmt_mutability(s, assigned_vars)
            for s in stmt.orelse:
                self._scan_stmt_mutability(s, assigned_vars)

        elif isinstance(stmt, ast.For):
            for s in stmt.body:
                self._scan_stmt_mutability(s, assigned_vars)

    def _preprocess_option_patterns(self, stmts: List[ast.stmt]) -> List[ast.stmt]:
        """Detect and mark Option unwrapping patterns.

        Patterns detected:
        1. x = option_expr; if x is None: return y  -> MatchUnwrap(x, option_expr, y)
        2. if x.attr is None: return y; ... z = x.attr  -> MatchUnwrap(z, x.attr, y)
           (assignment can be anywhere after the None check)
        """
        result = []
        skip_indices: set[int] = set()
        # Track which attr None checks we've seen: {(obj_name, attr_name): (return_value, index)}
        pending_attr_checks: dict[tuple[str, str], tuple[ast.expr, int]] = {}

        for i, stmt in enumerate(stmts):
            if i in skip_indices:
                continue

            next_stmt = stmts[i + 1] if i + 1 < len(stmts) else None

            # Pattern 1a: x = option_expr; if x is None: return
            if (isinstance(stmt, ast.Assign) and
                len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name) and
                next_stmt is not None and
                self._is_none_check_return(next_stmt, stmt.targets[0].id)):

                var_name = stmt.targets[0].id
                option_expr = stmt.value
                return_value = next_stmt.body[0].value  # The return value

                # Create a synthetic node to represent match unwrap
                result.append(MatchUnwrap(var_name, option_expr, return_value, is_continue=False))
                self.unwrapped_vars.add(var_name)
                skip_indices.add(i + 1)
                continue

            # Pattern 1b: x = option_expr; if x is None: continue
            if (isinstance(stmt, ast.Assign) and
                len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name) and
                next_stmt is not None and
                self._is_none_check_continue(next_stmt, stmt.targets[0].id)):

                var_name = stmt.targets[0].id
                option_expr = stmt.value

                # Create a synthetic node to represent match unwrap with continue
                result.append(MatchUnwrap(var_name, option_expr, None, is_continue=True))
                self.unwrapped_vars.add(var_name)
                skip_indices.add(i + 1)
                continue

            # Pattern 2a: if x.attr is None: return - record for later matching
            if self._is_attr_none_check_return(stmt):
                attr_expr = stmt.test.left  # x.attr
                if isinstance(attr_expr, ast.Attribute) and isinstance(attr_expr.value, ast.Name):
                    obj_name = attr_expr.value.id
                    attr_name = attr_expr.attr
                    return_value = stmt.body[0].value
                    pending_attr_checks[(obj_name, attr_name)] = (return_value, i, False)  # False = not continue
                    continue  # Don't add to result yet

            # Pattern 2a': if x.attr is None: continue - record for later matching
            if self._is_attr_none_check_continue(stmt):
                attr_expr = stmt.test.left  # x.attr
                if isinstance(attr_expr, ast.Attribute) and isinstance(attr_expr.value, ast.Name):
                    obj_name = attr_expr.value.id
                    attr_name = attr_expr.attr
                    pending_attr_checks[(obj_name, attr_name)] = (None, i, True)  # True = is_continue
                    continue  # Don't add to result yet

            # Pattern 2b: z = x.attr - check if we have a pending None check for this
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                if isinstance(stmt.value, ast.Attribute) and isinstance(stmt.value.value, ast.Name):
                    obj_name = stmt.value.value.id
                    attr_name = stmt.value.attr
                    key = (obj_name, attr_name)

                    if key in pending_attr_checks:
                        return_value, check_idx, is_continue = pending_attr_checks.pop(key)
                        var_name = stmt.targets[0].id
                        attr_expr = stmt.value

                        result.append(MatchUnwrap(var_name, attr_expr, return_value, is_continue=is_continue))
                        self.unwrapped_vars.add(var_name)
                        # The None check was already skipped (not added to result)
                        continue

            result.append(stmt)

        # Any remaining pending checks that weren't matched - add them back as regular if statements
        # (This shouldn't happen with well-formed code, but handle it gracefully)

        return result

    def _is_none_check(self, stmt: ast.stmt, var_name: str | None, action: type) -> bool:
        """Check 'if var is None: <action>' or 'if x.attr is None: <action>'.

        Args:
            stmt: Statement to check
            var_name: Variable to check (None = check for attribute access to Option field)
            action: ast.Return or ast.Continue

        Returns:
            True if the statement matches the pattern
        """
        if not isinstance(stmt, ast.If):
            return False
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], action):
            return False
        if stmt.orelse:  # No else clause
            return False

        test = stmt.test
        if not isinstance(test, ast.Compare):
            return False
        if len(test.ops) != 1 or len(test.comparators) != 1:
            return False
        if not isinstance(test.ops[0], (ast.Is, ast.Eq)):
            return False
        if not isinstance(test.comparators[0], ast.Constant) or test.comparators[0].value is not None:
            return False

        if var_name is not None:
            # Check for simple variable: `if var_name is None`
            if not isinstance(test.left, ast.Name) or test.left.id != var_name:
                return False
        else:
            # Check for attribute access: `if x.attr is None` where attr is an Option field
            if not isinstance(test.left, ast.Attribute):
                return False
            if test.left.attr not in (self.OPTION_LEVEL_ATTRS | self.OPTION_DECIMAL_ATTRS):
                return False

        return True

    # Thin wrappers for backward compatibility in _preprocess_option_patterns
    def _is_none_check_return(self, stmt: ast.stmt, var_name: str) -> bool:
        """Check if stmt is 'if var_name is None: return ...'"""
        return self._is_none_check(stmt, var_name, ast.Return)

    def _is_none_check_continue(self, stmt: ast.stmt, var_name: str) -> bool:
        """Check if stmt is 'if var_name is None: continue'"""
        return self._is_none_check(stmt, var_name, ast.Continue)

    def _is_attr_none_check_return(self, stmt: ast.stmt) -> bool:
        """Check if stmt is 'if x.attr is None: return ...' where attr is an Option field."""
        return self._is_none_check(stmt, None, ast.Return)

    def _is_attr_none_check_continue(self, stmt: ast.stmt) -> bool:
        """Check if stmt is 'if x.attr is None: continue' where attr is an Option field."""
        return self._is_none_check(stmt, None, ast.Continue)

    def _assigns_same_attr(self, assign_stmt: ast.Assign, if_stmt: ast.If) -> bool:
        """Check if assign_stmt assigns the same attribute that if_stmt checks."""
        if not isinstance(assign_stmt.value, ast.Attribute):
            return False

        if_attr = if_stmt.test.left  # x.attr from the if check
        assign_attr = assign_stmt.value  # x.attr from the assignment

        # Compare object and attribute
        if not isinstance(if_attr, ast.Attribute) or not isinstance(assign_attr, ast.Attribute):
            return False
        if if_attr.attr != assign_attr.attr:
            return False

        # Compare the object being accessed
        if isinstance(if_attr.value, ast.Name) and isinstance(assign_attr.value, ast.Name):
            return if_attr.value.id == assign_attr.value.id

        return False

    def _gen_stmt(self, stmt) -> str:
        """Generate Rust code for a statement."""
        # Handle our synthetic MatchUnwrap node
        if isinstance(stmt, MatchUnwrap):
            return self._gen_match_unwrap(stmt)
        elif isinstance(stmt, ast.Return):
            return self._gen_return(stmt)
        elif isinstance(stmt, ast.Assign):
            return self._gen_assign(stmt)
        elif isinstance(stmt, ast.If):
            return self._gen_if(stmt)
        elif isinstance(stmt, ast.For):
            return self._gen_for(stmt)
        elif isinstance(stmt, ast.Expr):
            # Expression statement (e.g., function call)
            return f"{self._indent()}{self._gen_expr(stmt.value)};"
        elif isinstance(stmt, ast.AugAssign):
            return self._gen_aug_assign(stmt)
        elif isinstance(stmt, ast.Continue):
            return f"{self._indent()}continue;"
        elif isinstance(stmt, ast.Break):
            return f"{self._indent()}break;"
        elif isinstance(stmt, ast.AnnAssign):
            return self._gen_ann_assign(stmt)
        else:
            return f"{self._indent()}// TODO: unsupported stmt {type(stmt).__name__}"

    def _gen_match_unwrap(self, node: MatchUnwrap) -> str:
        """Generate a match expression that unwraps an Option or returns early/continues."""
        # Check if this is a Level attribute (best_bid, best_ask) that needs .price extraction
        is_level_attr = False
        if isinstance(node.option_expr, ast.Attribute):
            if node.option_expr.attr in self.OPTION_LEVEL_ATTRS:
                is_level_attr = True

        # Generate the option expression (converts attributes to method calls)
        option_expr = self._gen_expr(node.option_expr)

        # For Level attributes, extract .price; otherwise just use v
        unwrap_expr = "v.price" if is_level_attr else "v"

        # Generate the None arm - either continue or return
        if node.is_continue:
            none_arm = "continue"
        else:
            return_expr = self._gen_expr(node.return_value)
            none_arm = f"return {return_expr}"

        return f"""{self._indent()}let {node.var_name} = match {option_expr} {{
{self._indent()}    Some(v) => {unwrap_expr},
{self._indent()}    None => {none_arm},
{self._indent()}}};"""

    def _gen_return(self, stmt: ast.Return) -> str:
        if stmt.value is None:
            return f"{self._indent()}return vec![];"
        expr = self._gen_expr(stmt.value)
        # Check if the expression already looks like a vec! or similar
        if expr.startswith("vec!"):
            return f"{self._indent()}return {expr};"
        return f"{self._indent()}return {expr};"

    def _gen_assign(self, stmt: ast.Assign) -> str:
        target_node = stmt.targets[0]
        target = self._gen_expr(target_node)

        # Check if this is a reassignment (variable already declared)
        if isinstance(target_node, ast.Name):
            var_name = target_node.id

            # Generate value, using integer literals for int_vars
            if var_name in self.int_vars:
                value = self._gen_int_expr(stmt.value)
            else:
                value = self._gen_expr(stmt.value)

            # Track if this variable is a String type
            # It's a String if the value is a string literal (generates .to_string())
            if self._is_string_type(stmt.value):
                self.string_vars.add(var_name)

            if var_name in self.declared_vars:
                # Reassignment - no let keyword
                return f"{self._indent()}{target} = {value};"
            else:
                # First declaration
                self.declared_vars.add(var_name)
                # Check if this variable needs to be mutable
                mut = "mut " if var_name in self.mutable_vars else ""
                return f"{self._indent()}let {mut}{target} = {value};"

        value = self._gen_expr(stmt.value)
        return f"{self._indent()}let {target} = {value};"

    def _gen_int_expr(self, expr: ast.expr) -> str:
        """Generate Rust code for an expression that should be an integer."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, int) and not isinstance(expr.value, bool):
            return str(expr.value)
        elif isinstance(expr, ast.BinOp):
            left = self._gen_int_expr(expr.left)
            right = self._gen_int_expr(expr.right)
            op = self._gen_binop(expr.op)
            return f"{left} {op} {right}"
        elif isinstance(expr, ast.Name):
            return expr.id
        else:
            return self._gen_expr(expr)

    def _is_string_type(self, expr: ast.expr) -> bool:
        """Check if an expression results in a String type (vs &str).

        Returns True for:
        - String literals (we generate .to_string())

        Returns False for:
        - Name references to constants (they are &str)
        - Attribute accesses
        """
        # String literal → generates .to_string() → String type
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return True
        # Assignment from another variable - check if that's a String
        if isinstance(expr, ast.Name):
            return expr.id in self.string_vars
        return False

    def _gen_ann_assign(self, stmt: ast.AnnAssign) -> str:
        """Generate Rust code for annotated assignment (e.g., signals: list[Signal] = [])."""
        if stmt.value is None:
            # Declaration without value - skip
            return f"{self._indent()}// (declaration only)"

        target = self._gen_expr(stmt.target)
        value = self._gen_expr(stmt.value)

        # Track as declared
        if isinstance(stmt.target, ast.Name):
            var_name = stmt.target.id
            if var_name in self.declared_vars:
                return f"{self._indent()}{target} = {value};"
            self.declared_vars.add(var_name)
            mut = "mut " if var_name in self.mutable_vars else ""
            return f"{self._indent()}let {mut}{target} = {value};"

        return f"{self._indent()}let {target} = {value};"

    def _gen_aug_assign(self, stmt: ast.AugAssign) -> str:
        target = self._gen_expr(stmt.target)
        value = self._gen_expr(stmt.value)
        op = self._gen_binop(stmt.op)
        return f"{self._indent()}{target} {op}= {value};"

    def _gen_if(self, stmt: ast.If) -> str:
        # Check for "if x is not None:" pattern where x is Option
        # Generate "if let Some(x) = x {" instead
        if_let_result = self._try_if_let_some(stmt)
        if if_let_result:
            return if_let_result

        cond = self._gen_expr(stmt.test)
        lines = [f"{self._indent()}if {cond} {{"]
        self.indent_level += 1
        for s in stmt.body:
            lines.append(self._gen_stmt(s))
        self.indent_level -= 1

        if stmt.orelse:
            if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
                # elif
                lines.append(f"{self._indent()}}} else {self._gen_if(stmt.orelse[0]).lstrip()}")
                return "\n".join(lines)
            else:
                lines.append(f"{self._indent()}}} else {{")
                self.indent_level += 1
                for s in stmt.orelse:
                    lines.append(self._gen_stmt(s))
                self.indent_level -= 1

        lines.append(f"{self._indent()}}}")
        return "\n".join(lines)

    def _try_if_let_some(self, stmt: ast.If) -> str | None:
        """Try to convert 'if x is not None:' to 'if let Some(x) = x {'.

        Returns generated code if pattern matches, None otherwise.
        """
        # Check for "x is not None" pattern
        test = stmt.test
        if not isinstance(test, ast.Compare):
            return None
        if len(test.ops) != 1 or len(test.comparators) != 1:
            return None
        if not isinstance(test.ops[0], ast.IsNot):
            return None
        if not isinstance(test.comparators[0], ast.Constant) or test.comparators[0].value is not None:
            return None
        if not isinstance(test.left, ast.Name):
            return None

        var_name = test.left.id

        # Generate if let Some pattern
        lines = [f"{self._indent()}if let Some({var_name}) = {var_name} {{"]
        self.indent_level += 1

        # Track that this variable is now unwrapped within this scope
        old_unwrapped = self.unwrapped_vars.copy()
        self.unwrapped_vars.add(var_name)

        for s in stmt.body:
            lines.append(self._gen_stmt(s))

        # Restore unwrapped state
        self.unwrapped_vars = old_unwrapped

        self.indent_level -= 1

        if stmt.orelse:
            lines.append(f"{self._indent()}}} else {{")
            self.indent_level += 1
            for s in stmt.orelse:
                lines.append(self._gen_stmt(s))
            self.indent_level -= 1

        lines.append(f"{self._indent()}}}")
        return "\n".join(lines)

    def _gen_for(self, stmt: ast.For) -> str:
        # Check for `for token_id, market in ctx.markets.items()` pattern
        if self._is_markets_iteration(stmt):
            return self._gen_markets_for(stmt)

        target = self._gen_expr(stmt.target)
        iter_expr = self._gen_expr(stmt.iter)
        lines = [f"{self._indent()}for {target} in {iter_expr} {{"]
        self.indent_level += 1
        for s in stmt.body:
            lines.append(self._gen_stmt(s))
        self.indent_level -= 1
        lines.append(f"{self._indent()}}}")
        return "\n".join(lines)

    def _is_markets_iteration(self, stmt: ast.For) -> bool:
        """Check if this is a `for token_id, market in ctx.markets.items()` loop."""
        # Check for tuple unpacking target
        if not isinstance(stmt.target, ast.Tuple):
            return False
        if len(stmt.target.elts) != 2:
            return False

        # Check for ctx.markets.items() call
        if not isinstance(stmt.iter, ast.Call):
            return False
        if not isinstance(stmt.iter.func, ast.Attribute):
            return False
        if stmt.iter.func.attr != "items":
            return False

        # Check for ctx.markets
        iter_value = stmt.iter.func.value
        if not isinstance(iter_value, ast.Attribute):
            return False
        if iter_value.attr != "markets":
            return False
        if not isinstance(iter_value.value, ast.Name):
            return False
        if iter_value.value.id != "ctx":
            return False

        return True

    def _gen_markets_for(self, stmt: ast.For) -> str:
        """Generate Rust code for iterating over ctx.markets."""
        # Extract variable names
        token_var = stmt.target.elts[0].id if isinstance(stmt.target.elts[0], ast.Name) else "token_id"
        market_var = stmt.target.elts[1].id if isinstance(stmt.target.elts[1], ast.Name) else "market"

        lines = [f"{self._indent()}for ({token_var}, {market_var}) in ctx.markets.iter() {{"]
        self.indent_level += 1

        # Preprocess the for loop body for Option patterns
        processed_body = self._preprocess_option_patterns(stmt.body)

        for s in processed_body:
            lines.append(self._gen_stmt(s))
        self.indent_level -= 1
        lines.append(f"{self._indent()}}}")
        return "\n".join(lines)

    def _gen_expr(self, expr: ast.expr) -> str:
        """Generate Rust code for an expression."""
        if isinstance(expr, ast.Name):
            return self._gen_name(expr)
        elif isinstance(expr, ast.Constant):
            return self._gen_constant(expr)
        elif isinstance(expr, ast.Call):
            return self._gen_call(expr)
        elif isinstance(expr, ast.Attribute):
            return self._gen_attribute(expr)
        elif isinstance(expr, ast.Compare):
            return self._gen_compare(expr)
        elif isinstance(expr, ast.BoolOp):
            return self._gen_boolop(expr)
        elif isinstance(expr, ast.BinOp):
            return self._gen_binop_expr(expr)
        elif isinstance(expr, ast.UnaryOp):
            return self._gen_unaryop(expr)
        elif isinstance(expr, ast.List):
            return self._gen_list(expr)
        elif isinstance(expr, ast.Subscript):
            return self._gen_subscript(expr)
        elif isinstance(expr, ast.IfExp):
            return self._gen_ifexp(expr)
        else:
            return f"/* TODO: {type(expr).__name__} */"

    def _gen_name(self, expr: ast.Name) -> str:
        # Map Python names to Rust
        name_map = {
            "True": "true",
            "False": "false",
            "None": "None",
            "signals": "signals",
        }
        return name_map.get(expr.id, expr.id)

    def _gen_constant(self, expr: ast.Constant) -> str:
        if isinstance(expr.value, str):
            return f'"{expr.value}".to_string()'
        elif isinstance(expr.value, bool):
            return "true" if expr.value else "false"
        elif isinstance(expr.value, float):
            # Keep floats as floats (for f64 comparisons)
            return str(expr.value)
        elif isinstance(expr.value, int):
            # Integers default to Decimal, but small ones used in comparisons
            # might be intended as regular numbers. Use dec! for safety.
            return f"dec!({expr.value})"
        elif expr.value is None:
            return "None"
        return str(expr.value)

    def _borrow_string_args(self, args: list[ast.expr]) -> str:
        """Generate borrowed string arguments for HashMap get() calls.

        For local String variables, we need to borrow them with &.
        For &str variables (constants, params), we pass them directly.
        """
        result = []
        for arg in args:
            if isinstance(arg, ast.Name):
                var_name = arg.id
                # Check if this variable is a String type (needs borrowing)
                if var_name in self.string_vars:
                    result.append(f"&{var_name}")
                else:
                    # &str type (constant, param) or iteration variable - pass directly
                    result.append(var_name)
            else:
                # Not a name - generate normally (string literals, etc.)
                result.append(self._gen_expr(arg))
        return ", ".join(result)

    def _gen_call(self, expr: ast.Call) -> str:
        """Generate Rust code for a function call."""
        # Handle special cases
        if isinstance(expr.func, ast.Attribute):
            obj = self._gen_expr(expr.func.value)
            method = expr.func.attr
            args = ", ".join(self._gen_expr(a) for a in expr.args)

            # ctx.book(token_id) -> ctx.order_books.get(&token_id)
            # When the argument is a local variable (Name), we need to borrow it
            # When it comes from iteration (like `for token_id, market in ctx.markets.items()`),
            # it's already &String so we don't add another &
            if obj == "ctx" and method == "book":
                borrowed_args = self._borrow_string_args(expr.args)
                return f"ctx.order_books.get({borrowed_args})"
            # ctx.position(token_id) -> ctx.positions.get(&token_id)
            elif obj == "ctx" and method == "position":
                borrowed_args = self._borrow_string_args(expr.args)
                return f"ctx.positions.get({borrowed_args})"
            # ctx.mid(token_id) -> ctx.order_books.get(&token_id).and_then(|b| b.mid_price())
            elif obj == "ctx" and method == "mid":
                borrowed_args = self._borrow_string_args(expr.args)
                return f"ctx.order_books.get({borrowed_args}).and_then(|b| b.mid_price())"
            # vec.append(x) -> vec.push(x)
            elif method == "append":
                return f"{obj}.push({args})"
            # str.lower() -> str.to_lowercase()
            elif method == "lower":
                return f"{obj}.to_lowercase()"
            # str.upper() -> str.to_uppercase()
            elif method == "upper":
                return f"{obj}.to_uppercase()"
            # str.contains(x) -> str.contains(x) (same in Rust)
            elif method == "contains":
                return f"{obj}.contains({args})"
            else:
                return f"{obj}.{method}({args})"

        elif isinstance(expr.func, ast.Name):
            func_name = expr.func.id

            # Signal types
            if func_name == "Buy":
                return self._gen_signal_call("Buy", expr)
            elif func_name == "Sell":
                return self._gen_signal_call("Sell", expr)
            elif func_name == "Cancel":
                return self._gen_cancel_call(expr)
            elif func_name == "Hold":
                return "Signal::Hold"
            elif func_name == "Shutdown":
                return self._gen_shutdown_call(expr)
            # Decimal("0.5") -> dec!(0.5)
            elif func_name == "Decimal":
                arg = expr.args[0]
                if isinstance(arg, ast.Constant):
                    return f"dec!({arg.value})"
                return f"Decimal::from_str({self._gen_expr(arg)}).unwrap()"
            # vec![] equivalent
            elif func_name == "list":
                return "vec![]"
            else:
                args = ", ".join(self._gen_expr(a) for a in expr.args)
                return f"{func_name}({args})"

        return "/* unknown call */"

    def _gen_signal_call(self, signal_type: str, expr: ast.Call) -> str:
        """Generate Signal::Buy or Signal::Sell."""
        # Extract keyword arguments
        kwargs = {kw.arg: self._gen_expr(kw.value) for kw in expr.keywords}

        token_id = kwargs.get("token_id", '""')
        price = kwargs.get("price", "dec!(0)")
        size = kwargs.get("size", "dec!(0)")
        urgency = kwargs.get("urgency", "Urgency::Medium")

        # Handle Urgency enum
        if "Urgency." in urgency:
            urgency = urgency.replace("Urgency.", "Urgency::")

        # Always convert token_id to String using .to_string()
        # This works for both &str (constants/variables) and &String (iteration variables)
        # since both implement ToString
        if not token_id.startswith('"'):
            token_id = f"{token_id}.to_string()"

        return f"Signal::{signal_type} {{ token_id: {token_id}, price: {price}, size: {size}, urgency: {urgency} }}"

    def _gen_cancel_call(self, expr: ast.Call) -> str:
        """Generate Signal::Cancel."""
        kwargs = {kw.arg: self._gen_expr(kw.value) for kw in expr.keywords}
        token_id = kwargs.get("token_id", '""')

        # Always use .to_string() for token_id (works for both &str and &String)
        if not token_id.startswith('"'):
            token_id = f"{token_id}.to_string()"

        return f"Signal::Cancel {{ token_id: {token_id} }}"

    def _gen_shutdown_call(self, expr: ast.Call) -> str:
        """Generate Signal::Shutdown."""
        kwargs = {kw.arg: self._gen_expr(kw.value) for kw in expr.keywords}
        reason = kwargs.get("reason", '""')
        return f"Signal::Shutdown {{ reason: {reason}.to_string() }}"

    def _gen_attribute(self, expr: ast.Attribute) -> str:
        obj = self._gen_expr(expr.value)
        attr = expr.attr

        # Special mappings
        if obj == "ctx":
            attr_map = {
                "timestamp": "ctx.timestamp",
                "total_pnl": "(ctx.realized_pnl + ctx.unrealized_pnl)",
                "total_realized_pnl": "ctx.realized_pnl",
                "total_unrealized_pnl": "ctx.unrealized_pnl",
                "usdc_balance": "ctx.usdc_balance",
            }
            return attr_map.get(attr, f"ctx.{attr}")

        # Urgency enum - map Python UPPER_CASE to Rust PascalCase
        if obj == "Urgency":
            urgency_map = {
                "LOW": "Low",
                "MEDIUM": "Medium",
                "HIGH": "High",
                "IMMEDIATE": "Immediate",
            }
            return f"Urgency::{urgency_map.get(attr, attr)}"

        # OrderBook method attributes - convert to method calls
        if attr in self.OPTION_LEVEL_ATTRS or attr in self.OPTION_DECIMAL_ATTRS or attr in self.DECIMAL_METHOD_ATTRS:
            return f"{obj}.{attr}()"

        # MarketInfo string attributes - need .clone()
        if attr in self.MARKET_STRING_ATTRS:
            return f"{obj}.{attr}.clone()"

        # MarketInfo Option attributes - direct access
        if attr in self.MARKET_OPTION_ATTRS:
            return f"{obj}.{attr}"

        return f"{obj}.{attr}"

    def _gen_compare(self, expr: ast.Compare) -> str:
        left = self._gen_expr(expr.left)

        # Handle "x is None" / "x is not None" patterns
        if len(expr.ops) == 1 and len(expr.comparators) == 1:
            op = expr.ops[0]
            comp = expr.comparators[0]
            if isinstance(comp, ast.Constant) and comp.value is None:
                if isinstance(op, ast.Is):
                    return f"{left}.is_none()"
                elif isinstance(op, ast.IsNot):
                    return f"{left}.is_some()"
            # Also handle "x != None" and "x == None"
            if isinstance(comp, ast.Constant) and comp.value is None:
                if isinstance(op, ast.NotEq):
                    return f"{left}.is_some()"
                elif isinstance(op, ast.Eq):
                    return f"{left}.is_none()"

            # Handle "x in y" -> y.contains(x) for substring checks
            if isinstance(op, ast.In):
                right = self._gen_expr(comp)
                # Don't add & if left is already a reference (like loop variables)
                return f"{right}.contains({left})"
            # Handle "x not in y" -> !y.contains(x)
            elif isinstance(op, ast.NotIn):
                right = self._gen_expr(comp)
                return f"!{right}.contains({left})"

        parts = [left]
        for op, comparator in zip(expr.ops, expr.comparators):
            op_str = self._gen_cmpop(op)
            right = self._gen_expr(comparator)
            parts.append(f"{op_str} {right}")
        return " ".join(parts)

    def _gen_cmpop(self, op: ast.cmpop) -> str:
        ops = {
            ast.Eq: "==",
            ast.NotEq: "!=",
            ast.Lt: "<",
            ast.LtE: "<=",
            ast.Gt: ">",
            ast.GtE: ">=",
            ast.Is: "==",
            ast.IsNot: "!=",
        }
        return ops.get(type(op), "==")

    def _gen_boolop(self, expr: ast.BoolOp) -> str:
        op_str = " && " if isinstance(expr.op, ast.And) else " || "
        values = [self._gen_expr(v) for v in expr.values]
        return f"({op_str.join(values)})"

    def _gen_binop_expr(self, expr: ast.BinOp) -> str:
        # For nested binops, wrap in parens for correct precedence
        left = self._gen_expr(expr.left)
        right = self._gen_expr(expr.right)
        op = self._gen_binop(expr.op)

        # Wrap operands in parens if they are binary ops (for precedence)
        if isinstance(expr.left, ast.BinOp):
            left = f"({left})"
        if isinstance(expr.right, ast.BinOp):
            right = f"({right})"

        return f"{left} {op} {right}"

    def _gen_binop(self, op: ast.operator) -> str:
        ops = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.Mod: "%",
        }
        return ops.get(type(op), "+")

    def _gen_unaryop(self, expr: ast.UnaryOp) -> str:
        operand = self._gen_expr(expr.operand)
        if isinstance(expr.op, ast.Not):
            return f"!{operand}"
        elif isinstance(expr.op, ast.USub):
            return f"-{operand}"
        return operand

    def _gen_list(self, expr: ast.List) -> str:
        if not expr.elts:
            return "vec![]"
        elts = ", ".join(self._gen_expr(e) for e in expr.elts)
        return f"vec![{elts}]"

    def _gen_subscript(self, expr: ast.Subscript) -> str:
        obj = self._gen_expr(expr.value)
        idx = self._gen_expr(expr.slice)
        # Translate dict access to .get()
        return f"{obj}.get(&{idx})"

    def _gen_ifexp(self, expr: ast.IfExp) -> str:
        test = self._gen_expr(expr.test)
        body = self._gen_expr(expr.body)
        orelse = self._gen_expr(expr.orelse)

        # Handle "x if x else y" pattern for lists -> "if !x.is_empty() { x } else { y }"
        # This is common for "signals if signals else [Hold()]"
        if isinstance(expr.test, ast.Name) and isinstance(expr.body, ast.Name):
            if expr.test.id == expr.body.id:
                var_name = expr.test.id
                return f"if !{var_name}.is_empty() {{ {body} }} else {{ {orelse} }}"

        return f"if {test} {{ {body} }} else {{ {orelse} }}"


def transpile(strategy_func: Callable, validate: bool = True, strict: bool = True) -> TranspileResult:
    """Transpile a Python strategy function to Rust.

    Args:
        strategy_func: A function decorated with @strategy
        validate: If True, validate the strategy before transpiling
        strict: If True (default), raise TranspileError on validation errors.
                If False, only print warnings and continue.

    Returns:
        TranspileResult with generated Rust code

    Raises:
        TranspileError: If validation fails and strict=True
        ValueError: If function is not decorated with @strategy
    """
    meta = get_strategy_meta(strategy_func)
    if meta is None:
        raise ValueError("Function must be decorated with @strategy")

    # Run validation
    if validate:
        errors, warnings = validate_strategy(strategy_func)

        # Print warnings
        for warning in warnings:
            print(f"  Warning: {warning}")

        # Handle errors
        if errors:
            error_msg = f"Strategy '{meta.name}' has {len(errors)} validation error(s):\n"
            for error in errors:
                error_msg += f"  - {error}\n"

            if strict:
                raise TranspileError(error_msg)
            else:
                print(f"  {error_msg}")

    codegen = RustCodeGen(meta)
    rust_code = codegen.generate()

    return TranspileResult(
        rust_code=rust_code,
        strategy_name=meta.name,
        struct_name=codegen.struct_name,
        tokens=meta.tokens,
    )


def transpile_to_file(strategy_func: Callable, output_path: str, validate: bool = True, strict: bool = True) -> TranspileResult:
    """Transpile a strategy and write to a file.

    Args:
        strategy_func: A function decorated with @strategy
        output_path: Path to write the generated Rust code
        validate: If True, validate the strategy before transpiling
        strict: If True (default), raise TranspileError on validation errors.

    Returns:
        TranspileResult with generated Rust code
    """
    result = transpile(strategy_func, validate=validate, strict=strict)
    with open(output_path, 'w') as f:
        f.write(result.rust_code)
    return result


def to_pascal_case(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return ''.join(word.capitalize() for word in name.split('_'))


@dataclass
class StrategyFileInfo:
    """Information extracted from a strategy .rs file."""
    module_name: str
    struct_name: str
    requires_market_discovery: bool


def scan_strategy_file(path: Path) -> StrategyFileInfo | None:
    """Extract strategy info from a Rust strategy file.

    Returns None if the file doesn't look like a valid strategy.
    """
    content = path.read_text()
    module_name = path.stem

    # Look for struct definition pattern: pub struct StructName {
    struct_match = re.search(r'pub struct (\w+)\s*\{', content)
    if not struct_match:
        return None

    struct_name = struct_match.group(1)

    # Detect if market discovery is needed:
    # Look for "tokens: vec![]" (empty tokens) in new() function
    # This indicates the strategy uses dynamic market discovery
    requires_market_discovery = bool(re.search(r'tokens:\s*vec!\[\s*\]', content))

    return StrategyFileInfo(
        module_name=module_name,
        struct_name=struct_name,
        requires_market_discovery=requires_market_discovery,
    )


def generate_mod_rs(strategies_dir: Path) -> str:
    """Generate mod.rs content with registry for all strategies in directory.

    Args:
        strategies_dir: Path to the pmengine/src/strategies directory

    Returns:
        Generated Rust code for mod.rs
    """
    # Scan for all .rs files (except mod.rs)
    strategy_files = sorted(strategies_dir.glob("*.rs"))
    strategy_files = [f for f in strategy_files if f.name != "mod.rs"]

    # Extract info from each file
    strategies: list[StrategyFileInfo] = []
    for f in strategy_files:
        info = scan_strategy_file(f)
        if info:
            strategies.append(info)

    # Sort by module name
    strategies.sort(key=lambda s: s.module_name)

    # Generate mod declarations
    mod_decls = "\n".join(f"mod {s.module_name};" for s in strategies)

    # Generate pub use statements
    pub_uses = "\n".join(f"pub use {s.module_name}::{s.struct_name};" for s in strategies)

    # Generate registry entries
    registry_entries = []
    for s in strategies:
        registry_entries.append(f'''    m.insert("{s.module_name}", StrategyInfo {{
        factory: || Box::new({s.module_name}::{s.struct_name}::new()),
        requires_market_discovery: {str(s.requires_market_discovery).lower()},
    }});''')

    registry_body = "\n\n".join(registry_entries)

    return f'''//! Auto-generated strategy registry - DO NOT EDIT MANUALLY
//! Regenerate with: pmstrat transpile --all

{mod_decls}

use std::collections::HashMap;
use crate::strategy::Strategy;

{pub_uses}

/// Information about a strategy in the registry.
pub struct StrategyInfo {{
    /// Factory function to create a new instance of the strategy.
    pub factory: fn() -> Box<dyn Strategy>,
    /// Whether this strategy requires market discovery (empty tokens list).
    pub requires_market_discovery: bool,
}}

/// Returns the strategy registry - a map of strategy names to their info.
///
/// This function is called by the engine to look up strategies by name.
/// The registry is auto-generated by `pmstrat transpile --all`.
pub fn registry() -> HashMap<&'static str, StrategyInfo> {{
    let mut m = HashMap::new();

{registry_body}

    m
}}
'''


def regenerate_mod_rs(strategies_dir: Path) -> None:
    """Regenerate mod.rs in the given strategies directory.

    Args:
        strategies_dir: Path to the pmengine/src/strategies directory
    """
    mod_rs_path = strategies_dir / "mod.rs"
    content = generate_mod_rs(strategies_dir)
    mod_rs_path.write_text(content)


def find_pmengine_strategies_dir() -> Path | None:
    """Find the pmengine/src/strategies directory relative to the current path.

    Searches in common locations relative to pmstrat.
    """
    # Common relative paths from pmstrat to pmengine
    candidates = [
        Path("../pmengine/src/strategies"),
        Path("../../pmengine/src/strategies"),
        Path("pmengine/src/strategies"),
    ]

    # Also try from cwd
    cwd = Path.cwd()
    for candidate in candidates:
        path = (cwd / candidate).resolve()
        if path.exists() and path.is_dir():
            return path

    return None


# =============================================================================
# Test Generation
# =============================================================================

@dataclass
class TestGeneratorConfig:
    """Configuration for test generation."""
    # Market discovery strategies need different test setup
    is_market_discovery: bool
    # Strategy params for filter tests
    params: dict[str, Any]
    # Strategy name and struct name
    strategy_name: str
    struct_name: str


class RustTestGenerator:
    """Generates Rust integration tests for transpiled strategies."""

    def __init__(self, config: TestGeneratorConfig):
        self.config = config
        # Detect strategy type from params
        self.is_market_maker = "SPREAD_BPS" in config.params or "MIN_SPREAD_PCT" in config.params
        self.is_sure_bets = "MIN_CERTAINTY" in config.params or "MAX_CERTAINTY" in config.params

    def generate(self) -> str:
        """Generate complete Rust test file."""
        parts = [
            self._gen_header(),
            self._gen_imports(),
            self._gen_constants(),
            self._gen_helpers(),
            self._gen_filter_tests(),
            self._gen_behavior_tests(),
            self._gen_summary_test(),
        ]
        return "\n".join(parts)

    def _gen_header(self) -> str:
        return f'''//! Auto-generated integration tests for {self.config.strategy_name}
//! DO NOT EDIT - regenerate with `pmstrat transpile`

'''

    def _gen_imports(self) -> str:
        if self.config.is_market_discovery:
            return f'''mod fixtures;

use fixtures::*;
use pmengine::strategies::{self.config.struct_name};
use pmengine::strategy::Strategy;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

'''
        else:
            # Non-market-discovery: only need minimal imports for instantiation test
            return f'''use pmengine::strategies::{self.config.struct_name};
use pmengine::strategy::Strategy;

'''

    def _gen_constants(self) -> str:
        """Generate test constants from strategy params."""
        # Only generate constants for market discovery strategies
        if not self.config.is_market_discovery:
            return ""

        lines = ["// Strategy constants (from transpiled strategy)"]
        for name, value in self.config.params.items():
            rust_type, rust_value = self._param_to_rust(name, value)
            # Only include filter-related params
            if name in ("MIN_LIQUIDITY", "MAX_LIQUIDITY", "MIN_PRICE", "MAX_PRICE",
                        "MIN_SPREAD_PCT", "MAX_SPREAD_PCT", "MIN_HOURS_TO_EXPIRY",
                        "MAX_HOURS_TO_EXPIRY", "ORDER_SIZE", "SKEW_FACTOR",
                        "SPREAD_BPS", "MIN_EDGE", "MAX_TOKENS", "MAX_POSITION"):
                lines.append(f"#[allow(dead_code)]")
                lines.append(f"const {name}: {rust_type} = {rust_value};")
        return "\n".join(lines) + "\n\n"

    def _param_to_rust(self, name: str, value: Any) -> tuple[str, str]:
        """Convert a Python parameter value to Rust type and literal."""
        return param_to_rust(name, value)

    def _gen_helpers(self) -> str:
        """Generate test helper functions.

        Returns empty string since helpers are now in fixtures/mod.rs.
        """
        return ""

    def _gen_filter_tests(self) -> str:
        """Generate filter tests based on strategy params."""
        if not self.config.is_market_discovery:
            return "// No filter tests for non-market-discovery strategies\n\n"

        tests = []

        if "MIN_LIQUIDITY" in self.config.params:
            tests.append(self._gen_liquidity_filter_test())
        if "MIN_PRICE" in self.config.params:
            tests.append(self._gen_low_price_filter_test())
        if "MAX_PRICE" in self.config.params:
            tests.append(self._gen_high_price_filter_test())
        if "MIN_HOURS_TO_EXPIRY" in self.config.params:
            tests.append(self._gen_expiry_filter_test())

        tests.append(self._gen_no_markets_test())
        return "\n".join(tests)

    def _gen_liquidity_filter_test(self) -> str:
        struct = self.config.struct_name
        # Use liquidity below MIN_LIQUIDITY param
        min_liq = self.config.params.get("MIN_LIQUIDITY", 10000.0)
        low_liq = min_liq / 2  # Half of minimum should fail
        return f'''#[test]
fn test_filters_out_low_liquidity() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, {low_liq}, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for low liquidity");
}}

'''

    def _gen_low_price_filter_test(self) -> str:
        struct = self.config.struct_name
        return f'''#[test]
fn test_filters_out_low_price() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.10), dec!(0.15), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for low price");
}}

'''

    def _gen_high_price_filter_test(self) -> str:
        struct = self.config.struct_name
        return f'''#[test]
fn test_filters_out_high_price() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.85), dec!(0.90), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for high price");
}}

'''

    def _gen_expiry_filter_test(self) -> str:
        struct = self.config.struct_name
        return f'''#[test]
fn test_filters_out_near_expiry() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 12.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold for near expiry");
}}

'''

    def _gen_no_markets_test(self) -> str:
        struct = self.config.struct_name
        return f'''#[test]
fn test_no_markets() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![]);
    let signals = strategy.on_tick(&ctx);
    let (_, _, _, holds) = count_signal_types(&signals);
    assert_eq!(holds, 1, "Should hold when no markets");
}}

'''

    def _gen_behavior_tests(self) -> str:
        """Generate behavior tests."""
        if not self.config.is_market_discovery:
            return ""

        tests = []
        tests.append(self._gen_qualifying_market_test())
        if "MAX_POSITION" in self.config.params:
            tests.append(self._gen_max_position_tests())
        tests.append(self._gen_multi_market_test())
        return "\n".join(tests)

    def _gen_qualifying_market_test(self) -> str:
        struct = self.config.struct_name

        if self.is_sure_bets:
            # Sure bets needs high certainty (0.95-0.99 ask) and short expiry
            return f'''#[test]
fn test_quotes_qualifying_market() {{
    let mut strategy = {struct}::new();
    // High certainty market: ask=0.96, expiring in 24h
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.94), dec!(0.96), 24.0, 1000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, buys, _, holds) = count_signal_types(&signals);
    assert!(buys >= 1, "Should place buy order");
    assert_eq!(holds, 0, "Should not hold");
}}

'''
        else:
            # Market maker style: needs mid in tradeable range
            return f'''#[test]
fn test_quotes_qualifying_market() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, holds) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel existing orders");
    assert_eq!(buys, 1, "Should place buy order");
    assert_eq!(sells, 1, "Should place sell order");
    assert_eq!(holds, 0, "Should not hold");
}}

'''

    def _gen_max_position_tests(self) -> str:
        struct = self.config.struct_name
        max_pos = self.config.params.get("MAX_POSITION", 75)
        return f'''#[test]
fn test_max_position_only_sells() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!({max_pos})),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel");
    assert_eq!(buys, 0, "Should not buy at max position");
    assert_eq!(sells, 1, "Should still sell");
}}

#[test]
fn test_max_short_position_only_buys() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(-{max_pos})),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert_eq!(cancels, 1, "Should cancel");
    assert_eq!(buys, 1, "Should still buy");
    assert_eq!(sells, 0, "Should not sell at max short");
}}

'''

    def _gen_multi_market_test(self) -> str:
        struct = self.config.struct_name

        if self.is_sure_bets:
            # Sure bets: multiple high certainty markets
            return f'''#[test]
fn test_quotes_multiple_markets() {{
    let mut strategy = {struct}::new();
    // Multiple high certainty markets
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.94), dec!(0.96), 24.0, 1000.0, dec!(0)),
        ("token2", dec!(0.95), dec!(0.97), 12.0, 2000.0, dec!(0)),
        ("token3", dec!(0.93), dec!(0.95), 36.0, 1500.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (_, buys, _, _) = count_signal_types(&signals);
    assert!(buys >= 1, "Should buy for at least 1 market");
}}

'''
        else:
            # Market maker style
            return f'''#[test]
fn test_quotes_multiple_markets() {{
    let mut strategy = {struct}::new();
    let ctx = create_context_with_markets(vec![
        ("token1", dec!(0.68), dec!(0.72), 48.0, 50000.0, dec!(0)),
        ("token2", dec!(0.73), dec!(0.77), 72.0, 30000.0, dec!(0)),
        ("token3", dec!(0.66), dec!(0.70), 96.0, 40000.0, dec!(0)),
    ]);
    let signals = strategy.on_tick(&ctx);
    let (cancels, buys, sells, _) = count_signal_types(&signals);
    assert!(cancels >= 1, "Should cancel for at least 1 market");
    assert!(buys >= 1, "Should buy for at least 1 market");
    assert!(sells >= 1, "Should sell for at least 1 market");
}}

'''

    def _gen_summary_test(self) -> str:
        name = self.config.strategy_name
        struct = self.config.struct_name
        return f'''#[test]
fn test_strategy_instantiation() {{
    let strategy = {struct}::new();
    assert_eq!(strategy.id(), "{name}");
}}
'''


def generate_tests(strategy_func: Callable) -> str:
    """Generate Rust test code for a strategy."""
    meta = get_strategy_meta(strategy_func)
    if meta is None:
        raise ValueError("Function must be decorated with @strategy")

    config = TestGeneratorConfig(
        is_market_discovery=len(meta.tokens) == 0,
        params=meta.params or {},
        strategy_name=meta.name,
        struct_name=to_pascal_case(meta.name),
    )

    generator = RustTestGenerator(config)
    return generator.generate()


def generate_tests_to_file(strategy_func: Callable, output_path: str) -> None:
    """Generate Rust tests and write to a file."""
    test_code = generate_tests(strategy_func)
    with open(output_path, 'w') as f:
        f.write(test_code)


def find_pmengine_tests_dir() -> Path | None:
    """Find the pmengine/tests directory relative to the current path."""
    candidates = [
        Path("../pmengine/tests"),
        Path("../../pmengine/tests"),
        Path("pmengine/tests"),
    ]

    cwd = Path.cwd()
    for candidate in candidates:
        path = (cwd / candidate).resolve()
        if path.exists() and path.is_dir():
            return path

    return None
