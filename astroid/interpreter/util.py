# copyright 2003-2015 LOGILAB S.A. (Paris, FRANCE), all rights reserved.
# contact http://www.logilab.fr/ -- mailto:contact@logilab.fr
#
# This file is part of astroid.
#
# astroid is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 2.1 of the License, or (at your
# option) any later version.
#
# astroid is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with astroid. If not, see <http://www.gnu.org/licenses/>.

"""Utilities for inference."""
import types

import six

from astroid import context as contextmod
from astroid import decorators
from astroid import exceptions
from astroid.interpreter import runtimeabc
from astroid import manager
from astroid.tree import treeabc
from astroid import util


MANAGER = manager.AstroidManager()
BUILTINS = six.moves.builtins.__name__


def infer_stmts(stmts, context, frame=None):
    """Return an iterator on statements inferred by each statement in *stmts*."""
    stmt = None
    inferred = False
    if context is not None:
        name = context.lookupname
        context = context.clone()
    else:
        name = None
        context = contextmod.InferenceContext()

    for stmt in stmts:
        if stmt is util.Uninferable:
            yield stmt
            inferred = True
            continue
        context.lookupname = stmt._infer_name(frame, name)
        try:
            for inferred in stmt.infer(context=context):
                yield inferred
                inferred = True
        except exceptions.UnresolvableName:
            continue
        except exceptions.InferenceError:
            yield util.Uninferable
            inferred = True
    if not inferred:
        raise exceptions.InferenceError(str(stmt))


@decorators.raise_if_nothing_inferred
def unpack_infer(stmt, context=None):
    """recursively generate nodes inferred by the given statement.
    If the inferred value is a list or a tuple, recurse on the elements
    """
    if isinstance(stmt, (treeabc.List, treeabc.Tuple)):
        for elt in stmt.elts:
            if elt is util.Uninferable:
                yield elt
                continue
            for inferred_elt in unpack_infer(elt, context):
                yield inferred_elt
        # Explicit StopIteration to return error information, see comment
        # in raise_if_nothing_inferred.
        raise StopIteration(dict(node=stmt, context=context))
    # if inferred is a final node, return it and stop
    inferred = next(stmt.infer(context))
    if inferred is stmt:
        yield inferred
        raise StopIteration(dict(node=stmt, context=context))
    # else, infer recursivly, except Uninferable object that should be returned as is
    for inferred in stmt.infer(context):
        if inferred is util.Uninferable:
            yield inferred
        else:
            for inf_inf in unpack_infer(inferred, context):
                yield inf_inf
    raise StopIteration(dict(node=stmt, context=context))

def are_exclusive(stmt1, stmt2, exceptions=None):
    """return true if the two given statements are mutually exclusive

    `exceptions` may be a list of exception names. If specified, discard If
    branches and check one of the statement is in an exception handler catching
    one of the given exceptions.

    algorithm :
     1) index stmt1's parents
     2) climb among stmt2's parents until we find a common parent
     3) if the common parent is a If or TryExcept statement, look if nodes are
        in exclusive branches
    """
    # index stmt1's parents
    stmt1_parents = {}
    children = {}
    node = stmt1.parent
    previous = stmt1
    while node:
        stmt1_parents[node] = 1
        children[node] = previous
        previous = node
        node = node.parent
    # climb among stmt2's parents until we find a common parent
    node = stmt2.parent
    previous = stmt2
    while node:
        if node in stmt1_parents:
            # if the common parent is a If or TryExcept statement, look if
            # nodes are in exclusive branches
            if isinstance(node, treeabc.If) and exceptions is None:
                if (node.locate_child(previous)[1]
                        is not node.locate_child(children[node])[1]):
                    return True
            elif isinstance(node, treeabc.TryExcept):
                c2attr, c2node = node.locate_child(previous)
                c1attr, c1node = node.locate_child(children[node])
                if c1node is not c2node:
                    if ((c2attr == 'body'
                         and c1attr == 'handlers'
                         and children[node].catch(exceptions)) or
                            (c2attr == 'handlers' and c1attr == 'body' and previous.catch(exceptions)) or
                            (c2attr == 'handlers' and c1attr == 'orelse') or
                            (c2attr == 'orelse' and c1attr == 'handlers')):
                        return True
                elif c2attr == 'handlers' and c1attr == 'handlers':
                    return previous is not children[node]
            return False
        previous = node
        node = node.parent
    return False


def class_instance_as_index(node):
    """Get the value as an index for the given instance.

    If an instance provides an __index__ method, then it can
    be used in some scenarios where an integer is expected,
    for instance when multiplying or subscripting a list.
    """
    context = contextmod.InferenceContext()
    context.callcontext = contextmod.CallContext(args=[node])

    try:
        for inferred in node.igetattr('__index__', context=context):
            if not isinstance(inferred, runtimeabc.BoundMethod):
                continue

            for result in inferred.infer_call_result(node, context=context):
                if (isinstance(result, treeabc.Const)
                        and isinstance(result.value, int)):
                    return result
    except exceptions.InferenceError:
        pass

def safe_infer(node, context=None):
    """Return the inferred value for the given node.

    Return None if inference failed or if there is some ambiguity (more than
    one node has been inferred).
    """
    try:
        inferit = node.infer(context=context)
        value = next(inferit)
    except exceptions.InferenceError:
        return
    try:
        next(inferit)
        return # None if there is ambiguity on the inferred node
    except exceptions.InferenceError:
        return # there is some kind of ambiguity
    except StopIteration:
        return value


def has_known_bases(klass, context=None):
    """Return true if all base classes of a class could be inferred."""
    try:
        return klass._all_bases_known
    except AttributeError:
        pass
    for base in klass.bases:
        result = safe_infer(base, context=context)
        # TODO: check for A->B->A->B pattern in class structure too?
        if (not isinstance(result, treeabc.ClassDef) or
                result is klass or
                not has_known_bases(result, context=context)):
            klass._all_bases_known = False
            return False
    klass._all_bases_known = True
    return True


def _type_check(type1, type2):
    if not all(map(has_known_bases, (type1, type2))):
        return util.Uninferable

    if not all([type1.newstyle, type2.newstyle]):
        return False
    try:
        return type1 in type2.mro()[:-1]
    except exceptions.MroError:
        # The MRO is invalid.
        return util.Uninferable


def is_subtype(type1, type2):
    """Check if *type1* is a subtype of *typ2*."""
    return _type_check(type2, type1)


def is_supertype(type1, type2):
    """Check if *type2* is a supertype of *type1*."""
    return _type_check(type1, type2)


def _object_type(node, context=None):
    context = context or contextmod.InferenceContext()
    builtins_ast = MANAGER.builtins()

    for inferred in node.infer(context=context):
        if isinstance(inferred, treeabc.ClassDef):
            if inferred.newstyle:
                metaclass = inferred.metaclass()
                if metaclass:
                    yield metaclass
                    continue
            yield builtins_ast.getattr('type')[0]
        elif isinstance(inferred, (treeabc.Lambda, runtimeabc.UnboundMethod)):
            if isinstance(inferred, treeabc.Lambda):
                if inferred.root() is builtins_ast:
                    yield builtins_ast[types.BuiltinFunctionType.__name__]
                else:
                    yield builtins_ast[types.FunctionType.__name__]
            elif isinstance(inferred, runtimeabc.BoundMethod):
                yield builtins_ast[types.MethodType.__name__]
            elif isinstance(inferred, runtimeabc.UnboundMethod):
                if six.PY2:
                    yield builtins_ast[types.MethodType.__name__]
                else:
                    yield builtins_ast[types.FunctionType.__name__]
            else:
                raise exceptions.InferenceError(
                    'Function {func!r} inferred from {node!r} '
                    'has no identifiable type.',
                    node=node, func=inferred, contex=context)
        elif isinstance(inferred, treeabc.Module):
            yield builtins_ast[types.ModuleType.__name__]
        else:
            yield inferred._proxied


def object_type(node, context=None):
    """Obtain the type of the given node

    This is used to implement the ``type`` builtin, which means that it's
    used for inferring type calls, as well as used in a couple of other places
    in the inference.
    The node will be inferred first, so this function can support all
    sorts of objects, as long as they support inference.
    """

    try:
        types = set(_object_type(node, context))
    except exceptions.InferenceError:
        return util.Uninferable
    if len(types) > 1 or not types:
        return util.Uninferable
    return list(types)[0]


def do_import_module(node, modname):
    """Return the ast for a module whose name is <modname> imported by the given node."""

    # handle special case where we are on a package node importing a module
    # using the same name as the package, which may end in an infinite loop
    # on relative imports
    # XXX: no more needed ?
    if not isinstance(node, (treeabc.Import, treeabc.ImportFrom)):
        raise TypeError('Operation is undefined for node of type %s'
                        % type(node))

    mymodule = node.root()
    level = getattr(node, 'level', None) # Import as no level
    # XXX we should investigate deeper if we really want to check
    # importing itself: modname and mymodule.name be relative or absolute
    if mymodule.relative_to_absolute_name(modname, level) == mymodule.name:
        # FIXME: we used to raise InferenceError here, but why ?
        return mymodule

    return mymodule.import_module(modname, level=level,
                                  relative_only=level and level >= 1)


def real_name(node, asname):
    """get name from 'as' name"""
    if not isinstance(node, (treeabc.Import, treeabc.ImportFrom)):
        raise TypeError('Operation is undefined for node of type %s'
                        % type(node))

    for name, _asname in node.names:
        if name == '*':
            return asname
        if not _asname:
            name = name.split('.', 1)[0]
            _asname = name
        if asname == _asname:
            return name
    raise exceptions.AttributeInferenceError(
        'Could not find original name for {attribute} in {target!r}',
        target=node, attribute=asname)