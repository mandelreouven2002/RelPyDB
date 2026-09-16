/*
 * _relpy_core — native acceleration for the RelPyDB in-memory query engine.
 *
 * This module does NOT change any front-facing behaviour. It evaluates the
 * structured `plan` that Condition objects carry (see relpy/queries.py) against
 * raw row dicts in a tight C loop, returning the indices of the rows that pass.
 * Anything a plan cannot express falls back to the pure-Python predicate, so
 * results are identical whether or not this module is present.
 *
 * Opcodes must stay in sync with relpy/queries.py.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

enum {
    OP_EQ = 0,
    OP_NE = 1,
    OP_GT = 2,
    OP_GE = 3,
    OP_LT = 4,
    OP_LE = 5,
    OP_IN = 6,
    OP_BETWEEN = 7,
    OP_ISNULL = 8,
    OP_NOTNULL = 9,
    OP_AND = 10,
    OP_OR = 11,
    OP_NOT = 12
};

/* Borrowed reference to row[colname], or NULL with an exception set
 * (KeyError if the column is genuinely absent, matching ColumnRef._value). */
static PyObject *
get_col(PyObject *row, PyObject *colname)
{
    PyObject *v = PyDict_GetItemWithError(row, colname); /* borrowed */
    if (v == NULL && !PyErr_Occurred()) {
        PyErr_Format(PyExc_KeyError,
                     "Column '%U' does not exist in row.", colname);
    }
    return v;
}

/* Evaluate a plan node against a row.
 * Returns 1 (true), 0 (false), or -1 (error, with an exception set). */
static int
eval_node(PyObject *node, PyObject *row)
{
    if (!PyTuple_Check(node)) {
        PyErr_SetString(PyExc_TypeError, "plan node must be a tuple");
        return -1;
    }
    Py_ssize_t size = PyTuple_GET_SIZE(node);
    if (size < 1) {
        PyErr_SetString(PyExc_ValueError, "empty plan node");
        return -1;
    }

    long op = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    if (op == -1 && PyErr_Occurred()) {
        return -1;
    }

    /* Boolean combinators. */
    if (op == OP_AND) {
        if (size != 3) goto badshape;
        int l = eval_node(PyTuple_GET_ITEM(node, 1), row);
        if (l < 0) return -1;
        if (l == 0) return 0;                 /* short-circuit */
        return eval_node(PyTuple_GET_ITEM(node, 2), row);
    }
    if (op == OP_OR) {
        if (size != 3) goto badshape;
        int l = eval_node(PyTuple_GET_ITEM(node, 1), row);
        if (l < 0) return -1;
        if (l == 1) return 1;                 /* short-circuit */
        return eval_node(PyTuple_GET_ITEM(node, 2), row);
    }
    if (op == OP_NOT) {
        if (size != 2) goto badshape;
        int c = eval_node(PyTuple_GET_ITEM(node, 1), row);
        if (c < 0) return -1;
        return c ? 0 : 1;
    }

    /* Leaf comparisons: node[1] is the column name. */
    if (size < 2) goto badshape;
    PyObject *colname = PyTuple_GET_ITEM(node, 1);
    PyObject *v = get_col(row, colname);
    if (v == NULL) return -1;

    switch (op) {
    case OP_EQ:
        if (size != 3) goto badshape;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_EQ);
    case OP_NE:
        if (size != 3) goto badshape;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_NE);
    case OP_GT:
        if (size != 3) goto badshape;
        if (v == Py_None) return 0;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_GT);
    case OP_GE:
        if (size != 3) goto badshape;
        if (v == Py_None) return 0;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_GE);
    case OP_LT:
        if (size != 3) goto badshape;
        if (v == Py_None) return 0;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_LT);
    case OP_LE:
        if (size != 3) goto badshape;
        if (v == Py_None) return 0;
        return PyObject_RichCompareBool(v, PyTuple_GET_ITEM(node, 2), Py_LE);
    case OP_IN: {
        if (size != 3) goto badshape;
        /* `v in value_set`; matches Python semantics incl. TypeError on
         * an unhashable v (which propagates and triggers a fallback). */
        return PySet_Contains(PyTuple_GET_ITEM(node, 2), v);
    }
    case OP_BETWEEN: {
        if (size != 5) goto badshape;
        if (v == Py_None) return 0;
        PyObject *lo = PyTuple_GET_ITEM(node, 2);
        PyObject *hi = PyTuple_GET_ITEM(node, 3);
        int inclusive = PyObject_IsTrue(PyTuple_GET_ITEM(node, 4));
        if (inclusive < 0) return -1;
        int cmp = inclusive ? Py_LE : Py_LT;
        int a = PyObject_RichCompareBool(lo, v, cmp);   /* lo (<|<=) v */
        if (a < 0) return -1;
        if (a == 0) return 0;
        return PyObject_RichCompareBool(v, hi, cmp);     /* v (<|<=) hi */
    }
    case OP_ISNULL:
        return (v == Py_None) ? 1 : 0;
    case OP_NOTNULL:
        return (v != Py_None) ? 1 : 0;
    default:
        PyErr_Format(PyExc_ValueError, "unknown plan opcode %ld", op);
        return -1;
    }

badshape:
    PyErr_SetString(PyExc_ValueError, "malformed plan node");
    return -1;
}

/* filter_indices(rows, plan) -> list[int]
 * Returns the indices of rows for which `plan` evaluates true. */
static PyObject *
py_filter_indices(PyObject *self, PyObject *args)
{
    PyObject *rows, *plan;
    if (!PyArg_ParseTuple(args, "OO", &rows, &plan)) {
        return NULL;
    }
    if (!PyList_Check(rows)) {
        PyErr_SetString(PyExc_TypeError, "rows must be a list");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(rows);
    PyObject *out = PyList_New(0);
    if (out == NULL) {
        return NULL;
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *row = PyList_GET_ITEM(rows, i);   /* borrowed */
        if (!PyDict_Check(row)) {
            Py_DECREF(out);
            PyErr_SetString(PyExc_TypeError, "each row must be a dict");
            return NULL;
        }
        int r = eval_node(plan, row);
        if (r < 0) {
            Py_DECREF(out);
            return NULL;
        }
        if (r == 1) {
            PyObject *idx = PyLong_FromSsize_t(i);
            if (idx == NULL) {
                Py_DECREF(out);
                return NULL;
            }
            if (PyList_Append(out, idx) < 0) {
                Py_DECREF(idx);
                Py_DECREF(out);
                return NULL;
            }
            Py_DECREF(idx);
        }
    }
    return out;
}

/* gather_columns(rows, columns) -> list[tuple]
 * Builds tuple(row[c] for c in columns) for every row, in one C loop.
 * Used to speed key extraction for ORDER BY / GROUP BY / DISTINCT / JOIN.
 * Raises KeyError if a column is missing (fallback to Python). */
static PyObject *
py_gather_columns(PyObject *self, PyObject *args)
{
    PyObject *rows, *columns;
    if (!PyArg_ParseTuple(args, "OO", &rows, &columns)) {
        return NULL;
    }
    if (!PyList_Check(rows)) {
        PyErr_SetString(PyExc_TypeError, "rows must be a list");
        return NULL;
    }
    if (!PyTuple_Check(columns)) {
        PyErr_SetString(PyExc_TypeError, "columns must be a tuple");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(rows);
    Py_ssize_t ncols = PyTuple_GET_SIZE(columns);
    PyObject *out = PyList_New(n);
    if (out == NULL) {
        return NULL;
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *row = PyList_GET_ITEM(rows, i);   /* borrowed */
        if (!PyDict_Check(row)) {
            Py_DECREF(out);
            PyErr_SetString(PyExc_TypeError, "each row must be a dict");
            return NULL;
        }
        PyObject *key = PyTuple_New(ncols);
        if (key == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        for (Py_ssize_t c = 0; c < ncols; c++) {
            PyObject *colname = PyTuple_GET_ITEM(columns, c);
            PyObject *v = get_col(row, colname);    /* borrowed or NULL */
            if (v == NULL) {
                Py_DECREF(key);
                Py_DECREF(out);
                return NULL;
            }
            Py_INCREF(v);
            PyTuple_SET_ITEM(key, c, v);            /* steals ref */
        }
        PyList_SET_ITEM(out, i, key);               /* steals ref */
    }
    return out;
}

static PyMethodDef core_methods[] = {
    {"filter_indices", py_filter_indices, METH_VARARGS,
     "filter_indices(rows, plan) -> list[int]: indices where plan is true."},
    {"gather_columns", py_gather_columns, METH_VARARGS,
     "gather_columns(rows, columns) -> list[tuple]: per-row column tuples."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef core_module = {
    PyModuleDef_HEAD_INIT,
    "_relpy_core",
    "Native acceleration for the RelPyDB query engine.",
    -1,
    core_methods
};

PyMODINIT_FUNC
PyInit__relpy_core(void)
{
    return PyModule_Create(&core_module);
}
