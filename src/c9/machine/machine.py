"""The C9 virtual machine

To implement closures, just note - a closure is just an unnamed function with
some bindings. Those bindings may be explicit, or not, but are taken from the
current lexical environment. Lexical bindings are introduced by function
definitions, or let-bindings.

"""

import logging
import importlib
from functools import singledispatchmethod
from typing import Any, Dict, List

from .controller import Controller
from .executable import Executable
from .instruction import Instruction
from .instructionset import *
from .state import State
from .probe import Probe
from .future import chain_resolve
from . import types as mt

LOG = logging.getLogger(__name__)


def traverse(o, tree_types=(list, tuple)):
    """Traverse an arbitrarily nested list"""
    if isinstance(o, tree_types):
        for value in o:
            for subvalue in traverse(value, tree_types):
                yield subvalue
    else:
        yield o


def import_python_function(fnname, modname):
    """Load function

    If modname is None, fnname is taken from __builtins__ (e.g. 'print')

    PYTHONPATH must be set up already.
    """
    if modname:
        spec = importlib.util.find_spec(modname)
        m = spec.loader.load_module()
    else:
        m = __builtins__
    fn = getattr(m, fnname)
    LOG.info("Loaded %s", fn)
    return fn


class C9Machine:
    """Virtual Machine to execute C9 bytecode.

    The machine operates in the context of a Controller. There may be multiple
    machines connected to the same controller. All machines share the same
    executable, defined by the controller.

    There is one Machine per compute node. There may be multiple compute nodes.

    When run normally, the Machine starts executing instructions from the
    beginning until the instruction pointer reaches the end.

    """

    builtins = {
        "print": Print,
        "=": Eq,
        "atomp": Atomp,
        "nullp": Nullp,
        "list": List,
        "conc": Conc,
        "first": First,
        "rest": Rest,
        "wait": Wait,
        "future": Future,
        "+": Plus,
        # "-": Minus
        "*": Multiply,
        "nth": Nth,
    }

    def __init__(self, vmid, invoker):
        self.vmid = vmid
        self.invoker = invoker
        self.data_controller = invoker.data_controller
        self.state = self.data_controller.get_state(self.vmid)
        self.probe = self.data_controller.get_probe(self.vmid)
        self.exe = self.data_controller.executable
        self.evaluator = invoker.evaluator_cls(self)
        self._foreign = {
            name: import_python_function(fn, mod)
            for name, (fn, mod) in self.exe.foreign.items()
        }
        LOG.info("locations %s", self.exe.locations.keys())
        LOG.info("foreign %s", self.exe.foreign.keys())
        # No entrypoint argument - just set the IP in the state

    @property
    def stopped(self):
        return self.state.stopped

    @property
    def terminated(self):
        """Run out of instructions to execute"""
        return self.state.ip == len(self.exe.code)

    @property
    def instruction(self):
        return self.exe.code[self.state.ip]

    def step(self):
        """Execute the current instruction and increment the IP"""
        assert self.state.ip < len(self.exe.code)
        self.probe.on_step(self)
        instr = self.exe.code[self.state.ip]
        self.state.ip += 1
        self.evali(instr)
        if self.terminated:
            self.state.stopped = True

    def run(self):
        self.probe.on_run(self)
        try:
            while not self.stopped:
                self.step()
        finally:
            self.probe.on_stopped(self)
            self.data_controller.stop(self.vmid, self.state, self.probe)

    @singledispatchmethod
    def evali(self, i: Instruction):
        """Evaluate instruction"""
        assert isinstance(i, Instruction)
        self.evaluator.evali(i)

    @evali.register
    def _(self, i: Bind):
        ptr = i.operands[0]
        val = self.state.ds_pop()
        self.state.set_bind(ptr, val)

    @evali.register
    def _(self, i: PushB):
        # The value on the stack must be a Symbol, which is used to find a
        # function to call. Binding precedence:
        #
        # local value -> functions -> foreigns -> builtins
        sym = i.operands[0]
        if not isinstance(sym, mt.C9Symbol):
            raise ValueError(sym, type(sym))

        ptr = str(sym)
        if ptr in self.state.bound_names:
            val = self.state.get_bind(ptr)
        elif ptr in self.exe.locations:
            val = mt.C9Function(ptr)
        elif ptr in self._foreign:
            val = mt.C9Foreign(ptr)
        elif ptr in C9Machine.builtins:
            val = mt.C9Instruction(ptr)
        else:
            raise Exception(f"Nothing bound to {ptr}")
        self.state.ds_push(val)

    @evali.register
    def _(self, i: PushV):
        val = i.operands[0]
        self.state.ds_push(val)

    @evali.register
    def _(self, i: Pop):
        self.state.ds_pop()

    @evali.register
    def _(self, i: Jump):
        distance = i.operands[0]
        self.state.ip += distance

    @evali.register
    def _(self, i: JumpIE):
        distance = i.operands[0]
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        if a == b:
            self.state.ip += distance

    @evali.register
    def _(self, i: Return):
        if self.state.can_return():
            self.probe.on_return(self)
            self.state.es_return()
        else:
            self.state.stopped = True
            value = self.state.ds_peek(0)
            self.probe.log(f"Returning value: {value}")

            # FIXME Why do this here? Controller can get the result after stopping
            future = self.data_controller.get_result_future(self.vmid)
            resolved_value, continuations = chain_resolve(future, value)
            if resolved_value:
                for machine, offset in continuations:
                    self.data_controller.set_future_value(
                        machine, offset, resolved_value
                    )
                    self.invoker.invoke(machine)

            self.data_controller.finish(self.vmid, value)

    @evali.register
    def _(self, i: Call):
        # Arguments for the function must already be on the stack
        num_args = i.operands[0]
        # The value to call will have been retrieved earlier by PushB.
        fn = self.state.ds_pop()
        self.probe.on_enter(self, fn)

        if isinstance(fn, mt.C9Function):
            self.state.es_enter(self.exe.locations[fn])

        elif isinstance(fn, mt.C9Foreign):
            foreign_f = self._foreign[fn]
            args = tuple(reversed([self.state.ds_pop() for _ in range(num_args)]))
            self.probe.log(f"--> {foreign_f} {args}")
            # TODO automatically wait for the args? Somehow mark which one we're
            # waiting for in the continuation
            result = foreign_f(*args)  # TODO convert types?
            self.state.ds_push(result)

        elif isinstance(fn, mt.C9Instruction):
            instr = C9Machine.builtins[fn](num_args)
            self.evali(instr)

        else:
            raise Exception(f"Don't know how to call {fn} ({type(fn)})")

    @evali.register
    def _(self, i: ACall):
        # Arguments for the function must already be on the stack
        # ACall can *only* call functions in self.locations (unlike Call)
        num_args = i.operands[0]
        fn_name = self.state.ds_pop()
        args = reversed([self.state.ds_pop() for _ in range(num_args)])

        machine = self.data_controller.new_machine(args, fn_name)
        self.invoker.invoke(machine)
        future = self.data_controller.get_result_future(machine)

        self.probe.log(f"Fork {self} => {future}")
        self.state.ds_push(future)

    @evali.register
    def _(self, i: Wait):
        offset = 0  # TODO cleanup - no more offset!
        val = self.state.ds_peek(offset)

        if self.data_controller.is_future(val):
            resolved, result = self.data_controller.get_or_wait(self.vmid, val, offset)
            if resolved:
                self.probe.log(f"Resolved! {offset} -> {result}")
                self.state.ds_set(offset, result)
            else:
                self.probe.log(f"Waiting for {val}")
                self.state.stopped = True

        elif isinstance(val, list) and any(
            self.data_controller.is_future(elt) for elt in traverse(val)
        ):
            # The programmer is responsible for waiting on all elements
            # of lists.
            # NOTE - we don't try to detect futures hidden in other
            # kinds of structured data, which could cause runtime bugs!
            raise Exception("Waiting on a list that contains futures!")

        else:
            # Not an exception. This can happen if a wait is generated for a
            # normal function call. ie the value already exists.
            pass

    ## "builtins":

    @evali.register
    def _(self, i: Atomp):
        val = self.state.ds_pop()
        self.state.ds_push(not isinstance(val, list))

    @evali.register
    def _(self, i: Nullp):
        val = self.state.ds_pop()
        self.state.ds_push(len(val) == 0)

    @evali.register
    def _(self, i: List):
        num_args = i.operands[0]
        elts = [self.state.ds_pop() for _ in range(num_args)]
        self.state.ds_push(mt.C9List(reversed(elts)))

    @evali.register
    def _(self, i: Conc):
        b = self.state.ds_pop()
        a = self.state.ds_pop()

        # Null is interpreted as the empty list for b
        b = mt.C9List([]) if isinstance(b, mt.C9Null) else b

        if not isinstance(b, mt.C9List):
            raise Exception(f"b ({b}, {type(b)}) is not a list")

        if isinstance(a, mt.C9List):
            self.state.ds_push(mt.C9List(a + b))
        else:
            self.state.ds_push(mt.C9List([a] + b))

    @evali.register
    def _(self, i: First):
        lst = self.state.ds_pop()
        self.state.ds_push(lst[0])

    @evali.register
    def _(self, i: Rest):
        lst = self.state.ds_pop()
        self.state.ds_push(lst[1:])

    @evali.register
    def _(self, i: Eq):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(a == b)

    @evali.register
    def _(self, i: Plus):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(a + b)

    @evali.register
    def _(self, i: Multiply):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(a * b)

    def __repr__(self):
        return f"<Machine {id(self)}>"


## Notes dumping ground (here madness lies)...


# Foreign function calls produce futures. This allows evaluation to continue. We
# assume that they are "long-running".
#
# When a MFCall is encountered, there are two options:
# - block until there is compute resource to evaluate it
# - submit it for evaluation (local or remote), returning a future
#
# So FCalls always return Futures. Also, when an MFCall is made, the arguments
# must be resolved. They are removed from the stack and replaced with a future.
#
# The Future returned by an MFCall can be passed around on the stack, and can be
# waited upon. Each future can only be Waited on once. The resolved future can
# of course be passed around. Actually maybe not - you can have multiple
# continuations, and you continue them in the same way as performing an fcall.
#
# When an MFCall finishes, it resolves the future. If there is a continuation for
# that future (ie something else Waited on it), then execution is continued from
# that point.
#
# To continue execution, the IP, stack, and bindings must be retrieved.
#
# When a Call is encountered, it is evaluated immediately. It may modify the
# stack (from the caller's point of view).
#
# Actually, there are Calls and ACalls, and FCalls are a special type of ACall
# (or Call!). The arguments to FCalls must be resolved, but the arguments to
# ACalls don't need to be (they can Wait).
#
# When a Wait is encountered, there are two options:
# - if the future has already resolved, continue
# - otherwise, save a continuation and terminate
#
# When  MFCall finishes, two options:
# - if a continuation exists, go there
# - otherwise, terminate
#
# When Return from (sync) Call is encountered, restore previous bindings and
# stack, and jump back


# Call or MFCall: push values onto stack. Result will be top value on the stack
# upon return. This is ok because the stack isn't shared.
#
# CallA or RunA: I don't think RunA needs to be implemented. It can be wrapped
# with CallA. CallA creates a new machine that starts at the given function, and
# resolves the future at the top of the stack. When the future resolves, it
# jumps back to the caller, with the caller stack.
#
# Function defintions themselves don't have to be "sync" or "async". CallA just
# fills in the extra logic in the implementation. This is a CISC-style approach.


# Programming the VM with other l.anguages
#
# Build a minimal "parser" - read a text file of assembly line by line. All
# operands are numbers or strings. Then, to run it, provide an "environment"
# (dict) of foreign functions that the machine can call. So a JS frontend can
# work just like the python one - it just needs to compile right, and provide
# the env.


# MFCall runs something in *Python* syncronously. It takes a function, and a
# number of arguments, and literally calls it. This function may be a type
# constructor from a language point of view, or not. It may return a Future. The
# machine knows how to wait for Futures.
#
# This allows "async execution" to be entirely implementation defined. The
# machine has no idea how to do it. It knows how to handle the result though. So
# it's becoming more like a very simple Forth-style stack machine. Be careful,
# either approach could work - pick one. Forth: very flexible, but more complex.
# Builtins: less flexible, but simpler.
