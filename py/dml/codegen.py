# © 2021-2022 Intel Corporation
# SPDX-License-Identifier: MPL-2.0

import re
from abc import ABCMeta, abstractmethod
import operator
import contextlib
from functools import reduce
import itertools
import os

from . import objects, crep, ctree, ast, int_register, logging, serialize
from . import dmlparse
from .logging import *
from .expr import *
from .ctree import *
from .expr_util import *
from .symtab import *
from .messages import *
from .output import out
from .types import *
import dml.globals

__all__ = (
    'mark_method_exported',
    'mark_method_referenced',
    'exported_methods',
    'method_queue',
    'saved_method_variables',
    'simple_events',

    'eval_type',
    'eval_method_inp',
    'eval_method_outp',
    'eval_initializer',
    'get_initializer',
    'codegen_expression',
    'codegen_expression_maybe_nonvalue',

    'NoFailure',
    'InitFailure',
    'LogFailure',
    'CatchFailure',
    'ReturnFailure',

    'c_rettype',
    'c_inargs',
    'method_instance',
    'require_fully_typed',
    'codegen_method_func',
    'codegen_method',
    'mkcall_method',
    'codegen_call',
    'codegen_call_byname',
    'codegen_call_expr',
    'codegen_inline',
    'codegen_inline_byname',

    #'compound',

    'declarations',
)

class UnknownMethod(Exception):
    def __init__(self, obj, method):
        Exception.__init__(self)
        self.obj = obj
        self.method = method
    def __str__(self):
        return 'Unknown method in %s : %s' % (repr(self.obj), self.method)

gensym_counter = 0
def gensym(prefix = '_gensym'):
    global gensym_counter
    gensym_counter += 1
    return prefix + str(gensym_counter)

# The stack of loops
loop_stack = []

# Keep track of which methods are referenced, thus needing generated code.
referenced_methods = {}
method_queue = []
exported_methods = {}

# Saved variables in methods, method->list of symbols
saved_method_variables = {}

class Failure(metaclass=ABCMeta):
    '''Handle exceptions failure handling is supposed to handle the various kind of
    functions that are generated, with different ways of signaling
    success/failure.'''
    allowed = True
    fail_stack = []
    def __init__(self, site):
        self.site = site
    def __enter__(self):
        self.fail_stack.append(self)
    def __exit__(self, exc_type, exc_val, exc_tb):
        top = self.fail_stack.pop()
        assert top is self
    @abstractmethod
    def fail(self, site):
        '''Return code that is used to handle an exception'''

class NoFailure(Failure):
    '''Disallow exceptions to be thrown in compile time'''
    allowed = False
    def fail(self, site):
        raise ICE(site, "fail not allowed here")

class InitFailure(Failure):
    '''Returns NULL from init_object on exception'''
    def fail(self, site):
        return mkReturn(site, mkIntegerLiteral(site, 0))

class LogFailure(Failure):
    '''Log exceptions as errors, without aborting execution'''
    def __init__(self, site, node, indices):
        super(LogFailure, self).__init__(site)
        self.node = node
        self.indices = indices

    def fail(self, site):
        return log_statement(site, self.node, self.indices, "error",
                             mkIntegerLiteral(site, 1), None,
                             "Uncaught DML exception")

class ReturnFailure(Failure):
    '''Generate boolean return statements to signal success. False
    means success.'''
    def fail(self, site):
        return mkReturn(site, mkBoolConstant(site, True))
    def nofail(self):
        '''Return code that is used to leave the method successfully'''
        return mkReturn(self.site, mkBoolConstant(self.site, False))

class CatchFailure(Failure):
    '''Handle exceptions by re-throwing them, which in C means jumping to
    a label.'''
    def __init__(self, site, method_node):
        Failure.__init__(self, site)
        self.label = None
        self.method = method_node
    def fail(self, site):
        if not self.label:
            self.label = gensym('throw')
        return mkThrow(site, self.label)

class ExitHandler(metaclass=ABCMeta):
    current = None

    def __enter__(self):
        self.prev = ExitHandler.current
        ExitHandler.current = self
    def __exit__(self, exc_type, exc_val, exc_tb):
        assert ExitHandler.current is self
        ExitHandler.current = self.prev

    @abstractmethod
    def codegen_exit(self, site, retvals):
        '''Return a statement for returning from the current method. retvals
        is None in DML 1.2, and a list of return values in DML 1.4.'''
        pass

def codegen_exit(site, retvals):
    return ExitHandler.current.codegen_exit(site, retvals)

class GotoExit(ExitHandler):
    count = 0
    def __init__(self):
        self.used = False
        GotoExit.count += 1
        self.label = 'exit%d' % (self.count,)

class GotoExit_dml12(GotoExit):
    def codegen_exit(self, site, retvals):
        assert retvals is None
        self.used = True
        return mkGoto(site, self.label)

class GotoExit_dml14(GotoExit):
    def __init__(self, outvars):
        self.outvars = outvars
        super(GotoExit_dml14, self).__init__()
    def codegen_exit(self, site, retvals):
        assert retvals is not None
        if len(retvals) != len(self.outvars):
            report(ERETARGS(site, len(self.outvars), len(retvals)))
            # avoid control flow errors by falling back to statement with
            # no fall-through
            return mkAssert(site, mkBoolConstant(site, False))
        self.used = True
        return mkCompound(
            site,
            [mkCopyData(site, val, out)
             for (out, val) in zip(self.outvars, retvals)]
            + [mkReturnFromInline(site, self.label)])

class ReturnExit(ExitHandler):
    def __init__(self, outp, throws):
        self.outp = outp
        self.throws = throws
    def codegen_exit(self, site, retvals):
        assert retvals is not None, 'dml 1.2/1.4 mixup'
        return codegen_return(site, self.outp, self.throws, retvals)

def declarations(scope):
    "Get all local declarations in a scope as a list of Declaration objects"
    decls = []
    for sym in scope.symbols():
        if sym.pseudo:
            # dbg("declarations(%s): skipping %r" % (scope.id, sym))
            continue
        if sym.stmt:
            continue
        decl = sym_declaration(sym)
        if decl:
            decls.append(decl)

    return decls


# Expression dispatch

expression_dispatcher = ast.astdispatcher('expr_')
codegen_expression_maybe_nonvalue = expression_dispatcher.dispatch
def codegen_expression(ast, location, scope):
    expr = codegen_expression_maybe_nonvalue(ast, location, scope)
    if isinstance(expr, NonValue):
        raise expr.exc()
    return expr

@expression_dispatcher
def expr_set(tree, location, scope):
    [target, source] = tree.args
    return mkAssignOp(tree.site,
                      codegen_expression(target, location, scope),
                      codegen_expression(source, location, scope))

@expression_dispatcher
def expr_conditional(tree, location, scope):
    [cond, texpr, fexpr] = tree.args
    cond = codegen_expression(cond, location, scope)
    if cond.constant and dml.globals.dml_version == (1, 2):
        # Constant propagate
        live_ast = texpr if cond.value else fexpr
        live_expr = codegen_expression_maybe_nonvalue(live_ast, location, scope)

        # Skip code generation for dead branch, but only in 1.2
        if (logging.show_porting
            and not tree.site.filename().endswith('dml-builtins.dml')):
            # If any branch contains an error or a non-value, then
            # it must be converted to '#?'.
            with logging.suppress_errors() as errors:
                dead_ast = fexpr if cond.value else texpr
                try:
                    codegen_expression(dead_ast, location, scope)
                except DMLError as e:
                    errors.append(e)
                if errors or isinstance(live_expr, NonValue):
                    report(PHASH(tree.site))
                    report(PHASHELSE(dmlparse.end_site(texpr.site), ':'))
        return live_expr
    return mkIfExpr(tree.site,
                    cond,
                    codegen_expression(texpr, location, scope),
                    codegen_expression(fexpr, location, scope))

@expression_dispatcher
def expr_hashcond(tree, location, scope):
    [cond, texpr, fexpr] = tree.args
    cond = as_bool(codegen_expression(cond, location, scope))
    if not cond.constant:
        raise ENCONST(tree.site, cond)
    live_ast = texpr if cond.value else fexpr
    return codegen_expression_maybe_nonvalue(live_ast, location, scope)

arith_binops = {
    '<':  mkLessThan,
    '<=': mkLessThanOrEquals,
    '>':  mkGreaterThan,
    '>=': mkGreaterThanOrEquals,
    '==': mkEquals,
    '!=': mkNotEquals,
    '&':  mkBitAnd,
    '|':  mkBitOr,
    '^':  mkBitXOr,
    '<<': mkShL,
    '>>': mkShR,
    '*':  mkMult,
    '/':  mkDiv,
    '%':  mkMod,
    '+':  mkAdd,
    '-':  mkSubtract,
    '&&': mkAnd,
    '||': mkOr,
}

@expression_dispatcher
def expr_binop(tree, location, scope):
    [lh, op, rh] = tree.args
    if op not in arith_binops:
        raise ICE(tree.site, 'Unknown binary operation: %s %s %s'
                  % (repr(lh), repr(op), repr(rh)))
    lh = codegen_expression(lh, location, scope)

    if op in ['&&', '||']:
        lh = as_bool(lh)
        if lh.constant and bool(lh.value) == (op == '||'):
            if tree.site.dml_version() == (1, 2):
                if logging.show_porting:
                    # if RH contains errors, we must convert it to #? #:
                    with logging.suppress_errors() as errors:
                        as_bool(codegen_expression(rh, location, scope))
                    if errors:
                        if op == '||':
                            report(PANDOR(tree.site, dmlparse.start_site(tree.site), dmlparse.end_site(tree.site), '||', '#? true #:', ''))
                        else:
                            report(PANDOR(tree.site, dmlparse.start_site(tree.site), dmlparse.end_site(tree.site), '&&', '#?', ' #: false'))
            else:
                as_bool(codegen_expression(rh, location, scope))
            return lh
        rh = as_bool(codegen_expression(rh, location, scope))
    else:
        rh = codegen_expression(rh, location, scope)
    return arith_binops[op](tree.site, lh, rh)

def codegen_sizeof(site, expr):
    fun = mkLit(site, 'sizeof',
                TFunction([], TNamed('size_t'),
                          varargs = True))
    return Apply(site, fun, [expr], fun.ctype())

def flatten(x):
    '''Recursively flatten lists and tuples'''
    return ([item for y in x for item in flatten(y)]
            if isinstance(x, (list, tuple)) and not isinstance(x, ast.AST)
            else [x])

def subast_has_dollar(expr_ast):
    if expr_ast.kind == 'objectref':
        return True
    else:
        return any(subast_has_dollar(sub) for sub in flatten(expr_ast.args)
                   if isinstance(sub, ast.AST))

@expression_dispatcher
def expr_unop(tree, location, scope):
    [op, rh_ast] = tree.args
    if (dml.globals.compat_dml12
        and op == 'sizeof' and rh_ast.kind == 'variable_dml12'):
        var = rh_ast.args[0]
        if var in typedefs and scope.lookup(var) is None:
            report(WSIZEOFTYPE(tree.site))
            return codegen_sizeof(
                tree.site, mkLit(tree.site, cident(var), None))
    rh = codegen_expression_maybe_nonvalue(rh_ast, location, scope)

    if isinstance(rh, NonValue):
        if op == 'defined':
            if undefined(rh):
                return mkBoolConstant(tree.site, False)
            if isinstance(rh, (NodeRef, NodeArrayRef, AbstractList)):
                return mkBoolConstant(tree.site, True)
        if op == '!' and isinstance(rh, InterfaceMethodRef):
            # see bug 24144
            return mkNot(tree.site, mkMethodPresent(tree.site, rh))
        raise rh.exc()
    if   op == '!':
        if dml.globals.compat_dml12 and dml.globals.api_version <= "5":
            t = rh.ctype()
            if isinstance(safe_realtype(t), TInt) and subast_has_dollar(rh_ast):
                # A previous bug caused DMLC to permit expressions on
                # the form '!$reg'. This pattern was fairly common;
                # this hack is an attempt to reduce the short-term
                # need to update existing models. See also bug 24248.
                if logging.show_porting:
                    # triggers PBITNEQ
                    as_bool(rh)
                return mkEquals(tree.site, rh, mkIntegerLiteral(tree.site, 0))
        return mkNot(tree.site, as_bool(rh))
    elif op == '~':  return mkBitNot(tree.site, rh)
    elif op == '-':  return mkUnaryMinus(tree.site, rh)
    elif op == '+':  return mkUnaryPlus(tree.site, rh)
    elif op == '&':  return mkAddressOf(tree.site, rh)
    elif op == '*':  return mkDereference(tree.site, rh)
    elif op == '++':  return mkPreInc(tree.site, rh)
    elif op == '--':  return mkPreDec(tree.site, rh)
    elif op == 'post++':  return mkPostInc(tree.site, rh)
    elif op == 'post--':  return mkPostDec(tree.site, rh)
    elif op == 'sizeof':
        if not dml.globals.compat_dml12 and not isinstance(rh, ctree.LValue):
            raise ERVAL(rh.site, 'sizeof')
        return codegen_sizeof(tree.site, rh)
    elif op == 'defined': return mkBoolConstant(tree.site, True)
    elif op == 'stringify':
        if not rh.constant:
            raise ENCONST(rh, rh)
        return mkStringConstant(tree.site, str(rh))
    else:
        raise Exception('Unknown unary operation: %s %s'
                        % (repr(op), repr(rh)))

@expression_dispatcher
def expr_typeop(tree, location, scope):
    [t] = tree.args
    (struct_defs, t) = eval_type(t, tree.site, location, scope)
    for (site, _) in struct_defs:
        report(EANONSTRUCT(site, "'sizeoftype' expression"))
    return codegen_sizeof(tree.site, mkLit(tree.site, t.declaration(''), None))

@expression_dispatcher
def expr_new(tree, location, scope):
    [t, count] = tree.args
    (struct_defs, t) = eval_type(t, tree.site, location, scope)
    for (site, _) in struct_defs:
        report(EANONSTRUCT(site, "'new' expression"))
    if count:
        count = codegen_expression(count, location, scope)
    return mkNew(tree.site, t, count)

@expression_dispatcher
def expr_apply(tree, location, scope):
    [fun, args] = tree.args
    fun = codegen_expression_maybe_nonvalue(fun, location, scope)
    args = [codegen_expression(arg, location, scope) for arg in args]
    # will report errors for non-callable non-values
    return fun.apply(args)

@expression_dispatcher
def expr_variable_dml12(tree, location, scope):
    [name] = tree.args
    e = lookup_var(tree.site, scope, name)
    if e is None:
        raise EIDENT(tree.site, name)
    return e

@expression_dispatcher
def expr_variable(tree, location, scope):
    [name] = tree.args
    e = lookup_var(tree.site, scope, name)
    if scope.lookup(name) is global_scope.lookup(name) and location:
        # Hack: Object hierarchy is shoehorned between global scope and any
        # local scope
        # TODO: we should move symbols from global scope into device
        # scope instead. And location + scope args should be unified.
        in_dev_tree = ctree.lookup_component(
            tree.site, location.node, location.indices, name, False)
        if in_dev_tree:
            e = in_dev_tree
    if e is None:
        raise EIDENT(tree.site, name)
    return e

@expression_dispatcher
def expr_objectref(tree, location, scope):
    [name] = tree.args
    if not location:
        # This happens when invoked from mkglobals
        raise ENCONST(tree.site, dollar(tree.site)+name)
    e = ctree.lookup_component(
        tree.site, location.node, location.indices, name, False)
    if not e:
        raise EREF(tree.site, name)
    assert dml.globals.dml_version == (1, 2)
    if logging.show_porting:
        if (scope.lookup(name)
            and scope.lookup(name) != global_scope.lookup(name)):
            this = location.node
            if this.objtype == 'method':
                this = this.parent
            node = this
            while not node.get_component(name):
                node = node.parent
                assert node
            if not node.parent:
                prefix = 'dev.'
            elif node is this:
                prefix = 'this.'
            elif node.objtype == 'bank' and not scope.lookup('bank'):
                prefix = 'bank.'
            else:
                prefix = 'dev.%s.' % (node.logname(
                        tuple(e.read() for e in location.indices)),)
            if not tree.site.filename().endswith('dml-builtins.dml'):
                report(PDOLLAR_QUALIFY(
                    dmlparse.start_site(tree.site), '', prefix))
    return e

def try_resolve_len(site, lh):
    if isinstance(lh, NonValue):
        if isinstance(lh, AbstractList):
            return mkIntegerConstant(site,
                                     len(tuple(lh.iter_flat())), False)
        elif isinstance(lh, NodeArrayRef):
            return mkIntegerConstant(site,
                                     lh.node.dimsizes[len(lh.indices)],
                                     False)
    elif isinstance(lh, EachIn):
        return mkSequenceLength(site, lh)
    return None

@expression_dispatcher
def expr_member(tree, location, scope):
    [lh, op, rh] = tree.args
    lh = codegen_expression_maybe_nonvalue(lh, location, scope)
    if not tree.site.dml_version() == (1, 2) and op == '.' and rh == 'len':
        member = try_resolve_len(tree.site, lh)
        if member:
            return member

    if isinstance(lh, NonValue) and not isinstance(lh, NodeRef):
        raise lh.exc()

    return mkSubRef(tree.site, lh, rh, op)

@expression_dispatcher
def expr_string(tree, location, scope):
    [val] = tree.args
    return mkStringConstant(tree.site, val)

@expression_dispatcher
def expr_int(tree, location, scope):
    [val] = tree.args
    return mkIntegerLiteral(tree.site, val)

@expression_dispatcher
def expr_float(tree, location, scope):
    [val] = tree.args
    return mkFloatConstant(tree.site, val)

@expression_dispatcher
def expr_index(tree, location, scope):
    [expr, index, bitorder] = tree.args
    expr = codegen_expression_maybe_nonvalue(expr, location, scope)
    index = codegen_expression_maybe_nonvalue(index, location, scope)
    return mkIndex(tree.site, expr, index)

@expression_dispatcher
def expr_slice(tree, location, scope):
    [expr, msb, lsb, bitorder] = tree.args
    expr = codegen_expression(expr, location, scope)
    msb = codegen_expression(msb, location, scope)
    if lsb is not None:
        lsb = codegen_expression(lsb, location, scope)
    return mkBitSlice(tree.site, expr, msb, lsb, bitorder)

@expression_dispatcher
def expr_list(tree, location, scope):
    [elts] = tree.args
    values = []
    for elt in elts:
        e = codegen_expression_maybe_nonvalue(elt, location, scope)
        if e.constant or isinstance(e, (NodeRef, AbstractList, NodeArrayRef,
                                        SessionVariableRef)):
            values.append(e)
        elif isinstance(e, NonValue):
            raise e.exc()
        else:
            raise ECLST(e)
    return mkList(tree.site, values)

@expression_dispatcher
def expr_cast(tree, location, scope):
    [expr_ast, casttype] = tree.args
    expr = codegen_expression_maybe_nonvalue(expr_ast, location, scope)
    (struct_defs, type) = eval_type(casttype, tree.site, location, scope)
    for (site, _) in struct_defs:
        report(EANONSTRUCT(site, "'cast' expression"))

    if (dml.globals.compat_dml12 and dml.globals.api_version <= "6"
        and isinstance(expr, InterfaceMethodRef)):
        # Workaround for bug 24144
        return mkLit(tree.site, "%s->%s" % (
            expr.node_expr.read(), expr.method_name), type)

    if isinstance(expr, NonValue) and (
            not isinstance(expr, NodeRef)
            or not isinstance(safe_realtype(type), (TTrait, TObjIdentity))):
        raise expr.exc()
    else:
        return mkCast(tree.site, expr, type)

@expression_dispatcher
def expr_undefined(tree, location, scope):
    return mkUndefined(tree.site)

percent_matcher = re.compile("%")

fmt_matcher = re.compile(r"""
    %
    (?P<flags>      [-#0 +'I]*)
    (?P<width>      [1-9][0-9]*|\*|)
    (?P<precision>  \.([0-9]+|\*)|)
    (?P<length>     (h|H|ll?|L|q|j|Z|z|R|L|P|B(8|16|32|64)*|))
    (?P<conversion> [boudipxXscaAeEfgG%])
    """, re.X)

# Make the printf directives match argument sizes and inline some constant
def fix_printf(fmt, args, argsites, site):
    filtered_fmt = ""
    filtered_args = []
    argi = 0

    last_end = 0
    while True:
        m = percent_matcher.search(fmt, last_end)
        if not m:
            filtered_fmt += fmt[last_end:]
            break

        start = m.start()
        m = fmt_matcher.match(fmt, start)
        if not m:
            raise EFORMAT(site, start+1)

        filtered_fmt += fmt[last_end:m.start()]
        last_end = m.end()

        flags      = m.group('flags')
        width      = m.group('width')
        precision  = m.group('precision')
        length     = m.group('length')
        conversion = m.group('conversion')

        if conversion == '%':
            # printf allows flags and stuff here, but ignores it, but
            # let's copy it just in case.
            filtered_fmt += ("%" + flags + width + precision + length
                             + conversion)
            continue

        if argi == len(args):
            raise EFMTARGN(site)

        if width == '*':
            filtered_args.append(mkCast(args[argi].site,
                                        ctree.as_int(args[argi]),
                                        TInt(32, True)))
            argi += 1

        if precision == '.*':
            filtered_args.append(ctree.as_int(args[argi]))
            argi += 1

        arg = args[argi]
        if conversion in "boudixX":
            # GCC emits warnings about ll vs l mismatches, even
            # though ll and l are both 64-bit
            # integers. Unfortunately, DMLC does not know the
            # difference between 'long' and 'long long'; uint64 is
            # long long while e.g. size_t is long on linux64. For
            # purposes of logging, it is a sufficient workaround
            # to unconditionally cast to long long.
            length = "ll"
            arg = mkCast(arg.site, as_int(args[argi]), TInt(64, False))

        elif conversion in "p":
            argtype = safe_realtype(arg.ctype())

            if not isinstance(argtype, TPtr):
                raise EFMTARGT(argsites[argi], arg,
                                argi+1, "pointer")

        elif conversion == 's':
            argtype = arg.ctype()
            if isinstance(arg, (QName, HiddenName, HiddenQName)):
                qfmt, qargs = arg.fmt()
                filtered_fmt += qfmt
                assumed_type = TInt(32, False)
                for qarg in qargs:
                    filtered_args.append(mkCast(qarg.site, qarg, assumed_type))
                argi += 1
                continue
            elif isinstance(argtype, TNamed) and argtype.c == 'strbuf_t':
                arg = mkApply(site,
                              mkLit(site, 'sb_str',
                                    TFunction([TPtr(argtype)],
                                              TPtr(TNamed('char',
                                                          const=True)))),
                              [mkAddressOf(site, arg)])

        filtered_fmt += "%" + flags + width + precision + length + conversion
        filtered_args.append(arg)
        argi += 1

    if argi < len(args):
        raise EFMTARGN(site)

    return filtered_fmt, filtered_args

def eval_type(asttype, site, location, scope, extern=False, typename=None,
              allow_void=False):
    '''Interpret a type AST.
    The return value is a pair (struct_defs, type), where type is the DMLType
    instance, and struct_defs is a list of StructType statements required by
    C to interpret a declaration that uses the type.
    'extern' is true inside 'extern typedef' declarations.
    'typename' is used as hint for a good struct label, e.g.
    typedef struct { ... } foo_t; gives the label 'foo_t' which allows
    nicer error messages'''
    assert location is None or isinstance(location, Location)

    assert asttype

    struct_defs = []
    etype = None
    if isinstance(asttype[0], tuple):
        tag, info = asttype[0]
        if tag == 'struct':
            members = []
            for (_, msite, name, type_ast) in info:
                (member_struct_defs, member_type) = eval_type(
                    type_ast, msite, location, scope, extern)
                # TODO This can be removed once nested deserialization of
                # identity is properly supported
                if isinstance(member_type, TObjIdentity):
                    raise ICE(msite, ('_identity_t is not allowed as part of '
                                      + 'struct or array'))
                members.append((name, member_type))
                struct_defs.extend(member_struct_defs)
            if extern:
                id = typename or TExternStruct.unique_id()
                etype = TExternStruct(members, id, typename=typename)
            elif members:
                etype = TStruct(members, label=typename)
                struct_defs.append((site, etype))
            else:
                if site.dml_version() == (1, 2):
                    etype = TVoid()
                else:
                    raise EEMPTYSTRUCT(site)
        elif tag == 'layout':
            if extern:
                raise ELAYOUT(site, "extern layout not permitted,"
                              + " use 'struct { }' instead")
            endian, fields = info
            members = []
            for (_, msite, name, type_ast) in fields:
                (member_struct_defs, member_type) = eval_type(
                    type_ast, msite, location, scope, False)
                members.append((msite, name, member_type))
                struct_defs.extend(member_struct_defs)
            if not members:
                raise EEMPTYSTRUCT(site)
            etype = TLayout(endian, members, label=typename)
            struct_defs.append((site, etype))
        elif tag == 'bitfields':
            width, fields = info
            if width > 64:
                raise EBFLD(site, "bitfields width is > 64")
            members = []
            for ((_, fsite, name, t), astmsb, astlsb) in fields:
                msb = expr_intval(codegen_expression(astmsb, location, scope))

                lsb = expr_intval(codegen_expression(astlsb, location, scope))

                (member_struct_defs, mtype) = eval_type(
                    t, site, location, scope, extern)
                # guaranteed by parser
                assert not member_struct_defs
                if not mtype.is_int:
                    raise EBFLD(fsite, "non-integer field")
                if mtype.bits != msb - lsb + 1:
                    raise EBFLD(fsite, "field %s has wrong size" % name)

                members.append((name, mtype, msb, lsb))
            etype = TInt(width, False, members)
        elif tag == 'typeof':
            expr = codegen_expression_maybe_nonvalue(info, location, scope)
            if (not dml.globals.compat_dml12
                and not isinstance(expr, ctree.LValue)
                # for compatibility with dml-builtins, using 1.2
                and not isinstance(expr, ctree.RegisterWithFields)):
                raise ERVAL(expr.site, 'typeof')
            if isinstance(expr, NonValue):
                if isinstance(expr, (ctree.NoallocNodeRef,
                                     ctree.RegisterWithFields,
                                     ctree.IncompleteNodeRefWithStorage)):
                    etype = expr.node_type
                else:
                    raise expr.exc()
            else:
                etype = expr.ctype()
            if not etype:
                raise ICE(site, "No type for expression: %s (%r)"
                           % (expr, expr))
        elif tag == 'sequence':
            etype = TTraitList(info)
        else:
            raise ICE(site, "Strange type")
    elif isinstance(asttype[0], str):
        etype = parse_type(asttype[0])
        if (isinstance(etype, TObjIdentity)
            and os.path.basename(site.filename()) != 'dml-builtins.dml'):
            report(WEXPERIMENTAL(site, '_identity_t'))
    else:
        raise ICE(site, "Stranger type")

    etype.declaration_site = site

    asttype = asttype[1:]
    while asttype:
        if asttype[0] == 'const':
            etype.const = True
            asttype = asttype[1:]
        elif asttype[0] == 'pointer':
            if (etype.is_int
                and not etype.is_endian
                and etype.bits not in (8, 16, 32, 64)):
                raise EINTPTRTYPE(site, TPtr(etype))
            etype = TPtr(etype)
            asttype = asttype[1:]
        elif asttype[0] == 'vect':
            if etype.void:
                raise EVOID(site)
            etype = TVector(etype)
            asttype = asttype[1:]
        elif asttype[0] == 'array':
            if etype.void:
                raise EVOID(site)
            # TODO This can be removed once nested deserialization of
            # identity is properly supported
            elif isinstance(etype, TObjIdentity):
                raise ICE(site, ('_identity_t is not allowed as part of '
                                 + 'struct or array'))
            alen = codegen_expression(asttype[1], location, scope)
            etype = TArray(etype, as_int(alen))
            asttype = asttype[2:]
        elif asttype[0] == 'funcall':
            if struct_defs:
                (site, _) = struct_defs[0]
                raise EANONSTRUCT(site, "function return type")

            arg_struct_defs = []
            inarg_asts = asttype[1]
            if inarg_asts and inarg_asts[-1] == '...':
                varargs = True
                inarg_asts = inarg_asts[:-1]
            else:
                varargs = False
            inargs = []
            for (_, tsite, name, type_ast) in inarg_asts:
                (arg_struct_defs, argt) = eval_type(
                    type_ast, tsite, location, scope, arg_struct_defs, extern,
                    allow_void=True)
                if arg_struct_defs:
                    (site, _) = arg_struct_defs[0]
                    raise EANONSTRUCT(site, "function argument type")
                if argt.void:
                    if len(inarg_asts) == 1 and not name:
                        # C compatibility
                        continue
                    else:
                        raise EVOID(tsite)
                inargs.append(argt)

            # Function parameters that are declared as arrays are
            # interpreted as pointers
            for i, arg in enumerate(inargs):
                if isinstance(arg, TArray):
                    # C99 has a syntax for specifying that the array
                    # should be converted to a const pointer, but DML
                    # doesn't support that syntax.
                    inargs[i] = TPtr(arg.base, False)
                if arg.is_int and arg.is_endian:
                    raise EEARG(site)

            if etype.is_int and etype.is_endian:
                raise EEARG(site)
            etype = TFunction(inargs, etype, varargs)
            asttype = asttype[2:]
        else:
            raise ICE(site, "weird type info: " + repr(asttype))

        etype.declaration_site = site

    if etype.void and not allow_void:
        raise EVOID(site)

    return (struct_defs, etype)

def eval_method_inp(inp_asts, location, scope):
    '''evaluate the inarg ASTs of a method declaration'''
    inp = []
    for (_, tsite, argname, type_ast) in inp_asts:
        if type_ast:
            (struct_defs, t) = eval_type(type_ast, tsite, location, scope)
            for (site, _) in struct_defs:
                report(EANONSTRUCT(site, "method argument"))
        else:
            t = None
        inp.append((argname, t))
    return inp

def eval_method_outp(outp_asts, location, scope):
    '''evaluate the outarg ASTs of a method declaration'''
    if not outp_asts:
        return []
    outp = []
    if outp_asts[0].site.dml_version() == (1, 2):
        for (_, tsite, argname, type_ast) in outp_asts:
            if type_ast:
                (struct_defs, t) = eval_type(type_ast, tsite, location, scope)
                for (site, _) in struct_defs:
                    report(EANONSTRUCT(site, "method out argument"))
            else:
                t = None
            outp.append((argname, t))
    else:
        for (i, (_, tsite, _, type_ast)) in enumerate(outp_asts):
            assert type_ast
            (struct_defs, t) = eval_type(type_ast, tsite, location, scope)
            for (site, _) in struct_defs:
                report(EANONSTRUCT(site, "method return type"))
            # In 1.4, output arguments are not user-visible, but _outN is used
            # by the generated C function if needed.
            outp.append(('_out%d' % (i,), t))
    return outp

class SimpleEvents(object):
    # Basically a dictionary, but items sorts on the value
    # (function name) to make it predictable
    def __init__(self):
        self.__dict = {}
    def items(self):
        return sorted(iter(list(self.__dict.items())), key = operator.itemgetter(1))
    def add(self, method):
        "Create an event object that calls a method"
        fun = self.__dict.get(method, None)
        if not fun:
            if method.dimensions > 0 or len(method.inp) > 0:
                fun = tuple('_simple_event%s_%d' % (i, len(self.__dict))
                            for i in ('', '_destroy', '_get_value',
                                      '_set_value'))
            else:
                fun = ('_simple_event_%d' % len(self.__dict),
                       'NULL', 'NULL', 'NULL')
            self.__dict[method] = fun

        return fun

simple_events = SimpleEvents()

def eval_initializer(site, etype, astinit, location, scope, static):
    """Deconstruct an AST for an initializer, and return a
       corresponding initializer object. Report EDATAINIT errors upon
       invalid initializers.
       
       Initializers are required to be constant for data objects and
       static variables. Local variables can be initialized with
       non-constant expressions. However, initializers for local
       variables of struct or bitfield types, in the form of
       brace-enclosed lists, are required to be constant expressions
       member-wise. Also, variable length arrays cannot have
       initializers. These rules match closely with the C language,
       except that array size must be explicitly specified and the
       number of initializers must match the number of elements of
       compound data types."""
    def do_eval(etype, astinit, const):
        if not isinstance(astinit, list):
            expr = codegen_expression(astinit, location, scope)
            if const and not expr.constant:
                raise EDATAINIT(astinit.site, 'non-constant expression')
            return ExpressionInitializer(
                source_for_assignment(astinit.site, etype, expr))
        if isinstance(etype, TArray):
            assert isinstance(etype.size, Expression)
            if etype.size.constant:
                alen = etype.size.value
            else:
                raise EDATAINIT(site, 'variable length array')
            if alen != len(astinit):
                raise EDATAINIT(site, 'mismatched array size')
            init = tuple([do_eval(etype.base, e, const) for e in astinit])
            return CompoundInitializer(site, init)
        elif isinstance(etype, TStruct):
            if len(etype.members) != len(astinit):
                raise EDATAINIT(site, 'mismatched number of fields')
            init = tuple([do_eval(m[1], e, True)
                          for m, e in zip(etype.members, astinit)])
            return CompoundInitializer(site, init)
        elif isinstance(etype, TExternStruct):
            if len(etype.members) != len(astinit):
                raise EDATAINIT(site, 'mismatched number of fields')
            init = {m[0]: do_eval(m[1], e, True)
                    for m, e in zip(etype.members, astinit)}
            return DesignatedStructInitializer(site, init)
        elif etype.is_int and etype.is_bitfields:
            if len(etype.members) != len(astinit):
                raise EDATAINIT(site, 'mismatched number of fields')
            val = 0
            for ((_, t, msb, lsb), e) in zip(etype.members, astinit):
                if isinstance(e, list):
                    raise EDATAINIT(e.site,
                        'scalar required for bitfield initializer')
                expr = codegen_expression(e, location, scope)
                if not isinstance(expr, IntegerConstant):
                    raise EDATAINIT(e.site, 'integer constant required')
                if (msb - lsb + 1) < expr.value.bit_length():
                    raise EASTYPE(e.site, t, expr)
                val |= expr.value << lsb
            return ExpressionInitializer(mkIntegerConstant(site, val, val < 0))
        elif isinstance(etype, TNamed):
            return do_eval(safe_realtype(etype), astinit, const)
        else:
            raise EDATAINIT(site,
                'compound initializer not supported for type %s' % etype)
    return do_eval(etype, astinit, static)

def get_initializer(site, etype, astinit, location, scope):
    """Return an expression to use as initializer for a variable.
    The 'init' is the ast for an initializer given in the source, or None.
    This also checks for and invalid 'etype'."""
    # Check that the type is defined
    try:
        typ = realtype(etype)
    except DMLUnknownType:
        raise ETYPE(site, etype)

    if astinit:
        try:
            return eval_initializer(
                site, etype, astinit, location, scope, False)
        except DMLError as e:
            report(e)
    # This isn't really part of the spec for DML 1.0 and 1.2, but to
    # avoid C compiler warnings it's best to do it anyway.
    if typ.is_int:
        if typ.is_endian:
            return MemsetInitializer(site)
        else:
            return ExpressionInitializer(mkIntegerLiteral(site, 0))
    elif isinstance(typ, TBool):
        return ExpressionInitializer(mkBoolConstant(site, False))
    elif typ.is_float:
        return ExpressionInitializer(mkFloatConstant(site, 0.0))
    elif isinstance(typ, (TStruct, TExternStruct, TArray, TTrait, TObjIdentity)):
        return MemsetInitializer(site)
    elif isinstance(typ, TPtr):
        return ExpressionInitializer(mkLit(site, 'NULL', typ))
    elif isinstance(typ, TVector):
        return ExpressionInitializer(mkLit(site, 'VNULL', typ))
    elif isinstance(typ, TFunction):
        raise EVARTYPE(site, etype.describe())
    elif isinstance(typ, TTraitList):
        return ExpressionInitializer(mkLit(site, '{NULL, 0, 0, 0, 0}', typ))
    raise ICE(site, "No initializer for %r" % (etype,))

statement_dispatcher = ast.astdispatcher('stmt_')

def codegen_statements(trees, *args):
    stmts = []
    for tree in trees:
        try:
            stmts.extend(statement_dispatcher.dispatch(tree, *args))
        except DMLError as e:
            report(e)
    return stmts

def codegen_statement(tree, *args):
    return mkCompound(tree.site, codegen_statements([tree], *args))

@statement_dispatcher
def stmt_compound(stmt, location, scope):
    [stmt_asts] = stmt.args
    lscope = Symtab(scope)
    statements = codegen_statements(stmt_asts, location, lscope)
    return [mkCompound(stmt.site, declarations(lscope) + statements)]

def check_shadowing(scope, name, site):
    if (dml.globals.dml_version == (1, 2)
        and isinstance(scope.parent, MethodParamScope)):
        if scope.parent.lookup(name, local = True):
            report(WDEPRECATED(site,
                    'Variable %s in top-level method scope shadows parameter'
                    % name))

    sym = scope.lookup(name, local = True)
    if sym:
        raise EDVAR(site, sym.site, name)

def check_varname(site, name):
    if name in {'char', 'double', 'float', 'int', 'long', 'short',
                'signed', 'unsigned', 'void', 'register'}:
        report(ESYNTAX(site, name, 'type name used as variable name'))

@statement_dispatcher
def stmt_local(stmt, location, scope):
    # This doesn't occur in DML 1.0
    [name, asttype, init] = stmt.args
    if dml.globals.dml_version == (1, 2) and not dml.globals.compat_dml12:
        check_varname(stmt.site, name)
    (struct_decls, etype) = eval_type(
        asttype, stmt.site, location, scope)
    etype = etype.resolve()
    init = get_initializer(stmt.site, etype, init, location, scope)

    check_shadowing(scope, name, stmt.site)

    sym = scope.add_variable(name, type = etype,
                             site = stmt.site, init = init,
                             static = False,
                             stmt = True,
                             make_unique=not dml.globals.debuggable)

    return ([mkStructDefinition(site, t) for (site, t) in struct_decls]
            + [sym_declaration(sym)])

@statement_dispatcher
def stmt_session(stmt, location, scope):
    [name, asttype, init] = stmt.args

    if location.method() is None:
        # Removing this error would make 'session' compile fine in
        # traits, but it would not work as expected: different
        # instances of one trait would share the same variable
        # instance.  TODO: We should either forbid session explicitly
        # (replacing the ICE with a proper error message), or decide
        # and implement some sensible semantics for it.
        raise ICE(stmt.site, "'session' declaration inside a trait is not"
                  + " yet allowed")
    elif (not dml.globals.dml_version == (1, 2)
          and not location.method().fully_typed):
        raise ESTOREDINLINE(stmt.site, 'session')

    (struct_decls, etype) = eval_type(asttype, stmt.site, location,
                                      global_scope)
    etype = etype.resolve()
    TStruct.late_global_struct_defs.extend(struct_decls)
    if init:
        try:
            init = eval_initializer(
                stmt.site, etype, init, location, global_scope, True)
        except DMLError as e:
            report(e)
            init = None
    check_shadowing(scope, name, stmt.site)

    # generate a nested array of variables, indexed into by
    # the dimensions of the method dimensions
    static_sym_type = etype
    for dimsize in reversed(location.method().dimsizes):
        static_sym_type = TArray(static_sym_type,
                                 mkIntegerConstant(stmt.site, dimsize, False))
        # initializer in methods cannot currently depend on indices
        # so we can replicate the same initializer for all
        # slots in the array.
        # TODO: it should be possible to support
        # index-dependent initialization now, though
        if init is not None:
            init = CompoundInitializer(stmt.site, [init] * dimsize)
    static_sym_name = dml.globals.device.get_unique_static_name(name)
    static_sym = StaticSymbol(static_sym_name, static_sym_name,
                              static_sym_type, stmt.site, init, stmt)
    static_var_expr = mkStaticVariable(stmt.site, static_sym)
    for idx in location.indices:
        static_var_expr = mkIndex(stmt.site, static_var_expr, idx)
    local_sym = ExpressionSymbol(name, static_var_expr, stmt.site)

    scope.add(local_sym)
    dml.globals.device.add_static_var(static_sym)

    return []

@statement_dispatcher
def stmt_saved_statement(stmt, location, scope):
    [name, asttype, init] = stmt.args

    # guaranteed by parser
    assert dml.globals.dml_version != (1, 2)

    if location.method() is None:
        # Removing this error would make 'saved' compile fine in
        # traits, but it would not work as expected: different
        # instances of one trait would share the same variable
        # instance.  TODO: We should either forbid session/saved explicitly
        # (replacing the ICE with a proper error message), or decide
        # and implement some sensible semantics for it.
        raise ICE(stmt.site, "'saved' declaration inside a shared method is not"
                  + " yet allowed")
    elif not location.method().fully_typed:
        raise ESTOREDINLINE(stmt.site, 'saved')

    (struct_decls, etype) = eval_type(asttype, stmt.site, location, scope)
    TStruct.late_global_struct_defs.extend(struct_decls)
    etype.resolve()
    if init:
        try:
            init = eval_initializer(
                stmt.site, etype, init, location, scope, True)
        except DMLError as e:
            report(e)
            init = None
    check_shadowing(scope, name, stmt.site)

    # acquire better name
    node = location.node
    cname = name
    while node.objtype != "device":
        cname = node.name + "_" + cname
        node = node.parent
    
    # generate a nested array of variables, indexed into by
    # the dimensions of the method dimensions
    static_sym_type = etype
    for dimsize in reversed(location.method().dimsizes):
        static_sym_type = TArray(static_sym_type,
                                 mkIntegerConstant(stmt.site, dimsize, False))
        # initializer in methods cannot currently depend on indices
        # so we can replicate the same initializer for all
        # slots in the array.
        # TODO: it should be possible to support
        # index-dependent initialization now, though
        if init is not None:
            init = CompoundInitializer(stmt.site, [init] * dimsize)
    static_sym_name = dml.globals.device.get_unique_static_name(name)
    static_sym = StaticSymbol(static_sym_name, static_sym_name,
                              static_sym_type, stmt.site, init, stmt)
    static_var_expr = mkStaticVariable(stmt.site, static_sym)
    for idx in location.indices:
        static_var_expr = mkIndex(stmt.site, static_var_expr, idx)
    local_sym = ExpressionSymbol(name, static_var_expr, stmt.site)
    scope.add(local_sym)

    dml.globals.device.add_static_var(static_sym)
    saved_method_variables.setdefault(location.method(), []).append(
        (static_sym, name))
    return []

@statement_dispatcher
def stmt_null(stmt, location, scope):
    return []

@statement_dispatcher
def stmt_if(stmt, location, scope):
    [cond_ast, truebranch, falsebranch] = stmt.args
    cond = as_bool(codegen_expression(cond_ast, location, scope))
    if cond.constant and stmt.site.dml_version() == (1, 2):
        if (logging.show_porting
            and not stmt.site.filename().endswith('dml-builtins.dml')):
            # If the dead branch contains an error, then it must be
            # converted to '#if'.
            with logging.suppress_errors() as errors:
                if not cond.value:
                    codegen_statement(truebranch, location, scope)
                elif falsebranch:
                    codegen_statement(falsebranch, location, scope)
            if errors:
                report(PHASH(stmt.site))
                if falsebranch:
                    report(PHASHELSE(dmlparse.end_site(truebranch.site),
                                     'else'))
            if (not falsebranch and cond_ast.kind == 'binop'
                and cond_ast.args[1] == '&&'):
                lh = as_bool(codegen_expression(cond_ast.args[0], location, scope))
                with logging.suppress_errors() as errors:
                    as_bool(codegen_expression(cond_ast.args[2], location, scope))
                if lh.constant and not lh.value and errors:
                    report(PIFAND(cond_ast.site, stmt.site, dmlparse.end_site(truebranch.site)))
        if cond.value:
            return codegen_statements([truebranch], location, scope)
        elif falsebranch:
            return codegen_statements([falsebranch], location, scope)
        else:
            return []
    else:
        # print 'IF', 'NONCONST', cond
        tbranch = codegen_statement(truebranch, location, scope)
        if falsebranch:
            fbranch = codegen_statement(falsebranch, location, scope)
        else:
            fbranch = None
        return [ctree.If(stmt.site, cond, tbranch, fbranch)]

@statement_dispatcher
def stmt_hashif(stmt, location, scope):
    [cond_ast, truebranch, falsebranch] = stmt.args
    cond = as_bool(codegen_expression(cond_ast, location, scope))
    if not cond.constant:
        raise ENCONST(cond_ast.site, cond)
    if cond.value:
        return [codegen_statement(truebranch, location, scope)]
    elif falsebranch:
        return [codegen_statement(falsebranch, location, scope)]
    else:
        return []

def try_codegen_invocation(site, apply_ast, outarg_asts, location, scope):
    '''Generate a method call statement if apply_ast is a method call,
    otherwise None'''
    if isinstance(apply_ast, list):
        # compound initializer
        return None
    if apply_ast.kind != 'apply':
        return None
    # possibly a method invocation
    (meth_ast, inarg_asts) = apply_ast.args
    meth_expr = codegen_expression_maybe_nonvalue(meth_ast, location, scope)
    if (isinstance(meth_expr, NonValue)
        and not isinstance(meth_expr, (
            TraitMethodRef, NodeRef, InterfaceMethodRef))):
        raise meth_expr.exc()
    if isinstance(meth_expr, TraitMethodRef):
        if not meth_expr.throws and len(meth_expr.outp) <= 1:
            # let the caller represent the method invocation as an
            # expression instead
            return None
        if (dml.globals.dml_version == (1, 2)
            and not in_try_block(location) and meth_expr.throws):
            # Shared methods marked as 'throws' count as
            # unconditionally throwing
            EBADFAIL_dml12.throwing_methods[location.method()] = site
        outargs = [
            codegen_expression(outarg_ast, location, scope)
            for outarg_ast in outarg_asts]
        inargs = [
            codegen_expression(inarg_ast, location, scope)
            for inarg_ast in inarg_asts]
        return codegen_call_traitmethod(site, meth_expr, inargs, outargs)
    elif not isinstance(meth_expr, NodeRef):
        return None
    # indeed a method invocation
    (meth_node, indices) = meth_expr.get_ref()
    if meth_node.objtype != 'method':
        return None
    if (meth_node.fully_typed and not meth_node.throws
        and len(meth_node.outp) <= 1):
        # let the caller represent the method invocation as an
        # expression instead
        return None
    outargs = [
        codegen_expression(outarg_ast, location, scope)
        for outarg_ast in outarg_asts]
    if dml.globals.dml_version == (1, 2):
        # some methods in the 1.2 lib (e.g. register.read_access) require
        # args to be undefined, so we must permit this when calling
        # the default implementation
        inargs = [
            codegen_expression_maybe_nonvalue(inarg_ast, location, scope)
            for inarg_ast in inarg_asts]
        for arg in inargs:
            if isinstance(arg, NonValue) and not undefined(arg):
                raise arg.exc()

        if (site.dml_version() == (1, 2)
            and not in_try_block(location)
            and meth_node.throws):
            mark_method_invocation(site, meth_node, location)
        if (site.dml_version() == (1, 4)
            and not in_try_block(location)
            and not location.method().throws
            and meth_node.site.dml_version() == (1, 2)
            and meth_node.throws):
            if dml12_method_throws_in_dml14(meth_node):
                report(EBADFAIL_dml12(site, [(meth_node.site, meth_node)], []))
            EBADFAIL_dml12.protected_calls.setdefault(
                meth_node, []).append((site, location.method()))
            f = CatchFailure(scope, location.method())
            with f:
                call = (codegen_call(site, meth_node, indices,
                                    inargs, outargs)
                        if meth_node.fully_typed
                        else common_inline(site, meth_node, indices,
                                           inargs, outargs))
            return mkTryCatch(site, f.label, call,
                              mkAssert(site, mkBoolConstant(site, False)))

    else:
        inargs = [
            codegen_expression(inarg_ast, location, scope)
            for inarg_ast in inarg_asts]
    if meth_node.fully_typed:
        return codegen_call(site, meth_node, indices,
                            inargs, outargs)
    else:
        return common_inline(site, meth_node, indices,
                              inargs, outargs)

@statement_dispatcher
def stmt_assign(stmt, location, scope):
    (kind, site, tgt_asts, src_ast) = stmt
    # tgt_asts is a list of lists of expressions; [[a, b], [c, d]]
    # corresponds to "(a, b) = (c, d) = "
    method_invocation = try_codegen_invocation(site, src_ast, tgt_asts[0],
                                               location, scope)
    if method_invocation:
        if len(tgt_asts) != 1:
            report(ESYNTAX(
                tgt_asts[1][0].site, '=',
                'assignment chain not allowed as method invocation target'))
        return [method_invocation]

    for tgt_list in tgt_asts:
        if len(tgt_list) != 1:
            if not isinstance(src_ast, list) and src_ast.kind == 'apply':
                report(ERETLVALS(site, 1, len(tgt_list)))
            else:
                report(ESYNTAX(
                    tgt_list[0].site, '(',
                    'only method calls can have multiple assignment targets'))
            return []

    tgts = [codegen_expression(tgt_ast, location, scope)
            for [tgt_ast] in tgt_asts]

    init = eval_initializer(
        site, tgts[-1].ctype(), src_ast, location, scope, False)

    lscope = Symtab(scope)
    stmts = []
    for (i, tgt) in enumerate(reversed(tgts[1:])):
        name = 'tmp%d' % (i,)
        sym = lscope.add_variable(
            name, type=tgt.ctype(), site=tgt.site, init=init, static=False,
            stmt=True)
        init = ExpressionInitializer(mkLocalVariable(tgt.site, sym))
        stmts.extend([sym_declaration(sym),
                      mkAssignStatement(tgt.site, tgt, init)])
    stmts.append(mkAssignStatement(tgts[0].site, tgts[0], init))

    return stmts

@statement_dispatcher
def stmt_assignop(stmt, location, scope):
    (kind, site, tgt_ast, op, src_ast) = stmt

    tgt = codegen_expression(tgt_ast, location, scope)
    if isinstance(tgt, ctree.BitSlice):
        # destructive hack
        return stmt_assign(
            ast.assign(site, [[tgt_ast]],
                       ast.binop(site, tgt_ast, op[:-1], src_ast)),
            location, scope)
    src = codegen_expression(src_ast, location, scope)
    ttype = tgt.ctype()
    lscope = Symtab(scope)
    sym = lscope.add_variable(
        'tmp', type = TPtr(ttype), site = tgt.site,
        init = ExpressionInitializer(mkAddressOf(tgt.site, tgt)),
        static = False, stmt = True)
    # Side-Effect Free representation of the tgt lvalue
    tgt_sef = mkDereference(site, mkLocalVariable(tgt.site, sym))
    return [
        sym_declaration(sym), mkExpressionStatement(
        site,
            mkAssignOp(site, tgt_sef, arith_binops[op[:-1]](
                site, tgt_sef, src)))]

@statement_dispatcher
def stmt_expression(stmt, location, scope):
    [expr] = stmt.args
    # a method invocation with no return value looks like an
    # expression statement to the grammar
    invocation = try_codegen_invocation(stmt.site, expr, [], location, scope)
    if invocation:
        return [invocation]
    return [mkExpressionStatement(stmt.site,
                                  codegen_expression(expr, location, scope))]

@statement_dispatcher
def stmt_throw(stmt, location, scope):
    handler = Failure.fail_stack[-1]
    if not handler.allowed:
        raise EBADFAIL(stmt.site)
    if dml.globals.dml_version == (1, 2) and not in_try_block(location):
        EBADFAIL_dml12.throwing_methods[location.method()] = stmt.site
    return [handler.fail(stmt.site)]

@statement_dispatcher
def stmt_error(stmt, location, scope):
    [msg] = stmt.args
    raise EERRSTMT(stmt.site, "forced compilation error in source code"
                   if msg is None else msg)

@statement_dispatcher
def stmt_warning(stmt, location, scope):
    [msg] = stmt.args
    report(WWRNSTMT(stmt.site, msg))
    return []

@statement_dispatcher
def stmt_return_dml12(stmt, location, scope):
    if logging.show_porting:
        m = location.method()
        if m and m.outp:
            report(PRETURNARGS(stmt.site, [name for (name, _) in m.outp]))
    [args] = stmt.args
    assert not args # ensured by parser
    return [codegen_exit(stmt.site, None)]

@statement_dispatcher
def stmt_return(stmt, location, scope):
    [args] = stmt.args
    return [codegen_exit(
        stmt.site, [codegen_expression(arg, location, scope)
                    for arg in args])]

@statement_dispatcher
def stmt_assert(stmt, location, scope):
    [expr] = stmt.args
    expr = codegen_expression(expr, location, scope)
    return [mkAssert(stmt.site, as_bool(expr))]
@statement_dispatcher
def stmt_goto(stmt, location, scope):
    [label] = stmt.args
    if not dml.globals.compat_dml12:
        report(ESYNTAX(stmt.site, 'goto', 'goto statement not allowed'))
    return [mkGoto(stmt.site, label)]

@statement_dispatcher
def stmt_label(stmt, location, scope):
    [label, statement] = stmt.args
    return [mkLabel(stmt.site, label),
            codegen_statement(statement, location, scope)]
@statement_dispatcher
def stmt_case_dml12(stmt, location, scope):
    [expr_ast, statement] = stmt.args
    expr = codegen_expression(expr_ast, location, scope)
    return [mkCase(stmt.site, expr),
            codegen_statement(statement, location, scope)]

@statement_dispatcher
def stmt_default_dml12(stmt, location, scope):
    [statement] = stmt.args
    return [mkDefault(stmt.site), codegen_statement(statement, location, scope)]

@statement_dispatcher
def stmt_case(stmt, location, scope):
    [expr_ast] = stmt.args
    expr = codegen_expression(expr_ast, location, scope)
    return [mkCase(stmt.site, expr)]

@statement_dispatcher
def stmt_default(stmt, location, scope):
    assert not stmt.args
    return [mkDefault(stmt.site)]

@statement_dispatcher
def stmt_delete(stmt, location, scope):
    [expr] = stmt.args
    expr = codegen_expression(expr, location, scope)
    return [mkDelete(stmt.site, expr)]

log_index = 0
@statement_dispatcher
def stmt_log(stmt, location, scope):
    [logkind, level, later_level, groups, fmt, args] = stmt.args
    argsites = [arg.site for arg in args]
    args = [ codegen_expression(arg, location, scope)
             for arg in args ]

    site = stmt.site

    level = ctree.as_int(codegen_expression(level, location, scope))
    if level.constant and not (1 <= level.value <= 4):
        report(ELLEV(level.site, 4))
        level = mkIntegerLiteral(site, 1)

    if later_level is not None:
        later_level = ctree.as_int(codegen_expression(
            later_level, location, scope))
        if (later_level.constant and level.constant and
            later_level.value == level.value):
            report(WREDUNDANTLEVEL(site))
        if later_level.constant and not (1 <= later_level.value <= 5):
            report(ELLEV(later_level.site, 5))
            later_level = mkIntegerLiteral(site, 4)
        global log_index
        table_ptr = TPtr(TNamed("ht_int_table_t"))
        table = mkLit(site, '&(_dev->_subsequent_log_ht)', table_ptr)
        # Acquire a key based on obj or trait identity
        if location.method():
            identity = ObjIdentity(site, location.node.parent, location.indices)
        else:
            identity = TraitObjIdentity(site, lookup_var(site, scope, "this"))
        key = mkApply(site,
                      mkLit(site, "_identity_to_key",
                            TFunction([TObjIdentity()], TInt(64, False))),
                      [identity])

        once_lookup = mkLit(
            site, "_select_log_level",
            TFunction([table_ptr, TInt(64, False), TInt(64, False),
                       TInt(64, False), TInt(64, False)],
                      TInt(64, False)))
        level_expr = mkApply(site, once_lookup,
                             [table, key, mkIntegerLiteral(site, log_index),
                              level, later_level])
        log_index += 1
        pre_statements = [mkDeclaration(site, "_calculated_level",
                                        TInt(64, False),
                                        ExpressionInitializer(level_expr))]
        level = mkLocalVariable(site, LocalSymbol("_calculated_level",
                                                  "_calculated_level",
                                                  TInt(64, False)))

    else:
        pre_statements = []
    fmt, args = fix_printf(fmt, args, argsites, site)
    return [mkCompound(site, pre_statements + [
        log_statement(site, location.node, location.indices,
                      logkind, level,
                      codegen_expression(groups, location, scope),
                      fmt, *args)])]

@statement_dispatcher
def stmt_try(stmt, location, scope):
    [tryblock, excblock] = stmt.args

    f = CatchFailure(scope, location.method())
    with f:
        tryblock = codegen_statement(tryblock, location, scope)
    if dml.globals.dml_version == (1, 2) and not f.label:
        return [tryblock]
    excblock = codegen_statement(excblock, location, scope)
    return [mkTryCatch(stmt.site, f.label, tryblock, excblock)]

@statement_dispatcher
def stmt_after(stmt, location, scope):
    [unit, delay, callexpr] = stmt.args
    site = stmt.site

    if callexpr[0] == 'apply':
        method = callexpr[2]
        inargs = callexpr[3]
    else:
        assert dml.globals.dml_version == (1, 2), repr(callexpr)
        method = callexpr
        inargs = []

    delay = codegen_expression(delay, location, scope)
    old_delay_type = delay.ctype()
    if unit == 's':
        api_unit = 'time'
        unit_type = TFloat('double')
    elif unit == 'cycles':
        api_unit = 'cycle'
        unit_type = TInt(64, True)
    else:
        raise ICE(self.site, f"Unsupported unit of time: '{unit}'")

    try:
        delay = source_for_assignment(site, unit_type, delay)
    except EASTYPE:
        raise EBTYPE(site, old_delay_type, unit_type)

    if unit == 'cycles' and not safe_realtype(old_delay_type).is_int:
        report(WTTYPEC(site, old_delay_type, unit_type, unit))

    method = codegen_expression_maybe_nonvalue(method, location, scope)

    if not isinstance(method, NodeRef):
        raise ENMETH(site, method)

    method, indices = method.get_ref()

    if method.objtype != 'method':
        raise ENMETH(site, method)

    if len(method.outp) > 0:
        raise EAFTER(site, method, None)

    require_fully_typed(site, method)
    func = method_instance(method)

    inargs = [codegen_expression(inarg, location, scope)
              for inarg in inargs]

    typecheck_inargs(site, inargs, func.inp, 'method')

    # After-call is only possible for methods with serializable parameters
    unserializable = []
    for (pname, ptype) in func.inp:
        try:
            serialize.map_dmltype_to_attrtype(site, ptype)
        except ESERIALIZE:
            unserializable.append((pname, ptype))

    if len(unserializable) > 0:
        raise EAFTER(site, method, unserializable)

    mark_method_referenced(func)
    eventfun = simple_events.add(method)

    return [mkAfter(site, api_unit, delay, method, eventfun, indices, inargs)]

@statement_dispatcher
def stmt_select(stmt, location, scope):
    [itername, lst, cond_ast, stmt_ast, else_ast] = stmt.args
    # dbg('SELNODE %r, %r, %r' % (location.node, location.indices, lst))
    lst = codegen_expression_maybe_nonvalue(lst, location, scope)
    # dbg('SELECT %s in %r' % (itername, lst))
    if isinstance(lst, NonValue):
        if isinstance(lst, AbstractList):
            l = lst.iter_flat()
            scope = Symtab(scope)
            else_dead = False
            # list of (cond, body)
            clauses = []
            for it in l:
                condscope = Symtab(scope)
                condscope.add(ExpressionSymbol(itername, it, stmt.site))
                cond = as_bool(codegen_expression(
                    cond_ast, location, condscope))
                if cond.constant and not cond.value:
                    continue
                clauses.append((
                    cond, codegen_statement(stmt_ast, location, condscope)))
                if cond.constant and cond.value:
                    else_dead = True
                    break

            if else_dead:
                (last_cond, last_stmt) = clauses.pop(-1)
                assert last_cond.constant and last_cond.value
                if_chain = last_stmt
            else:
                if_chain = codegen_statement(else_ast, location, scope)
            for (cond, stmt) in reversed(clauses):
                if_chain = mkIf(cond.site, cond, stmt, if_chain)
            return [if_chain]
        raise lst.exc()
    elif dml.globals.compat_dml12 and isinstance(lst.ctype(), TVector):
        itervar = lookup_var(stmt.site, scope, itername)
        if not itervar:
            raise EIDENT(stmt.site, itername)
        return [mkVectorForeach(stmt.site,
                                lst, itervar,
                                codegen_statement(stmt_ast, location, scope))]
    else:
        raise ENLST(stmt.site, lst)

def foreach_each_in(site, itername, trait, each_in,
                    body_ast, location, scope):
    scope = Symtab(scope)
    each_in_sym = scope.add_variable(
        '_each_in_expr', type=TTraitList(trait.name),
        init=ExpressionInitializer(each_in), stmt = True)
    ident = each_in_sym.value
    inner_scope = Symtab(scope)
    trait_type = TTrait(trait)
    trait_ptr = (f'(struct _{cident(trait.name)} *) '
                 + '(_list.base + _inner_idx * _list.offset)')
    obj_ref = '(_identity_t) { .id = _list.id, .encoded_index = _inner_idx}'
    inner_scope.add_variable(
        itername, type=trait_type,
        init=ExpressionInitializer(
            mkLit(site,
                  ('((%s) {%s, %s})' % (trait_type.declaration(''),
                                        trait_ptr, obj_ref)),
                  trait_type
                  )))
    inner_body = mkCompound(site, declarations(inner_scope)
        + codegen_statements([body_ast], location, inner_scope))
    loop = mkFor(
        site,
        [mkLit(site, 'int _outer_idx = %s.starti' % (ident,), TVoid())],
        mkLit(site, '_outer_idx < %s.endi' % (ident,), TBool()),
        [mkExpressionStatement(
            site, mkLit(site, '++_outer_idx', TInt(32, True)))],
        mkCompound(
            site,
            [mkInline(site,
                      '_vtable_list_t _list = %s.base[_outer_idx];' % (ident,)),
             mkInline(site, 'uint64 _num = _list.num / %s.array_size;' % (ident,)),
             mkInline(site, 'uint64 _start = _num * %s.array_idx;' % (ident,)),
             mkFor(site,
                   [mkLit(site, 'uint64 _inner_idx = _start', TVoid())],
                   mkLit(site, '_inner_idx < _start + _num', TBool()),
                   [mkExpressionStatement(
                       site, mkLit(site, '++_inner_idx', TVoid()))],
                   inner_body)]))

    return [mkCompound(site, [sym_declaration(each_in_sym), loop])]

@expression_dispatcher
def expr_each_in(ast, location, scope):
    (traitname, node_ast) = ast.args
    node_expr = codegen_expression_maybe_nonvalue(node_ast, location, scope)
    if not isinstance(node_expr, NodeRef):
        raise ENOBJ(node_expr.site, node_expr)
    (node, indices) = node_expr.get_ref()
    trait = dml.globals.traits.get(traitname)
    if trait is None:
        raise ENTMPL(ast.site, traitname)
    return mkEachIn(ast.site, trait, node, indices)

@statement_dispatcher
def stmt_foreach_dml12(stmt, location, scope):
    [itername, lst, statement] = stmt.args
    lst = codegen_expression_maybe_nonvalue(lst, location, scope)
    if isinstance(lst, NonValue):
        if not isinstance(lst, AbstractList):
            raise lst.exc()
        return foreach_constant_list(stmt.site, itername, lst,
                                     statement, location, scope)

    list_type = safe_realtype(lst.ctype())
    if isinstance(list_type, TVector):
        itervar = lookup_var(stmt.site, scope, itername)
        if not itervar:
            raise EIDENT(lst, itername)
        loop_stack.append('c')
        try:
            res = mkVectorForeach(stmt.site, lst, itervar,
                                  codegen_statement(statement, location, scope))
        finally:
            loop_stack.pop()
        return [res]
    else:
        raise ENLST(stmt.site, lst)

@statement_dispatcher
def stmt_foreach(stmt, location, scope):
    [itername, lst, statement] = stmt.args
    lst = codegen_expression(lst, location, scope)
    list_type = safe_realtype(lst.ctype())
    if isinstance(list_type, TTraitList):
        return foreach_each_in(
            stmt.site, itername,
            # .traitname was validated by safe_realtype()
            dml.globals.traits[list_type.traitname],
            lst, statement, location, scope)
    else:
        raise ENLST(stmt.site, lst)

@statement_dispatcher
def stmt_hashforeach(stmt, location, scope):
    [itername, lst, statement] = stmt.args
    lst = codegen_expression_maybe_nonvalue(lst, location, scope)
    if isinstance(lst, NonValue):
        if not isinstance(lst, AbstractList):
            raise lst.exc()
        return foreach_constant_list(stmt.site, itername, lst,
                                     statement, location, scope)
    elif not lst.constant:
        raise ENCONST(stmt.site, lst)
    else:
        raise ENLST(stmt.site, lst)

def foreach_constant_list(site, itername, lst, statement, location, scope):
    assert isinstance(lst, AbstractList)
    spec = []
    try:
        loop_stack.append('unroll')
        for items in lst.iter():
            loopvars = tuple(mkLit(site, '_ai%d_%d' % (len(loop_stack), dim),
                                   TInt(32, True))
                             for dim in range(len(items.dimsizes)))
            loopscope = Symtab(scope)
            loopscope.add(ExpressionSymbol(
                itername, items.expr(loopvars), site))
            stmt = codegen_statement(statement, location, loopscope)

            if isinstance(stmt, Null):
                continue

            decls = []
            for dim in reversed(range(len(items.dimsizes))):
                decls.append(mkDeclaration(site, loopvars[dim].str,
                                           TInt(32, True)))
                stmt = mkFor(
                    site,
                    [mkAssignOp(site,
                                loopvars[dim],
                                mkIntegerLiteral(site, 0))],
                    mkLessThan(site, loopvars[dim],
                               mkIntegerLiteral(site,
                                                items.dimsizes[dim])),
                    [mkInline(site, '++%s;' % (loopvars[dim].str,))],
                    stmt)
            spec.append(mkCompound(site, decls + [stmt]))

        return spec
    finally:
        loop_stack.pop()

@statement_dispatcher
def stmt_while(stmt, location, scope):
    [cond, statement] = stmt.args
    cond = as_bool(codegen_expression(cond, location, scope))
    if stmt.site.dml_version() == (1, 2) and cond.constant and not cond.value:
        return [mkNull(stmt.site)]
    else:
        loop_stack.append('c')
        try:
            res = mkWhile(stmt.site, cond,
                          codegen_statement(statement, location, scope))
        finally:
            loop_stack.pop()
        return [res]

@statement_dispatcher
def stmt_for(stmt, location, scope):
    [pres, cond, posts, statement] = stmt.args
    pres = [codegen_expression(pre, location, scope)
            for pre in pres]
    if cond is None:
        cond = mkBoolConstant(stmt.site, 1)
    else:
        cond = as_bool(codegen_expression(cond, location, scope))
    posts = codegen_statements(posts, location, scope)
    loop_stack.append('c')
    try:
        res = mkFor(stmt.site, pres, cond, posts,
                    codegen_statement(statement, location, scope))
    finally:
        loop_stack.pop()
    return [res]

@statement_dispatcher
def stmt_dowhile(stmt, location, scope):
    [cond, statement] = stmt.args
    cond = as_bool(codegen_expression(cond, location, scope))
    loop_stack.append('c')
    try:
        res = mkDoWhile(stmt.site, cond,
                        codegen_statement(statement, location, scope))
    finally:
        loop_stack.pop()
    return [res]

@statement_dispatcher
def stmt_switch(stmt, location, scope):
    [expr, body_ast] = stmt.args
    expr = codegen_expression(expr, location, scope)
    loop_stack.append('c')
    if stmt.site.dml_version() != (1, 2):
        assert body_ast.kind == 'compound'
        [stmt_asts] = body_ast.args
        stmts = codegen_statements(stmt_asts, location, scope)
        if (not stmts
            or not isinstance(stmts[0], (ctree.Case, ctree.Default))):
            raise ESWITCH(
                body_ast.site, "statement before first case label")
        defaults = [i for (i, sub) in enumerate(stmts)
                    if isinstance(sub, ctree.Default)]
        if len(defaults) > 1:
            raise ESWITCH(stmts[defaults[1]].site, "duplicate default label")
        if defaults:
            for sub in stmts[defaults[0]:]:
                if isinstance(sub, ctree.Case):
                    raise ESWITCH(sub.site,
                                  "case label after default label")
        body = ctree.Compound(body_ast.site, stmts)
    else:
        body = codegen_statement(body_ast, location, scope)
    try:
        res = mkSwitch(stmt.site, expr, body)
    finally:
        loop_stack.pop()
    return [res]

@statement_dispatcher
def stmt_continue(stmt, location, scope):
    if not loop_stack or loop_stack[-1] == 'method':
        raise ECONT(stmt.site)
    elif loop_stack[-1] == 'c':
        return [mkContinue(stmt.site)]
    else:
        raise ECONTU(stmt.site)

@statement_dispatcher
def stmt_break(stmt, location, scope):
    if not loop_stack or loop_stack[-1] == 'method':
        raise EBREAK(stmt.site)
    elif loop_stack[-1] == 'c':
        return [mkBreak(stmt.site)]
    else:
        raise EBREAKU(stmt.site)

def eval_call_stmt(method_ast, location, scope):
    '''Given a call (or inline) AST node, deconstruct it and eval the
    method reference and all parameters.'''
    expr = codegen_expression_maybe_nonvalue(method_ast, location, scope)
    if isinstance(expr, NonValue) and not isinstance(
            expr, (TraitMethodRef, NodeRef)):
        raise expr.exc()
    return expr

def verify_args(site, inp, outp, inargs, outargs):
    '''Verify that the given arguments can be used when calling or
    inlining method'''
    if len(inargs) != len(inp):
        raise EARG(site, 'input')
    if len(outargs) != len(outp):
        if dml.globals.dml_version == (1, 2):
            raise EARG(site, 'output')
        else:
            raise ERETLVALS(site, len(outp), len(outargs))
    for arg in outargs:
        if not arg.writable:
            report(EASSIGN(site, arg))
            return False
    return True

def mkcall_method(site, fun, indices):
    for i in indices:
        if isinstance(i, NonValue):
            raise i.exc()
    return lambda args: mkApply(
        site, fun,
        [mkLit(site, '_dev', TDevice(crep.structtype(dml.globals.device)))]
        + list(indices) + args)

def common_inline(site, method, indices, inargs, outargs):
    if not verify_args(site, method.inp, method.outp, inargs, outargs):
        return mkNull(site)

    if dml.globals.debuggable:
        if method.fully_typed and (not dml.globals.compat_dml12
                                   or all(not arg.constant for arg in inargs)):
            # call method instead of inlining it
            func = method_instance(method)
        else:
            # create a specialized method instance based on parameter
            # types, and call that
            intypes = tuple(arg if arg.constant or undefined(arg)
                            else methfunc_param(ptype, arg)
                            for ((pname, ptype), arg)
                            in zip(method.inp, inargs))
            outtypes = tuple(methfunc_param(ptype, arg)
                             for ((pname, ptype), arg)
                             in zip(method.outp, outargs))
            func = untyped_method_instance(method, (intypes, outtypes))
        mark_method_referenced(func)

        # Filter out inlined arguments
        used_args = [i for (i, (n, t)) in enumerate(func.inp)
                     if isinstance(t, DMLType)]
        inargs = [inargs[i] for i in used_args]
        inp = [func.inp[i] for i in used_args]

        return codegen_call_stmt(site, func.method.name,
                                 mkcall_method(site,
                                               func.cfunc_expr(site),
                                               indices),
                                 inp, func.outp, func.throws, inargs, outargs)

    loop_stack.append('method')
    try:
        res = codegen_inline(site, method, indices, inargs, outargs)
    finally:
        loop_stack.pop()
    return res

def in_try_block(location):
    '''Return whether we are currently protected by a try block
    within the currently dispatched method.'''

    handler = Failure.fail_stack[-1]
    return (isinstance(handler, CatchFailure)
            # Note when inlining a method, the fail handler can be a
            # CatchFailure from the calling method; we detect this by
            # comparing method nodes.  There are theoretical cases
            # where this comparison is insufficient; a method can
            # inline itself within a try block. But that never happens
            # in practice, and I don't even know if it can cause any
            # problems in theory.
            and handler.method == location.method())

def dml12_method_throws_in_dml14(meth_node):
    return (meth_node.parent.objtype, meth_node.name) in {
        ('attribute', 'set'),
        ('bank', 'read_access'),
        ('bank', 'write_access')}

def mark_method_invocation(call_site, method, location):
    '''Mark that a given method is called from a certain location.
    This is used in calls between 1.4->1.2: DML 1.4 requires in general that
    methods that don't throw are marked as nothrow, but most 1.2
    methods are not supposed to throw an exception, even if they have nothrow '''
    if method.site.dml_version() == (1, 2):
        if dml12_method_throws_in_dml14(method):
            # some methods will be converted to 'throws' when moving
            # to 1.4; these will eventually need encapsulation in try
            # blocks, so we count them as throwing.
            EBADFAIL_dml12.throwing_methods[location.method()] = call_site
        else:
            # ordinary 1.2 method: will count as throwing if it
            # actually throws, or (recursively) if it calls a method
            # that does. This analysis is done by EBADFAIL_dml12.all_errors().
            EBADFAIL_dml12.uncaught_method_calls.setdefault(
                method, []).append((call_site, location.method()))
    else:
        # 1.4 methods marked as 'throws' count as throwing even if they don't,
        # because they will need a try block
        EBADFAIL_dml12.throwing_methods[location.method()] = call_site

@statement_dispatcher
def stmt_inline(stmt, location, scope):
    (method_ast, inarg_asts, outarg_asts) = stmt.args
    assert dml.globals.dml_version == (1, 2)
    inargs = [codegen_expression_maybe_nonvalue(arg, location, scope)
              for arg in inarg_asts]
    for e in inargs:
        if isinstance(e, NonValue) and not undefined(e):
            raise e.exc()
    outargs = [codegen_expression(arg, location, scope)
               for arg in outarg_asts]
    expr = eval_call_stmt(method_ast, location, scope)
    if isinstance(expr, NodeRef):
        (method, indices) = expr.get_ref()
        if method.objtype != 'method':
            raise ENMETH(stmt.site, expr)
        if not in_try_block(location) and method.throws:
            mark_method_invocation(expr.site, method, location)
        return [common_inline(stmt.site, method, indices, inargs, outargs)]
    else:
        raise ENMETH(stmt.site, expr)

@statement_dispatcher
def stmt_call(stmt, location, scope):
    (method_ast, inarg_asts, outarg_asts) = stmt.args
    assert dml.globals.dml_version == (1, 2)
    inargs = [codegen_expression(arg, location, scope)
              for arg in inarg_asts]
    outargs = [codegen_expression(arg, location, scope)
               for arg in outarg_asts]
    expr = eval_call_stmt(method_ast, location, scope)
    if isinstance(expr, NodeRef):
        (method, indices) = expr.get_ref()
        if method.objtype != 'method':
            raise ENMETH(stmt.site, expr)
        if not in_try_block(location) and method.throws:
            mark_method_invocation(expr.site, method, location)
        return [codegen_call(stmt.site, method, indices, inargs, outargs)]
    elif isinstance(expr, TraitMethodRef):
        if not in_try_block(location) and expr.throws:
            # Shared methods marked as 'throws' count as
            # unconditionally throwing
            EBADFAIL_dml12.throwing_methods[location.method()] = expr.site
        return [codegen_call_traitmethod(stmt.site, expr, inargs, outargs)]
    else:
        raise ENMETH(stmt.site, expr)

# Context manager that protects from recursive inlining
class RecursiveInlineGuard(object):
    # method nodes for the currently applied methods, possibly nested
    stack = set()

    def __init__(self, site, meth_node):
        self.site = site
        self.meth_node = meth_node
    def __enter__(self):
        if self.meth_node in self.stack:
            raise ERECUR(self.site, self.meth_node)
        self.stack.add(self.meth_node)
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stack.remove(self.meth_node)

def codegen_inline_byname(node, indices, meth_name, inargs, outargs,
                          site,
                          inhibit_copyin = False):
    assert isinstance(node, objects.DMLObject)
    assert isinstance(indices, tuple)
    assert isinstance(meth_name, str)

    if meth_name:
        meth_node = node.get_component(meth_name, 'method')
        if not meth_node:
            raise UnknownMethod(site, meth_name)
    else:
        meth_node = node

    return codegen_inline(site, meth_node, indices,
                          inargs, outargs, inhibit_copyin)

intercepted_in_int_reg = {
    'get_name': int_register.codegen_get_name,
    'get_number': int_register.codegen_get_number,
    'read': int_register.codegen_read,
    'write': int_register.codegen_write
}
intercepted_in_bank = {
    '_read_one_reg': 'codegen_read_access',
    '_write_one_reg': 'codegen_write_access'
}

def intercepted_method(meth_node):
    if (meth_node.name in intercepted_in_int_reg
        and meth_node.parent.objtype == 'implement'
        and meth_node.parent.name == 'int_register'
        and meth_node.parent.parent.objtype == 'bank'):
        return intercepted_in_int_reg[meth_node.name]
    if (meth_node.name in intercepted_in_bank
        and meth_node.parent.objtype == 'bank'):
        from . import io_memory
        return getattr(io_memory, intercepted_in_bank[meth_node.name])
    return False

def codegen_inline(site, meth_node, indices, inargs, outargs,
                   inhibit_copyin = False):
    assert isinstance(meth_node, objects.DMLObject)
    PWUNUSED.inlined_methods.add(meth_node.site)

    if len(inargs) != len(meth_node.inp):
        raise ICE(meth_node, "wrong number of inargs")
    if len(outargs) != len(meth_node.outp):
        raise ICE(meth_node, "wrong number of outargs")

    if meth_node.throws and not Failure.fail_stack[-1].allowed:
        raise EBADFAIL(site)

    meth_node.refcount += 1        # regard method as used

    # Open the scope
    with contextlib.ExitStack() as contexts:
        contexts.enter_context(RecursiveInlineGuard(site, meth_node))
        contexts.enter_context(ErrorContext(meth_node, site))
        if not meth_node.throws:
            contexts.enter_context(NoFailure(site))
        param_scope = MethodParamScope(global_scope)
        param_scope.add(meth_node.default_method.default_sym(indices))

        if intercepted_method(meth_node):
            # Inlining an intercepted method would yield an
            # error. These methods are fully typed, so converting to a
            # call is safe.
            return codegen_call(site, meth_node, indices,
                                inargs, outargs)
        for (arg, (parmname, parmtype), argno) in zip(inargs, meth_node.inp,
                                                      list(range(len(inargs)))):
            # Create an alias
            if parmtype:
                if undefined(arg):
                    raise arg.exc()
                argtype  = arg.ctype()
                if not argtype:
                    raise ICE(arg.site, "unknown expression type")
                parmt = safe_realtype(parmtype)
                argt = safe_realtype(argtype)
                (ok, trunc, constviol) = parmt.canstore(argt)
                if not ok:
                    raise EARGT(site, 'inline', meth_node.name,
                                arg.ctype(), parmname, parmtype, 'input')

                if constviol:
                    raise ECONSTP(site, parmname, "method call")
                arg = coerce_if_eint(arg)

            if inhibit_copyin or undefined(arg):
                param_scope.add(ExpressionSymbol(parmname, arg, arg.site))
            elif arg.constant and (parmtype is None
                                   or dml.globals.compat_dml12):
                # Constants must be passed directly to
                # provide constant folding.  Other values are stored in a
                # local variable to improve type checking and variable
                # scoping.
                inlined_arg = mkInlinedParam(site, arg, parmname,
                                             parmtype or arg.ctype())
                param_scope.add(ExpressionSymbol(
                    parmname, inlined_arg, site))
            else:
                param_scope.add_variable(parmname,
                                         type = parmtype or arg.ctype(),
                                         site = meth_node.site,
                                         init = ExpressionInitializer(arg))
                arg.decref()

        if meth_node.astcode.site.dml_version() == (1, 2):
            if inhibit_copyin:
                # inhibit_copyin also inhibits copyout
                for (arg, (parmname, parmtype)) in zip(outargs, meth_node.outp):
                    param_scope.add(ExpressionSymbol(parmname, arg, site))
                copyout = []
            else:
                outvars = [
                    add_proxy_outvar(
                        meth_node.site, parmname,
                        parmtype if parmtype else arg.ctype(),
                        param_scope)
                    for (arg, (parmname, parmtype))
                    in zip(outargs, meth_node.outp)]
                copyout = [
                    copy_outarg(arg, var, parmname,
                                parmtype if parmtype else arg.ctype(),
                                meth_node.name)
                    for (arg, var, (parmname, parmtype)) in zip(
                            outargs, outvars, meth_node.outp)] 
            exit_handler = GotoExit_dml12()
            with exit_handler:
                code = [codegen_statement(meth_node.astcode,
                                          Location(meth_node, indices),
                                          param_scope)]
            if exit_handler.used:
                code.append(mkLabel(site, exit_handler.label))
            code.extend(copyout)
            body = mkCompound(site, declarations(param_scope) + code)
            return mkInlinedMethod(site, meth_node, body)
        else:
            assert meth_node.astcode.kind == 'compound'
            [subs] = meth_node.astcode.args
            location = Location(meth_node, indices)
            exit_handler = GotoExit_dml14(outargs)
            with exit_handler:
                code = codegen_statements(subs, location, param_scope)
            if exit_handler.used:
                code.append(mkLabel(site, exit_handler.label))
            body = mkCompound(site, declarations(param_scope) + code)
            if meth_node.outp and body.control_flow().fallthrough:
                report(ENORET(meth_node.astcode.site))
            return mkInlinedMethod(site, meth_node, body)

def c_rettype(outp, throws):
    if throws:
        return TBool()
    elif outp:
        (_, rettype) = outp[0]
        return rettype
    else:
        return TVoid()

def c_inargs(inp, outp, throws):
    '''Return the signature of the C function representing a DML method,
    on the form (outtype, [arg1, ...]), where each arg is a pair
    (name, type). inp includes any implicit arguments
    (device struct pointer, indices, etc)'''
    if throws:
        return inp + [(n, TPtr(t)) for (n, t) in outp]
    elif outp:
        return inp + [(n, TPtr(t)) for (n, t) in outp[1:]]
    else:
        return list(inp)

class MethodFunc(object):
    '''A concrete method instance, where all parameters are fully
    typed. One MethodFunc corresponds to one generated C function. A
    fully typed method will always yield a single MethodFunc. An
    incompletely typed method may generate multiple MethodFunc
    instances, one for each set of parameter types it is inlined
    with. When a method is inlined with a constant parameter, this
    will result in a separate MethodFunc instance with the constant
    parameter removed from the signature, and the constant propagated
    into the method body.'''
    __slots__ = ('method', 'inp', 'outp', 'throws',
                 'cparams', 'rettype', 'suffix')

    def __init__(self, method, inp, outp, throws, cparams, suffix):
        '''(inp, outp, throws) describe the method's signature; cparams
        describe the generated C function parameters corresponding to
        inp. If some method parameters are constant propagated, then
        the corresponding method parameter is on the form (name,
        value), instead of (name, type), and the corresponding C
        function parameter is omitted.'''

        self.method = method

        self.inp = tuple(inp)
        self.outp = tuple(outp)
        self.throws = throws
        self.suffix = suffix

        # rettype is the return type of the C function
        self.rettype = c_rettype(outp, throws)
        self.cparams = c_inargs(
            implicit_params(method) + list(cparams), outp, throws)

    @property
    def prototype(self):
        return self.rettype.declaration(
            "%s(%s)" % (self.get_cname(),
                        ", ".join([t.declaration(n)
                                   for (n, t) in self.cparams])))

    def cfunc_expr(self, site):
        return mkLit(site, self.get_cname(), self.cfunc_type)

    @property
    def cfunc_type(self):
        return TFunction([t for (_, t) in self.cparams], self.rettype)

    def get_name(self):
        '''textual description of method, used in comment'''
        name = self.method.logname()
        if self.suffix:
            name += " (specialized)"
        return name

    def get_cname(self):
        base = crep.cref(self.method)
        return '_DML_M_' + base + self.suffix

def canonicalize_signature(signature):
    "Make a signature hashable"
    # The problem is that the same type can be represented by
    # different DMLType objects. Use an ugly trick and canonicalize to
    # the string representation.
    (intypes, outtypes) = signature
    return (tuple(t.value if isinstance(t, Expression) and t.constant
                  else str(t) for t in intypes),
            tuple(str(t) for t in outtypes))

def implicit_params(method):
    structtype = TDevice(crep.structtype(dml.globals.device))
    return [("_dev", structtype)] + [('_idx%d' % i, TInt(32, False))
                                     for i in range(method.dimensions)]

def untyped_method_instance(method, signature):
    """Return the MethodFunc instance for a given signature"""
    canon_signature = canonicalize_signature(signature)
    # Idempotency; this can be called repeatedly for the same method.
    if canon_signature in method.funcs:
        return method.funcs[canon_signature]

    (intypes, outtypes) = signature
    inp = [(arg, stype)
           for stype, (arg, etype) in zip(intypes, method.inp)]
    assert all(isinstance(t, DMLType) for t in outtypes)
    outp = [(arg, stype)
            for stype, (arg, etype) in zip(outtypes, method.outp)]

    cparams = [(n, t) for (n, t) in inp if isinstance(t, DMLType)]

    func = MethodFunc(method, inp, outp, method.throws, cparams,
                      "__"+str(len(method.funcs)))

    method.funcs[canon_signature] = func
    return func

def method_instance(method):
    """Return the MethodFunc instance for a typed method"""
    assert method.fully_typed
    if None in method.funcs:
        return method.funcs[None]

    func = MethodFunc(method, method.inp, method.outp, method.throws,
                      method.inp, "")

    method.funcs[None] = func
    return func

def codegen_method_func(func):
    """Return the function body of the C function corresponding to a
    specific instance of a method defined directly in the device tree"""
    method = func.method

    indices = tuple(mkLit(method.site, '_idx%d' % i, TInt(32, False),
                          str=dollar(method.site) + "%s" % (idxvar,))
                    for (i, idxvar) in enumerate(method.parent.idxvars()))
    intercepted = intercepted_method(method)
    if intercepted:
        assert method.throws
        return intercepted(
            method.parent, indices,
            [mkLit(method.site, n, t) for (n, t) in func.inp],
            [mkLit(method.site, "*%s" % n, t) for (n, t) in func.outp],
            method.site)
    inline_scope = MethodParamScope(global_scope)
    for (name, e) in func.inp:
        if dml.globals.dml_version == (1, 2) and not dml.globals.compat_dml12:
            check_varname(method.site, name)
        if isinstance(e, Expression):
            inlined_arg = (
                mkInlinedParam(method.site, e, name, e.ctype())
                if defined(e) else e)
            inline_scope.add(ExpressionSymbol(name, inlined_arg, method.site))
    inp = [(n, t) for (n, t) in func.inp if isinstance(t, DMLType)]

    with ErrorContext(method):
        code = codegen_method(
            method.site, inp, func.outp, func.throws,
            method.astcode, method.default_method.default_sym(indices),
            Location(method, indices), inline_scope, method.rbrace_site)
    return code

def codegen_return(site, outp, throws, retvals):
    '''Generate code for returning from a method with a given list of
    return values'''
    if len(outp) != len(retvals):
        report(ERETARGS(site, len(outp), len(retvals)))
        # avoid control flow errors by falling back to statement with
        # no fall-through
        return mkAssert(site, mkBoolConstant(site, False))
    if throws:
        ret = mkReturn(site, mkBoolConstant(site, False))
    elif outp:
        (_, t) = outp[0]
        ret = mkReturn(site, retvals[0], t)
    else:
        ret = mkReturn(site, None)
    stmts = []
    return_first_outarg = bool(not throws and outp)
    for ((name, typ), val) in itertools.islice(
            zip(outp, retvals), return_first_outarg, None):
        if (return_first_outarg and site.dml_version() == (1, 2)):
            # Avoid outputting "*x = *x" for nothrow methods in 1.2
            assert isinstance(val, ctree.Dereference)
            assert isinstance(val.rh, ctree.Lit)
            assert val.rh.str == name
            continue
        stmts.append(mkCopyData(site, val, mkLit(site, "*%s" % (name,), typ)))
    stmts.append(ret)
    return mkCompound(site, stmts)

def codegen_method(site, inp, outp, throws, ast, default,
                   location, fnscope, rbrace_site):
    for (arg, etype) in inp:
        fnscope.add_variable(arg, type=etype, site=site, make_unique=False)
    initializers = [get_initializer(site, parmtype, None, None, None)
                    for (_, parmtype) in outp]

    fnscope.add(default)

    fail_handler = ReturnFailure(rbrace_site) if throws else NoFailure(site)

    if ast.site.dml_version() == (1, 2):
        if throws:
            # Declare and initialize one variable for each output
            # parameter.  We cannot write to the output parameters
            # directly, because they should be left untouched if an
            # exception is thrown.
            code = []
            for ((varname, parmtype), init) in zip(outp, initializers):
                sym = fnscope.add_variable(
                    varname, type=parmtype, init=init, make_unique=True)
                sym.incref()
                code.append(sym_declaration(sym))
        else:
            if outp:
                # pass first out argument as return value
                (name, typ) = outp[0]
                sym = fnscope.add_variable(name, typ, site=site,
                                           init=initializers[0],
                                           make_unique=False)
                sym.incref()
                code = [sym_declaration(sym)]
                for ((name, typ), init) in zip(outp[1:], initializers[1:]):
                    # remaining output arguments pass-by-pointer
                    param = mkDereference(site, mkLit(site, name, TPtr(typ)))
                    fnscope.add(ExpressionSymbol(name, param, site))
                    code.append(mkAssignStatement(site, param, init))
            else:
                code = []

        exit_handler = GotoExit_dml12()
        with fail_handler, exit_handler:
            code.append(codegen_statement(ast, location, fnscope))
        if exit_handler.used:
            code.append(mkLabel(site, exit_handler.label))
        code.append(codegen_return(site, outp, throws, [
            lookup_var(site, fnscope, varname) for (varname, _) in outp]))
        return mkCompound(site, code)
    else:
        exit_handler = ReturnExit(outp, throws)
        # manually deconstruct compound AST node, to make sure
        # top-level locals share scope with parameters
        assert ast.kind == 'compound'
        [subs] = ast.args
        with fail_handler, exit_handler:
            body = codegen_statements(subs, location, fnscope)
        code = mkCompound(site, body)
        if code.control_flow().fallthrough:
            if outp:
                report(ENORET(site))
            elif throws:
                return mkCompound(site, body + [mkReturn(
                    site, mkBoolConstant(site, False))])
        return code

# Keep track of methods that we need to generate code for
def mark_method_referenced(func):
    cnt = referenced_methods.get(func, 0)
    cnt += 1
    referenced_methods[func] = cnt
    func.method.refcount += 1
    if cnt == 1:
        method_queue.append(func)

def mark_method_exported(func, name, export_site):
    # name -> func instances -> export statement sites
    if name in exported_methods:
        (otherfunc, othersite) = exported_methods[name]
        report(ENAMECOLL(export_site, othersite, name))
    else:
        exported_methods[name] = (func, export_site)

def methfunc_param(ptype, arg):
    if ptype:
        return ptype
    # Special case, normally endian integer inargs or outargs are not allowed,
    # so we pretend its a regular integer here and count on coercion
    # to handle the translation
    realargtype = realtype(arg.ctype())
    if realargtype.is_int and realargtype.is_endian:
        return TInt(realargtype.bits, realargtype.signed,
                    realargtype.members, realargtype.const)
    return arg.ctype()

def require_fully_typed(site, meth_node):
    if not meth_node.fully_typed:
        for (parmname, parmtype) in meth_node.inp:
            if not parmtype:
                raise ENARGT(meth_node.site, parmname, 'input', site)
        for (parmname, parmtype) in meth_node.outp:
            if not parmtype:
                raise ENARGT(meth_node.site, parmname, 'output', site)
        raise ICE(site, "no missing parameter type")

def codegen_call_expr(site, meth_node, indices, args):
    require_fully_typed(site, meth_node)
    func = method_instance(meth_node)
    mark_method_referenced(func)
    typecheck_inargs(site, args, func.inp, 'method')
    return mkcall_method(site, func.cfunc_expr(site), indices)(args)

def codegen_call_traitmethod(site, expr, inargs, outargs):
    if not isinstance(expr, TraitMethodRef):
        raise ICE(site, "cannot call %r: not a trait method" % (expr,))
    if not verify_args(site, expr.inp, expr.outp, inargs, outargs):
        return mkNull(site)
    def mkcall(args):
        rettype = c_rettype(expr.outp, expr.throws)
        # implicitly convert endian int arguments to integers
        args = [coerce_if_eint(arg) for arg in args]
        return expr.call_expr(args, rettype)
    return codegen_call_stmt(site, str(expr), mkcall, expr.inp, expr.outp,
                             expr.throws, inargs, outargs)

def codegen_call(site, meth_node, indices, inargs, outargs):
    '''Generate a call using a direct reference to the method node'''
    if not verify_args(site, meth_node.inp, meth_node.outp, inargs, outargs):
        return mkNull(site)
    require_fully_typed(site, meth_node)
    func = method_instance(meth_node)

    if dml.globals.compat_dml12:
        # For backward compatibility. See bug 21367.
        inargs = [mkCast(site, arg, TPtr(TNamed('char')))
                  if isinstance(arg, StringConstant) else arg
                  for arg in inargs]

    mark_method_referenced(func)
    return codegen_call_stmt(site, func.method.name,
                             mkcall_method(site,
                                           func.cfunc_expr(site),
                                           indices),
                             func.inp, func.outp, func.throws, inargs, outargs)

def codegen_call_byname(site, node, indices, meth_name, inargs, outargs):
    '''Generate a call using the parent node and indices, plus the method
    name.  For convenience.'''
    assert isinstance(node, objects.DMLObject)
    assert isinstance(meth_name, str)

    meth_node = node.get_component(meth_name, 'method')
    if not meth_node:
        raise UnknownMethod(node, meth_name)
    return codegen_call(site, meth_node, indices, inargs, outargs)

def copy_outarg(arg, var, parmname, parmtype, method_name):
    '''Type-check the output argument 'arg', and create a local
    variable with that type in scope 'callscope'. The address of this
    variable will be passed as an output argument to the C function
    generated by the method.

    This is needed to protect the output parameter from being
    modified, in case a method clobbers the parameter and then throws
    an exception. We would be able to skip the proxy variable for
    calls to non-throwing methods when arg.ctype() and parmtype are
    equivalent types, but we don't do this today.'''
    argtype = arg.ctype()

    if not argtype:
        raise ICE(arg.site, "unknown expression type")
    else:
        ok, trunc, constviol = realtype(parmtype).canstore(realtype(argtype))
        if not ok:
            raise EARGT(arg.site, 'call', method_name,
                         arg.ctype(), parmname, parmtype, 'output')

    return mkCopyData(var.site, var, arg)

def add_proxy_outvar(site, parmname, parmtype, callscope):
    varname = parmname
    varinit = get_initializer(site, parmtype, None, None, None)
    sym = callscope.add_variable(varname, type=parmtype, init=varinit, site=site)
    return mkLocalVariable(site, sym)

def codegen_call_stmt(site, name, mkcall, inp, outp, throws, inargs, outargs):
    '''Generate a statement for calling a method'''
    if len(outargs) != len(outp):
        raise ICE(site, "wrong number of outargs")

    return_first_outarg = bool(not throws and outp)

    callscope = Symtab(global_scope)

    # Add proxy output variables if needed. This is needed e.g. if
    # an uint8 variable is passed in an uint32 output parameter.
    postcode = []
    outargs_conv = []
    for (arg, (parmname, parmtype)) in zip(
            outargs[return_first_outarg:], outp[return_first_outarg:]):
        # It would make sense to pass output arguments directly, but
        # the mechanisms to detect whether this is safe are
        # broken. See bug 21900.
        # if (isinstance(arg, (
        #         Variable, ctree.Dereference, ctree.ArrayRef, ctree.SubRef))
        #     and TPtr(parmtype).canstore(TPtr(arg.ctype()))):
        #     outargs_conv.append(mkAddressOf(arg.site, arg))
        # else:
        var = add_proxy_outvar(site, '_ret_' + parmname, parmtype,
                               callscope)
        outargs_conv.append(mkAddressOf(var.site, var))
        postcode.append(copy_outarg(arg, var, parmname, parmtype, name))

    typecheck_inargs(site, inargs, inp, 'method')
    call_expr = mkcall(list(inargs) + outargs_conv)

    if throws:
        if not Failure.fail_stack[-1].allowed:
            raise EBADFAIL(site)
        call_stmt = mkIf(site, call_expr, Failure.fail_stack[-1].fail(site))
    else:
        if outargs:
            call_stmt = mkCopyData(site, call_expr, outargs[0])
        else:
            call_stmt = mkExpressionStatement(site, call_expr)

    return mkCompound(site, declarations(callscope) + [call_stmt] + postcode)

ctree.codegen_call_expr = codegen_call_expr
