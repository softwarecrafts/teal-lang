import itertools
import logging
from functools import singledispatch, singledispatchmethod
from typing import Dict, Tuple

from ..machine import instructionset as mi
from ..machine import types as mt
from ..teal_parser import nodes

LOG = logging.getLogger(__name__)


def flatten(list_of_lists: list) -> list:
    "Flatten one level of nesting"
    return list(itertools.chain.from_iterable(list_of_lists))


class CompileError(Exception):
    pass


class CompileToplevel:
    def __init__(self, exprs):
        """Compile a toplevel list of expressions"""
        self.functions = {}
        self.bindings = {}
        for e in exprs:
            self.compile_toplevel(e)

    def make_function(self, n: nodes.N_Definition, name="anon") -> str:
        """Make a new executable function object with a unique name, and save it"""
        count = len(self.functions)
        identifier = f"#{count}:{name}"
        fn_code = self.compile_function(n)
        self.functions[identifier] = fn_code
        return identifier

    def compile_function(self, n: nodes.N_Definition) -> list:
        """Compile a function into executable code"""
        bindings = flatten(
            [[mi.Bind(mt.TlSymbol(arg)), mi.Pop()] for arg in reversed(n.paramlist)]
        )
        body = self.compile_expr(n.body)
        return bindings + body + [mi.Return()]

    # At the toplevel, no executable code is created - only bindings

    @singledispatchmethod
    def compile_toplevel(self, n) -> None:
        raise NotImplementedError(n)

    @compile_toplevel.register
    def _(self, n: nodes.N_Binop):
        if n.op != "=":
            raise CompileError(f"{n}: Only assignment allowed at the top level.")

        if not isinstance(n.lhs, nodes.N_Id):
            raise CompileError(f"{n}: Can only assign to symbols at top level")

        if isinstance(n.rhs, nodes.N_Definition):
            identifier = self.make_function(n.rhs, name=n.lhs.name)
            val = mt.TlFunctionPtr(identifier, None)

        elif isinstance(n.rhs, nodes.N_Call):
            if isinstance(n.rhs.fn, nodes.N_Id) and n.rhs.fn.name == "import":
                identifier = n.rhs.args[0].value.name
                sym = n.rhs.args[1].symbol
                mod = n.rhs.args[1].value.name
                if sym and sym != ":frompy":
                    raise CompileError(f"{n}: Can't import non-python")

                val = mt.TlForeignPtr(identifier, mod)
            else:
                raise CompileError(f"{n}: Only import() calls are supported for now")

        else:
            raise CompileError(f"{n}: Only functions / imports allowed")

        self.bindings[n.lhs.name] = val

    # @compile_toplevel.register
    # def _(self, n: nodes.N_Definition):
    #     # Add to the global bindings table
    #     identifier = self.make_function(n)
    #     self.bindings[n.name] = mt.TlFunctionPtr(identifier, None)

    # @compile_toplevel.register
    # def _(self, n: nodes.N_Import):
    #     # TODO __builtins__?
    #     name = n.as_ or n.name
    #     self.bindings[name] = mt.TlForeignPtr(n.name, n.mod)

    # Expressions result in executable code being created

    @singledispatchmethod
    def compile_expr(self, node) -> list:
        raise NotImplementedError(node)

    @compile_expr.register
    def _(self, n: nodes.N_Definition):
        # Create a local binding to the function
        identifier = self.make_function(n)
        stack = None  # TODO?!
        return [
            mi.PushV(mt.TlFunctionPtr(identifier, stack)),
            mi.Bind(mt.TlSymbol(n.name)),
            mi.PushB(mt.TlSymbol(n.name)),  # 'return' the func ptr
        ]

    @compile_expr.register
    def _(self, n: nodes.N_Literal):
        val = mt.to_teal_type(n.value)
        return [mi.PushV(val)]

    @compile_expr.register
    def _(self, n: nodes.N_Id):
        return [mi.PushB(mt.TlSymbol(n.name))]

    @compile_expr.register
    def _(self, n: nodes.N_Progn):
        # only keep the last result
        discarded = flatten(self.compile_expr(exp) + [mi.Pop()] for exp in n.exprs[:-1])
        return discarded + self.compile_expr(n.exprs[-1])

    @compile_expr.register
    def _(self, n: nodes.N_Call):
        arg_code = flatten(self.compile_expr(arg) for arg in n.args)
        call_inst = mi.ACall if isinstance(n.fn, nodes.N_Async) else mi.Call
        return arg_code + self.compile_expr(n.fn) + [call_inst(mt.TlInt(len(n.args)))]

    @compile_expr.register
    def _(self, n: nodes.N_Async):
        if not isinstance(n.expr, nodes.N_Id):
            raise ValueError(f"Can't use async with {n.expr}")
        return self.compile_expr(n.expr)

    @compile_expr.register
    def _(self, n: nodes.N_Argument):
        # TODO optional arguments...
        return self.compile_expr(n.value)

    @compile_expr.register
    def _(self, n: nodes.N_Await):
        if not isinstance(n.expr, (nodes.N_Call, nodes.N_Id)):
            raise ValueError(f"Can't use async with {n.expr}")
        val = self.compile_expr(n.expr)
        return val + [mi.Wait(mt.TlInt(0))]

    @compile_expr.register
    def _(self, n: nodes.N_If):
        cond_code = self.compile_expr(n.cond)
        else_code = self.compile_expr(n.els)
        then_code = self.compile_expr(n.then)
        return [
            # --
            *cond_code,
            mi.PushV(mt.TlTrue()),
            mi.JumpIE(mt.TlInt(len(else_code) + 1)),  # to then_code
            *else_code,
            mi.Jump(mt.TlInt(len(then_code))),  # to Return
            *then_code,
        ]

    @compile_expr.register
    def _(self, n: nodes.N_Binop):
        rhs = self.compile_expr(n.rhs)

        if n.op == "=":
            if not isinstance(n.lhs, nodes.N_Id):
                raise ValueError(f"Can't assign to non-identifier {n.lhs}")
            return rhs + [mi.Bind(mt.TlSymbol(str(n.lhs.name)))]

        else:
            lhs = self.compile_expr(n.lhs)
            # TODO check arg order. Reverse?
            return rhs + lhs + [mi.PushB(mt.TlSymbol(str(n.op))), mi.Call(mt.TlInt(2))]


###


def tl_compile(top_exprs: list) -> Tuple[Dict, Dict]:
    """Compile top-level expressions and return (bindings, functions)"""
    res = CompileToplevel(top_exprs)
    return res.bindings, res.functions
