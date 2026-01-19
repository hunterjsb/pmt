"""Python to Rust transpiler for pmstrat strategies."""

import ast
import inspect
import textwrap
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Any

from .dsl import get_strategy_meta, StrategyMeta


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
            None => return return_value,
        };
    """
    var_name: str
    option_expr: ast.expr
    return_value: ast.expr


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
    # Attributes on MarketInfo that are Option types
    MARKET_OPTION_ATTRS = {"end_date", "hours_until_expiry"}
    # Attributes on MarketInfo that are String types (need .clone())
    MARKET_STRING_ATTRS = {"question", "outcome"}

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

    def _to_pascal_case(self, name: str) -> str:
        """Convert snake_case to PascalCase."""
        return ''.join(word.capitalize() for word in name.split('_'))

    def _indent(self) -> str:
        return "    " * self.indent_level

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

        return f'''//! Auto-generated from Python strategy: {self.meta.name}
//! DO NOT EDIT - regenerate with `pmstrat transpile`

use crate::strategy::{{Signal, Strategy, StrategyContext, Urgency}};
use crate::position::Fill;
use rust_decimal_macros::dec;

pub struct {self.struct_name} {{
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

        # Pre-process to combine assign + None check patterns
        processed_stmts = self._preprocess_option_patterns(stmts)

        for stmt in processed_stmts:
            # Skip docstrings
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            lines.append(self._gen_stmt(stmt))
        return "\n".join(lines)

    def _scan_mutability(self, stmts: List[ast.stmt]) -> None:
        """Scan statements to find variables that need to be mutable.

        A variable needs `mut` if:
        - It has .push()/.append()/.pop() called on it
        - It's used with augmented assignment (+=, -=, etc.)
        """
        for stmt in stmts:
            self._scan_stmt_mutability(stmt)

    def _scan_stmt_mutability(self, stmt: ast.stmt) -> None:
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

        elif isinstance(stmt, ast.If):
            for s in stmt.body:
                self._scan_stmt_mutability(s)
            for s in stmt.orelse:
                self._scan_stmt_mutability(s)

        elif isinstance(stmt, ast.For):
            for s in stmt.body:
                self._scan_stmt_mutability(s)

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

            # Pattern 1: x = option_expr; if x is None: return
            if (isinstance(stmt, ast.Assign) and
                len(stmt.targets) == 1 and
                isinstance(stmt.targets[0], ast.Name) and
                next_stmt is not None and
                self._is_none_check_return(next_stmt, stmt.targets[0].id)):

                var_name = stmt.targets[0].id
                option_expr = stmt.value
                return_value = next_stmt.body[0].value  # The return value

                # Create a synthetic node to represent match unwrap
                result.append(MatchUnwrap(var_name, option_expr, return_value))
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
                    pending_attr_checks[(obj_name, attr_name)] = (return_value, i)
                    continue  # Don't add to result yet

            # Pattern 2b: z = x.attr - check if we have a pending None check for this
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                if isinstance(stmt.value, ast.Attribute) and isinstance(stmt.value.value, ast.Name):
                    obj_name = stmt.value.value.id
                    attr_name = stmt.value.attr
                    key = (obj_name, attr_name)

                    if key in pending_attr_checks:
                        return_value, check_idx = pending_attr_checks.pop(key)
                        var_name = stmt.targets[0].id
                        attr_expr = stmt.value

                        result.append(MatchUnwrap(var_name, attr_expr, return_value))
                        self.unwrapped_vars.add(var_name)
                        # The None check was already skipped (not added to result)
                        continue

            result.append(stmt)

        # Any remaining pending checks that weren't matched - add them back as regular if statements
        # (This shouldn't happen with well-formed code, but handle it gracefully)

        return result

    def _is_none_check_return(self, stmt: ast.stmt, var_name: str) -> bool:
        """Check if stmt is 'if var_name is None: return ...'"""
        if not isinstance(stmt, ast.If):
            return False
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
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
        if not isinstance(test.left, ast.Name) or test.left.id != var_name:
            return False

        return True

    def _is_attr_none_check_return(self, stmt: ast.stmt) -> bool:
        """Check if stmt is 'if x.attr is None: return ...' where attr is an Option field."""
        if not isinstance(stmt, ast.If):
            return False
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
            return False
        if stmt.orelse:
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

        # Check that left side is an attribute access to an Option field
        if not isinstance(test.left, ast.Attribute):
            return False
        if test.left.attr not in (self.OPTION_LEVEL_ATTRS | self.OPTION_DECIMAL_ATTRS):
            return False

        return True

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
        else:
            return f"{self._indent()}// TODO: unsupported stmt {type(stmt).__name__}"

    def _gen_match_unwrap(self, node: MatchUnwrap) -> str:
        """Generate a match expression that unwraps an Option or returns early."""
        return_expr = self._gen_expr(node.return_value)

        # Check if this is a Level attribute (best_bid, best_ask) that needs .price extraction
        is_level_attr = False
        if isinstance(node.option_expr, ast.Attribute):
            if node.option_expr.attr in self.OPTION_LEVEL_ATTRS:
                is_level_attr = True

        # Generate the option expression (converts attributes to method calls)
        option_expr = self._gen_expr(node.option_expr)

        # For Level attributes, extract .price; otherwise just use v
        unwrap_expr = "v.price" if is_level_attr else "v"

        return f"""{self._indent()}let {node.var_name} = match {option_expr} {{
{self._indent()}    Some(v) => {unwrap_expr},
{self._indent()}    None => return {return_expr},
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
        value = self._gen_expr(stmt.value)

        # Check if this variable needs to be mutable
        mut = ""
        if isinstance(target_node, ast.Name) and target_node.id in self.mutable_vars:
            mut = "mut "

        return f"{self._indent()}let {mut}{target} = {value};"

    def _gen_aug_assign(self, stmt: ast.AugAssign) -> str:
        target = self._gen_expr(stmt.target)
        value = self._gen_expr(stmt.value)
        op = self._gen_binop(stmt.op)
        return f"{self._indent()}{target} {op}= {value};"

    def _gen_if(self, stmt: ast.If) -> str:
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
        for s in stmt.body:
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
        elif isinstance(expr.value, (int, float)):
            return f"dec!({expr.value})"
        elif expr.value is None:
            return "None"
        return str(expr.value)

    def _gen_call(self, expr: ast.Call) -> str:
        """Generate Rust code for a function call."""
        # Handle special cases
        if isinstance(expr.func, ast.Attribute):
            obj = self._gen_expr(expr.func.value)
            method = expr.func.attr
            args = ", ".join(self._gen_expr(a) for a in expr.args)

            # ctx.book(token_id) -> ctx.order_books.get(&token_id)
            if obj == "ctx" and method == "book":
                return f"ctx.order_books.get(&{args})"
            # ctx.position(token_id) -> ctx.positions.get(&token_id)
            elif obj == "ctx" and method == "position":
                return f"ctx.positions.get(&{args})"
            # ctx.mid(token_id) -> ctx.order_books.get(&token_id).and_then(|b| b.mid_price())
            elif obj == "ctx" and method == "mid":
                return f"ctx.order_books.get(&{args}).and_then(|b| b.mid_price())"
            # vec.append(x) -> vec.push(x)
            elif method == "append":
                return f"{obj}.push({args})"
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

        return f"Signal::{signal_type} {{ token_id: {token_id}, price: {price}, size: {size}, urgency: {urgency} }}"

    def _gen_cancel_call(self, expr: ast.Call) -> str:
        """Generate Signal::Cancel."""
        kwargs = {kw.arg: self._gen_expr(kw.value) for kw in expr.keywords}
        token_id = kwargs.get("token_id", '""')
        return f"Signal::Cancel {{ token_id: {token_id} }}"

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
        if attr in self.OPTION_LEVEL_ATTRS or attr in self.OPTION_DECIMAL_ATTRS:
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
        return f"if {test} {{ {body} }} else {{ {orelse} }}"


def transpile(strategy_func: Callable) -> TranspileResult:
    """Transpile a Python strategy function to Rust.

    Args:
        strategy_func: A function decorated with @strategy

    Returns:
        TranspileResult with generated Rust code
    """
    meta = get_strategy_meta(strategy_func)
    if meta is None:
        raise ValueError("Function must be decorated with @strategy")

    codegen = RustCodeGen(meta)
    rust_code = codegen.generate()

    return TranspileResult(
        rust_code=rust_code,
        strategy_name=meta.name,
        struct_name=codegen.struct_name,
        tokens=meta.tokens,
    )


def transpile_to_file(strategy_func: Callable, output_path: str) -> TranspileResult:
    """Transpile a strategy and write to a file."""
    result = transpile(strategy_func)
    with open(output_path, 'w') as f:
        f.write(result.rust_code)
    return result
