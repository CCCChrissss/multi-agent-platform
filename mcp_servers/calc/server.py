"""MCP server exposing one arithmetic-evaluation tool.

Exists as the platform's smallest possible "a brand new agent has something
real to call" capability (docs/ui-backend-integration-plan.md P0): the
calculator agent built from scratch through the UI needs at least one tool
that genuinely computes, so that "it really ran" isn't just the model
answering from its own head.

Deliberately NOT `eval()`. `expression` arrives from a model, which means an
untrusted string on a trust boundary, so this walks an `ast.parse()` tree
against a whitelist of arithmetic nodes instead -- no names, no calls, no
attribute access, no comprehensions, so there is nothing to reach the
interpreter with. Two magnitude guards on top of the whitelist (see
_MAX_EXPRESSION_CHARS / _MAX_EXPONENT) because a whitelist alone still
admits `9**9**9`, which is arithmetic, allowed, and would pin a CPU for
minutes allocating the result.

Errors are returned to the agent as ToolInputError with an instruction
("use ... instead"), never as a traceback -- the tool's return value is the
agent's feedback loop (docs/harness-engineering-principles.md, 回饋時機一).
"""

from __future__ import annotations

import ast
import json
import operator
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_servers.tool_errors import ToolInputError, guarded_tool

mcp = FastMCP("calc", log_level="WARNING")

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_OP_SYMBOLS = "+ - * / // % ** ( )"
# Long enough for any arithmetic a demo agent will produce, short enough that
# the parser can't be handed something pathological.
_MAX_EXPRESSION_CHARS = 200
# `2 ** 10000` is still "just arithmetic" to the whitelist above, but produces
# a 3000-digit integer; chained (`9 ** 9 ** 9`) it never finishes. Bound the
# exponent rather than trying to bound the result after computing it.
_MAX_EXPONENT = 64


def _log(line: str) -> None:
    # stdio transport reserves stdout for JSON-RPC -- app logs must go to stderr
    print(line, file=sys.stderr, flush=True)


def _eval(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolInputError(
                f"only numbers are allowed in an expression, got {node.value!r}. "
                f"Use digits and the operators {_OP_SYMBOLS}."
            )
        return node.value

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ToolInputError(f"unsupported unary operator. Use only {_OP_SYMBOLS}.")
        return op(_eval(node.operand))

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ToolInputError(f"unsupported operator. Use only {_OP_SYMBOLS}.")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ToolInputError(
                f"exponent {right} is too large (limit {_MAX_EXPONENT}). Use a smaller exponent."
            )
        try:
            return op(left, right)
        except ZeroDivisionError:
            raise ToolInputError("division by zero. Check the divisor before calling again.") from None

    raise ToolInputError(
        f"{type(node).__name__} is not allowed -- this tool only evaluates plain arithmetic. "
        f"Use digits and the operators {_OP_SYMBOLS}."
    )


@mcp.tool()
@guarded_tool(_log, "evaluate")
def evaluate(expression: str) -> str:
    """Evaluate an arithmetic expression and return its numeric result.

    Supports + - * / // % ** and parentheses over numbers. No variables, no
    function calls -- pass a self-contained expression such as "3 + 5 * 2".
    """
    expression = expression.strip()
    if not expression:
        raise ToolInputError('expression must be a non-empty arithmetic expression, e.g. "3 + 5 * 2".')
    if len(expression) > _MAX_EXPRESSION_CHARS:
        raise ToolInputError(
            f"expression is {len(expression)} characters, over the {_MAX_EXPRESSION_CHARS} limit. "
            "Split it into smaller calls."
        )

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolInputError(
            f"{expression!r} is not a valid arithmetic expression ({exc.msg}). "
            f'Use digits and the operators {_OP_SYMBOLS}, e.g. "3 + 5 * 2".'
        ) from None

    result = _eval(tree.body)
    _log(f"[calc-mcp] evaluated {expression!r} -> {result!r}")
    return json.dumps({"expression": expression, "result": result}, ensure_ascii=False)


if __name__ == "__main__":
    _log("[calc-mcp] server starting")
    mcp.run()
