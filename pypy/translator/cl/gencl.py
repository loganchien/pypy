from pypy.objspace.flow.model import Constant
from pypy.annotation.annrpython import RPythonAnnotator

from pypy.translator.simplify import simplify_graph
from pypy.translator.transform import transform_graph, default_extra_passes, transform_slice

from pypy.rpython.ootypesystem.ootype import Instance, List
from pypy.translator.cl.clrepr import repr_arg, repr_var, repr_const


class Op:

    def __init__(self, gen, op):
        self.gen = gen
        self.str = repr_arg
        self.op = op
        self.opname = op.opname
        self.args = op.args
        self.result = op.result

    def __iter__(self):
        if self.opname in self.binary_ops:
            for line in self.op_binary(self.opname):
                yield line
        else:
            meth = getattr(self, "op_" + self.opname)
            for line in meth():
                yield line

    def op_same_as(self):
        target = self.str(self.result)
        origin = self.str(self.args[0])
        yield "(setf %s %s)" % (target, origin)

    binary_ops = {
        #"add": "+",
        "int_add": "+",
        "sub": "-",
        "inplace_add": "+", # weird, but it works
        "inplace_lshift": "ash",
        "mod": "mod",
        "int_mod": "mod",
        "lt": "<",
        "int_lt": "<",
        "le": "<=",
        "eq": "=",
        "int_eq": "=",
        "gt": ">",
        "and_": "logand",
        "getitem": "elt",
    }

    def op_binary(self, op):
        s = self.str
        result, (arg1, arg2) = self.result, self.args
        cl_op = self.binary_ops[op]
        yield "(setf %s (%s %s %s))" % (s(result), cl_op, s(arg1), s(arg2))

    def op_int_is_true(self):
        target = self.str(self.result) 
        arg = self.str(self.args[0])
        yield "(setf %s (not (zerop %s)))" % (target, arg)

    def declare_class(self, cls):
        # cls is really type of Instance
        name = cls._name
        fields = cls._fields
        fieldnames = fields.keys()
        field_declaration = ' '.join(fieldnames)
        class_declaration = "(defstruct %s %s)" % (name, field_declaration)
        return class_declaration

    def op_new(self):
        cls = self.args[0].value
        if isinstance(cls, List):
            generator = self.op_new_list
        else:
            generator = self.op_new_instance
        for line in generator(cls):
            yield line

    def op_new_list(self, cls):
        target = self.str(self.result)
        yield "(setf %s (make-array 0 :adjustable t))" % (target,)

    def op_new_instance(self, cls):
        yield self.declare_class(cls)
        target = self.str(self.result)
        yield "(setf %s (make-%s))" % (target, cls._name)

    def op_oosend(self):
        method = self.args[0].value
        receiver = self.args[1]
        args = self.args[2:]
        if isinstance(receiver.concretetype, List):
            impl = ListImpl(receiver)
            getattr(impl, method)(*args)

    def op_oogetfield(self):
        target = self.str(self.result)
        clsname = self.args[0].concretetype._name
        fieldname = self.args[1].value
        obj = self.str(self.args[0])
        yield "(setf %s (%s-%s %s))" % (target, clsname, fieldname, obj)

    def op_oosetfield(self):
        target = self.str(self.result)
        clsname = self.args[0].concretetype._name
        fieldname = self.args[1].value
        if fieldname == "meta": # XXX
            raise StopIteration
        obj = self.str(self.args[0])
        fieldvalue = self.str(self.args[2])
        yield "(setf (%s-%s %s) %s)" % (clsname, fieldname, obj, fieldvalue)


class ListImpl:

    def __init__(self, receiver):
        self.obj = repr_arg(receiver)

    def ll_length(self):
        pass

    def ll_getitem_fast(self, index):
        pass

    def ll_setitem_fast(self, index, value):
        index = repr_arg(index)
        value = repr_arg(value)
        return "(setf (aref %s %s) %s)" % (self.obj, index, value)

    def _ll_resize(self, size):
        size = repr_arg(size)
        return "(adjust-array %s %s)" % (self.obj, size)


class GenCL:

    def __init__(self, fun, input_arg_types=[]):
        # NB. 'fun' is a graph!
        simplify_graph(fun)
        self.fun = fun
        self.blockref = {}

    def annotate(self, input_arg_types):
        ann = RPythonAnnotator()
        inputcells = [ann.typeannotation(t) for t in input_arg_types]
        ann.build_graph_types(self.fun, inputcells)
        self.setannotator(ann)

    def setannotator(self, annotator):
        self.ann = annotator

    def get_type(self, var):
        return var.concretetype

    def emitcode(self, public=True):
        code = "\n".join(list(self.emit()))
        return code

    def emit(self):
        for line in self.emit_defun(self.fun):
            yield line

    def emit_defun(self, fun):
        yield ";;;; Main"
        yield "(defun " + fun.name
        arglist = fun.getargs()
        yield "("
        for arg in arglist:
            yield repr_var(arg)
        yield ")"
        yield "(prog"
        blocklist = list(fun.iterblocks())
        vardict = {}
        for block in blocklist:
            tag = len(self.blockref)
            self.blockref[block] = tag
            for var in block.getvariables():
                vardict[var] = self.get_type(var)
        yield "( last-exc"
        for var in vardict:
            if var in arglist:
                yield "(%s %s)" % (repr_var(var), repr_var(var))
            else:
                yield repr_var(var)
        yield ")"
        yield "(setf last-exc nil)"
        for block in blocklist:
            yield ""
            for line in self.emit_block(block):
                yield line
        yield "))"

    def emit_block(self, block):
        self.cur_block = block
        tag = self.blockref[block]
        yield "tag" + str(tag)
        for op in block.operations:
            emit_op = Op(self, op)
            for line in emit_op:
                yield line
        exits = block.exits
        if len(exits) == 1:
            for line in self.emit_link(exits[0]):
                yield line
        elif len(exits) > 1:
            # only works in the current special case
            if (len(exits) == 2 and
                exits[0].exitcase == False and
                exits[1].exitcase == True):
                yield "(if " + repr_arg(block.exitswitch)
                yield "(progn"
                for line in self.emit_link(exits[1]):
                    yield line
                yield ") ; else"
                yield "(progn"
                for line in self.emit_link(exits[0]):
                    yield line
                yield "))"
            else:
                # this is for the more general case.  The previous special case
                # shouldn't be needed but in Python 2.2 we can't tell apart
                # 0 vs nil  and  1 vs t  :-(
                for exit in exits[:-1]:
                    yield "(if (equalp " + repr_arg(block.exitswitch)
                    yield repr_const(exit.exitcase) + ')'
                    yield "(progn"
                    for line in self.emit_link(exit):
                        yield line
                    yield ")"
                yield "(progn ; else should be %s" % repr_const(exits[-1].exitcase)
                for line in self.emit_link(exits[-1]):
                    yield line
                yield ")" * len(exits)
        elif len(block.inputargs) == 2:    # exc_cls, exc_value
            exc_cls   = repr_var(block.inputargs[0])
            exc_value = repr_var(block.inputargs[1])
            yield "(something-like-throw-exception %s %s)" % (exc_cls, exc_value)
        else:
            retval = repr_var(block.inputargs[0])
            yield "(return %s )" % retval

    def format_jump(self, block):
        tag = self.blockref[block]
        return "(go tag" + str(tag) + ")"

    def emit_link(self, link):
        source = map(repr_arg, link.args)
        target = map(repr_var, link.target.inputargs)
        yield "(setf"
        couples = zip(source, target)
        for s, t in couples[:-1]:
            yield "%s %s" % (t, s)
        else:
            s, t = couples[-1]
            yield "%s %s)" % (t, s)
        yield self.format_jump(link.target)

