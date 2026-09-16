/*
 * _relpyengine — a native C core for RelPyDB.
 *
 * This is the "mostly C" core: table data lives in native C columnar memory
 * (typed arrays, not Python objects), and scans, filters, aggregates and
 * group-by run entirely in C. Python is only the thin shell that builds the
 * query plan and reads back the final result.
 *
 * This is the same architecture SQLite and DuckDB use: a C engine with a small
 * language binding on top. It is deliberately a focused core — it owns the hot,
 * data-heavy path (store rows, scan them, filter, aggregate, group) — while the
 * Python layer keeps the ergonomic API and handles the rarer operations.
 *
 * Supported column types: int (int64), float (double), bool, str (UTF-8), each
 * with a NULL bitmap.  Filter plan opcodes match relpy/queries.py.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

/* ---- filter opcodes (must match relpy/queries.py) ---- */
enum { OP_EQ=0, OP_NE=1, OP_GT=2, OP_GE=3, OP_LT=4, OP_LE=5,
       OP_IN=6, OP_BETWEEN=7, OP_ISNULL=8, OP_NOTNULL=9,
       OP_AND=10, OP_OR=11, OP_NOT=12 };

/* ---- column types ---- */
enum { COL_INT=0, COL_FLOAT=1, COL_BOOL=2, COL_STR=3 };

typedef struct {
    char   *name;
    int     type;
    uint8_t *nulls;      /* 1 == NULL */
    /* exactly one of these is used per type */
    int64_t *idata;
    double  *fdata;
    uint8_t *bdata;
    char   **sdata;      /* owned NUL-terminated UTF-8 copies */
} Column;

typedef struct {
    char      *name;
    int        ncols;
    Column    *cols;
    Py_ssize_t nrows;
    Py_ssize_t cap;
} Table;

typedef struct {
    PyObject_HEAD
    Table     *tables;
    int        ntables;
    int        tcap;
} DatabaseObject;

/* ============================ helpers ================================== */

static Table *find_table(DatabaseObject *db, const char *name) {
    for (int i = 0; i < db->ntables; i++)
        if (strcmp(db->tables[i].name, name) == 0)
            return &db->tables[i];
    return NULL;
}

static int find_col(Table *t, const char *name) {
    for (int i = 0; i < t->ncols; i++)
        if (strcmp(t->cols[i].name, name) == 0)
            return i;
    return -1;
}

static int ensure_cap(Table *t, Py_ssize_t need) {
    if (need <= t->cap) return 0;
    Py_ssize_t newcap = t->cap ? t->cap : 16;
    while (newcap < need) newcap *= 2;
    for (int c = 0; c < t->ncols; c++) {
        Column *col = &t->cols[c];
        uint8_t *nn = realloc(col->nulls, newcap);
        if (!nn) return -1;
        col->nulls = nn;
        switch (col->type) {
        case COL_INT:  { void *p = realloc(col->idata, newcap*sizeof(int64_t)); if(!p) return -1; col->idata=p; break; }
        case COL_FLOAT:{ void *p = realloc(col->fdata, newcap*sizeof(double));  if(!p) return -1; col->fdata=p; break; }
        case COL_BOOL: { void *p = realloc(col->bdata, newcap);                 if(!p) return -1; col->bdata=p; break; }
        case COL_STR:  { void *p = realloc(col->sdata, newcap*sizeof(char*));   if(!p) return -1; col->sdata=p; break; }
        }
    }
    t->cap = newcap;
    return 0;
}

/* Convert a Python cell value into column storage at row r. Returns 0/-1. */
static int store_cell(Column *col, Py_ssize_t r, PyObject *v) {
    if (v == Py_None) {
        col->nulls[r] = 1;
        if (col->type == COL_STR) col->sdata[r] = NULL;
        else if (col->type == COL_INT) col->idata[r] = 0;
        else if (col->type == COL_FLOAT) col->fdata[r] = 0;
        else col->bdata[r] = 0;
        return 0;
    }
    col->nulls[r] = 0;
    switch (col->type) {
    case COL_INT: {
        long long x = PyLong_AsLongLong(v);
        if (x == -1 && PyErr_Occurred()) {
            /* allow float -> int store only if exact? RelPy stores as given;
               fall back: accept python int only. */
            return -1;
        }
        col->idata[r] = x; return 0;
    }
    case COL_FLOAT: {
        double x = PyFloat_AsDouble(v);
        if (x == -1.0 && PyErr_Occurred()) return -1;
        col->fdata[r] = x; return 0;
    }
    case COL_BOOL: {
        int x = PyObject_IsTrue(v);
        if (x < 0) return -1;
        col->bdata[r] = (uint8_t)x; return 0;
    }
    case COL_STR: {
        Py_ssize_t len;
        const char *s = PyUnicode_AsUTF8AndSize(v, &len);
        if (!s) return -1;
        char *copy = malloc(len + 1);
        if (!copy) { PyErr_NoMemory(); return -1; }
        memcpy(copy, s, len); copy[len] = 0;
        col->sdata[r] = copy; return 0;
    }
    }
    return -1;
}

/* Build a Python object from a stored cell. New reference. */
static PyObject *load_cell(Column *col, Py_ssize_t r) {
    if (col->nulls[r]) Py_RETURN_NONE;
    switch (col->type) {
    case COL_INT:   return PyLong_FromLongLong(col->idata[r]);
    case COL_FLOAT: return PyFloat_FromDouble(col->fdata[r]);
    case COL_BOOL:  if (col->bdata[r]) Py_RETURN_TRUE; Py_RETURN_FALSE;
    case COL_STR:   return PyUnicode_FromString(col->sdata[r] ? col->sdata[r] : "");
    }
    Py_RETURN_NONE;
}

/* ==================== compiled filter plan ============================= */

typedef struct Node {
    int op;
    int col;                 /* leaf: column index */
    int rhs_kind;            /* 0 none,1 int,2 float,3 bool,4 str */
    long long ri;
    double     rf;
    int        rb;
    char      *rs; Py_ssize_t rs_len;
    /* between */
    int between_incl;
    long long lo_i, hi_i; double lo_f, hi_f; int bounds_float;
    PyObject  *set;          /* IN: borrowed set */
    struct Node *left, *right;
} Node;

static void free_node(Node *n) {
    if (!n) return;
    free_node(n->left); free_node(n->right);
    free(n->rs);
    free(n);
}

/* classify a python scalar into kind + fill numeric fields */
static void classify(PyObject *v, int *kind, long long *iv, double *fv, int *bv,
                     char **sv, Py_ssize_t *slen) {
    if (v == Py_None) { *kind = 0; return; }
    if (PyBool_Check(v)) { *kind = 3; *bv = (v == Py_True);
                           *iv = *bv; *fv = (double)*bv; return; }
    if (PyLong_Check(v)) { *kind = 1; *iv = PyLong_AsLongLong(v);
                           *fv = (double)*iv; return; }
    if (PyFloat_Check(v)) { *kind = 2; *fv = PyFloat_AsDouble(v);
                            *iv = (long long)*fv; return; }
    if (PyUnicode_Check(v)) { Py_ssize_t l; const char *s = PyUnicode_AsUTF8AndSize(v,&l);
                              *kind = 4; *sv = (char*)s; *slen = l; return; }
    *kind = -1;
}

static Node *compile_plan(Table *t, PyObject *p) {
    if (p == Py_None) return NULL;
    if (!PyTuple_Check(p) || PyTuple_GET_SIZE(p) < 1) {
        PyErr_SetString(PyExc_ValueError, "bad plan node"); return NULL;
    }
    long op = PyLong_AsLong(PyTuple_GET_ITEM(p, 0));
    if (op == -1 && PyErr_Occurred()) return NULL;
    Node *n = calloc(1, sizeof(Node));
    if (!n) { PyErr_NoMemory(); return NULL; }
    n->op = (int)op;

    if (op == OP_AND || op == OP_OR) {
        n->left  = compile_plan(t, PyTuple_GET_ITEM(p, 1));
        n->right = compile_plan(t, PyTuple_GET_ITEM(p, 2));
        if (!n->left || !n->right) { free_node(n); return NULL; }
        return n;
    }
    if (op == OP_NOT) {
        n->left = compile_plan(t, PyTuple_GET_ITEM(p, 1));
        if (!n->left) { free_node(n); return NULL; }
        return n;
    }
    /* leaf: item[1] is column name */
    const char *cname = PyUnicode_AsUTF8(PyTuple_GET_ITEM(p, 1));
    if (!cname) { free_node(n); return NULL; }
    n->col = find_col(t, cname);
    if (n->col < 0) { PyErr_Format(PyExc_KeyError, "column '%s'", cname); free_node(n); return NULL; }

    if (op == OP_ISNULL || op == OP_NOTNULL) return n;

    if (op == OP_IN) { n->set = PyTuple_GET_ITEM(p, 2); return n; }  /* borrowed */

    if (op == OP_BETWEEN) {
        int k; long long iv; double fv; int bv; char *sv=NULL; Py_ssize_t sl=0;
        classify(PyTuple_GET_ITEM(p,2), &k,&iv,&fv,&bv,&sv,&sl);
        n->lo_i = iv; n->lo_f = fv; if (k==2) n->bounds_float=1;
        classify(PyTuple_GET_ITEM(p,3), &k,&iv,&fv,&bv,&sv,&sl);
        n->hi_i = iv; n->hi_f = fv; if (k==2) n->bounds_float=1;
        n->between_incl = PyObject_IsTrue(PyTuple_GET_ITEM(p,4));
        return n;
    }

    /* comparison leaf: item[2] is rhs scalar */
    char *sv=NULL; Py_ssize_t sl=0;
    classify(PyTuple_GET_ITEM(p,2), &n->rhs_kind, &n->ri, &n->rf, &n->rb, &sv, &sl);
    if (n->rhs_kind == 4) {
        n->rs = malloc(sl+1); if (!n->rs){PyErr_NoMemory();free_node(n);return NULL;}
        memcpy(n->rs, sv, sl); n->rs[sl]=0; n->rs_len = sl;
    }
    return n;
}

/* numeric value of a cell as double; caller checks null first */
static inline double cell_double(Column *c, Py_ssize_t r) {
    switch (c->type) {
    case COL_INT:  return (double)c->idata[r];
    case COL_FLOAT:return c->fdata[r];
    case COL_BOOL: return (double)c->bdata[r];
    default: return NAN;
    }
}

static int apply_cmp(int op, int cmp) {
    switch (op) {
    case OP_EQ: return cmp == 0;
    case OP_NE: return cmp != 0;
    case OP_GT: return cmp > 0;
    case OP_GE: return cmp >= 0;
    case OP_LT: return cmp < 0;
    case OP_LE: return cmp <= 0;
    }
    return 0;
}

/* evaluate compiled node for row r; returns 0/1/-1(err) */
static int eval(Node *n, Table *t, Py_ssize_t r) {
    switch (n->op) {
    case OP_AND: { int l = eval(n->left,t,r); if (l<0)return -1; if(!l)return 0; return eval(n->right,t,r); }
    case OP_OR:  { int l = eval(n->left,t,r); if (l<0)return -1; if(l)return 1;  return eval(n->right,t,r); }
    case OP_NOT: { int c = eval(n->left,t,r); if (c<0)return -1; return !c; }
    }
    Column *c = &t->cols[n->col];
    int isnull = c->nulls[r];

    if (n->op == OP_ISNULL)  return isnull ? 1 : 0;
    if (n->op == OP_NOTNULL) return isnull ? 0 : 1;

    if (n->op == OP_IN) {
        PyObject *cell = load_cell(c, r);
        if (!cell) return -1;
        int res = PySet_Contains(n->set, cell);
        Py_DECREF(cell);
        return res;  /* may be -1 with TypeError -> propagates -> fallback */
    }

    if (n->op == OP_BETWEEN) {
        if (isnull) return 0;
        if (c->type == COL_STR) return 0; /* between on str: unsupported here */
        double v = cell_double(c, r);
        double lo = n->bounds_float ? n->lo_f : (double)n->lo_i;
        double hi = n->bounds_float ? n->hi_f : (double)n->hi_i;
        if (n->between_incl) return (lo <= v && v <= hi);
        return (lo < v && v < hi);
    }

    /* comparison */
    if (c->type == COL_STR) {
        if (n->rhs_kind == 0) {  /* compare to None */
            if (n->op == OP_EQ) return isnull ? 1 : 0;
            if (n->op == OP_NE) return isnull ? 0 : 1;
            return 0;
        }
        if (n->rhs_kind != 4) { /* str vs non-str */
            if (n->op == OP_EQ) return 0;
            if (n->op == OP_NE) return 1;
            return 0;  /* ordering across types: RelPy would error; treat false */
        }
        if (isnull) {  /* null str vs a str value */
            if (n->op == OP_EQ) return 0;
            if (n->op == OP_NE) return 1;
            return 0;
        }
        int cmp = strcmp(c->sdata[r], n->rs);  /* UTF-8 preserves code-point order */
        return apply_cmp(n->op, cmp);
    }

    /* numeric/bool column */
    if (n->rhs_kind == 0) {         /* compare to None */
        if (n->op == OP_EQ) return isnull ? 1 : 0;
        if (n->op == OP_NE) return isnull ? 0 : 1;
        return 0;                    /* gt/ge/lt/le vs None -> False (RelPy) */
    }
    if (n->rhs_kind == 4) {         /* number vs str */
        if (n->op == OP_EQ) return 0;
        if (n->op == OP_NE) return 1;
        return 0;
    }
    if (isnull) {                   /* null cell, non-None rhs */
        if (n->op == OP_EQ) return 0;
        if (n->op == OP_NE) return 1;
        return 0;                    /* gt/.. on null -> False */
    }
    double v = cell_double(c, r);
    double rhs = (n->rhs_kind == 2) ? n->rf : (double)n->ri;
    int cmp = (v < rhs) ? -1 : (v > rhs) ? 1 : 0;
    return apply_cmp(n->op, cmp);
}

/* ==================== Database methods ================================= */

static PyObject *db_create_table(DatabaseObject *self, PyObject *args) {
    const char *name;
    if (!PyArg_ParseTuple(args, "s", &name)) return NULL;
    if (find_table(self, name)) { PyErr_Format(PyExc_ValueError, "table '%s' exists", name); return NULL; }
    if (self->ntables >= self->tcap) {
        int nc = self->tcap ? self->tcap*2 : 4;
        void *p = realloc(self->tables, nc*sizeof(Table));
        if (!p) return PyErr_NoMemory();
        self->tables = p; self->tcap = nc;
    }
    Table *t = &self->tables[self->ntables++];
    memset(t, 0, sizeof(Table));
    t->name = strdup(name);
    Py_RETURN_NONE;
}

static PyObject *db_add_column(DatabaseObject *self, PyObject *args) {
    const char *tname, *cname, *typecode;
    if (!PyArg_ParseTuple(args, "sss", &tname, &cname, &typecode)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError, "table '%s'", tname); return NULL; }
    int ty;
    switch (typecode[0]) {
    case 'i': ty = COL_INT; break;
    case 'f': ty = COL_FLOAT; break;
    case 'b': ty = COL_BOOL; break;
    case 's': ty = COL_STR; break;
    default: PyErr_SetString(PyExc_ValueError, "bad typecode"); return NULL;
    }
    if (t->nrows > 0) { PyErr_SetString(PyExc_ValueError, "add columns before inserting"); return NULL; }
    Column *nc = realloc(t->cols, (t->ncols+1)*sizeof(Column));
    if (!nc) return PyErr_NoMemory();
    t->cols = nc;
    Column *col = &t->cols[t->ncols++];
    memset(col, 0, sizeof(Column));
    col->name = strdup(cname); col->type = ty;
    Py_RETURN_NONE;
}

static PyObject *db_insert_many(DatabaseObject *self, PyObject *args) {
    const char *tname; PyObject *rows;
    if (!PyArg_ParseTuple(args, "sO", &tname, &rows)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError, "table '%s'", tname); return NULL; }
    PyObject *seq = PySequence_Fast(rows, "rows must be a sequence");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    if (ensure_cap(t, t->nrows + n) < 0) { Py_DECREF(seq); return PyErr_NoMemory(); }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *row = PySequence_Fast_GET_ITEM(seq, i);
        PyObject *rseq = PySequence_Fast(row, "row must be a sequence");
        if (!rseq) { Py_DECREF(seq); return NULL; }
        if (PySequence_Fast_GET_SIZE(rseq) != t->ncols) {
            PyErr_SetString(PyExc_ValueError, "row length != column count");
            Py_DECREF(rseq); Py_DECREF(seq); return NULL;
        }
        Py_ssize_t r = t->nrows;
        for (int c = 0; c < t->ncols; c++) {
            if (store_cell(&t->cols[c], r, PySequence_Fast_GET_ITEM(rseq, c)) < 0) {
                Py_DECREF(rseq); Py_DECREF(seq); return NULL;
            }
        }
        t->nrows++;
        Py_DECREF(rseq);
    }
    Py_DECREF(seq);
    Py_RETURN_NONE;
}

static PyObject *db_row_count(DatabaseObject *self, PyObject *args) {
    const char *tname;
    if (!PyArg_ParseTuple(args, "s", &tname)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError, "table '%s'", tname); return NULL; }
    return PyLong_FromSsize_t(t->nrows);
}

static void free_table_contents(Table *t) {
    for (int c = 0; c < t->ncols; c++) {
        Column *col = &t->cols[c];
        free(col->name); free(col->nulls);
        free(col->idata); free(col->fdata); free(col->bdata);
        if (col->sdata) {
            for (Py_ssize_t r = 0; r < t->nrows; r++) free(col->sdata[r]);
            free(col->sdata);
        }
    }
    free(t->cols); free(t->name);
}

/* drop_table(name): remove a table so a single table can be rebuilt inside a
 * shared engine. Silently succeeds if the table does not exist. */
static PyObject *db_drop_table(DatabaseObject *self, PyObject *args) {
    const char *tname;
    if (!PyArg_ParseTuple(args, "s", &tname)) return NULL;
    for (int i = 0; i < self->ntables; i++) {
        if (strcmp(self->tables[i].name, tname) == 0) {
            free_table_contents(&self->tables[i]);
            for (int j = i; j < self->ntables - 1; j++)
                self->tables[j] = self->tables[j + 1];
            self->ntables--;
            break;
        }
    }
    Py_RETURN_NONE;
}

/* build a selection array (indices) satisfying the plan (or all rows) */
static Py_ssize_t *select_rows(Table *t, PyObject *plan, Py_ssize_t *out_n, int *err) {
    *err = 0;
    Node *root = NULL;
    if (plan != Py_None) { root = compile_plan(t, plan); if (!root) { *err = 1; return NULL; } }
    Py_ssize_t *sel = malloc((t->nrows ? t->nrows : 1) * sizeof(Py_ssize_t));
    if (!sel) { free_node(root); PyErr_NoMemory(); *err = 1; return NULL; }
    Py_ssize_t m = 0;
    for (Py_ssize_t r = 0; r < t->nrows; r++) {
        int keep = 1;
        if (root) { keep = eval(root, t, r); if (keep < 0) { free(sel); free_node(root); *err = 1; return NULL; } }
        if (keep) sel[m++] = r;
    }
    free_node(root);
    *out_n = m;
    return sel;
}

/* filter(table, plan) -> count only (fast) */
static PyObject *db_filter_count(DatabaseObject *self, PyObject *args) {
    const char *tname; PyObject *plan;
    if (!PyArg_ParseTuple(args, "sO", &tname, &plan)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError, "table '%s'", tname); return NULL; }
    Py_ssize_t n; int err;
    Py_ssize_t *sel = select_rows(t, plan, &n, &err);
    if (err) return NULL;
    free(sel);
    return PyLong_FromSsize_t(n);
}

/* materialize(table, plan, select_names_or_None) -> list[dict] (insertion order) */
static PyObject *db_materialize(DatabaseObject *self, PyObject *args) {
    const char *tname; PyObject *plan; PyObject *selcols;
    if (!PyArg_ParseTuple(args, "sOO", &tname, &plan, &selcols)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError, "table '%s'", tname); return NULL; }

    /* resolve selected column indices */
    int *idx; int ncols_out;
    if (selcols == Py_None) {
        ncols_out = t->ncols;
        idx = malloc(ncols_out * sizeof(int));
        for (int i = 0; i < ncols_out; i++) idx[i] = i;
    } else {
        PyObject *s = PySequence_Fast(selcols, "select must be a sequence");
        if (!s) return NULL;
        ncols_out = (int)PySequence_Fast_GET_SIZE(s);
        idx = malloc(ncols_out * sizeof(int));
        for (int i = 0; i < ncols_out; i++) {
            const char *nm = PyUnicode_AsUTF8(PySequence_Fast_GET_ITEM(s, i));
            int ci = nm ? find_col(t, nm) : -1;
            if (ci < 0) { free(idx); Py_DECREF(s); PyErr_Format(PyExc_KeyError, "column '%s'", nm?nm:"?"); return NULL; }
            idx[i] = ci;
        }
        Py_DECREF(s);
    }

    Py_ssize_t n; int err;
    Py_ssize_t *sel = select_rows(t, plan, &n, &err);
    if (err) { free(idx); return NULL; }

    PyObject *out = PyList_New(n);
    if (!out) { free(idx); free(sel); return NULL; }
    for (Py_ssize_t i = 0; i < n; i++) {
        Py_ssize_t r = sel[i];
        PyObject *d = PyDict_New();
        if (!d) { free(idx); free(sel); Py_DECREF(out); return NULL; }
        for (int c = 0; c < ncols_out; c++) {
            Column *col = &t->cols[idx[c]];
            PyObject *val = load_cell(col, r);
            if (!val || PyDict_SetItemString(d, col->name, val) < 0) {
                Py_XDECREF(val); Py_DECREF(d); free(idx); free(sel); Py_DECREF(out); return NULL;
            }
            Py_DECREF(val);
        }
        PyList_SET_ITEM(out, i, d);
    }
    free(idx); free(sel);
    return out;
}

/* ---- aggregation core, shared by aggregate() and group() ---- */

typedef struct {
    int func;        /* 0 count,1 sum,2 avg,3 min,4 max */
    int col;         /* -1 for count(*) */
} Agg;

enum { AGG_COUNT=0, AGG_SUM=1, AGG_AVG=2, AGG_MIN=3, AGG_MAX=4 };

typedef struct {
    Py_ssize_t count;      /* non-null count (or group size for count(*)) */
    Py_ssize_t size;       /* group size */
    double dsum; long long isum; int int_sum;
    double dmin, dmax; long long imin, imax; int has_val; int is_int;
} AggState;

static void agg_init(AggState *s, Table *t, Agg *a) {
    memset(s, 0, sizeof(*s));
    s->is_int = (a->col >= 0 && t->cols[a->col].type == COL_INT);
    s->int_sum = s->is_int;
}

static void agg_step(AggState *s, Table *t, Agg *a, Py_ssize_t r) {
    s->size++;
    if (a->func == AGG_COUNT && a->col < 0) { return; }   /* count(*) uses size */
    Column *c = &t->cols[a->col];
    if (c->nulls[r]) return;
    s->count++;
    double v = cell_double(c, r);
    long long iv = (c->type == COL_INT) ? c->idata[r] : 0;
    switch (a->func) {
    case AGG_SUM: if (s->is_int) s->isum += iv; else s->dsum += v; break;
    case AGG_AVG: s->dsum += v; break;
    case AGG_MIN:
        if (!s->has_val || v < s->dmin) { s->dmin = v; s->imin = iv; }
        s->has_val = 1; break;
    case AGG_MAX:
        if (!s->has_val || v > s->dmax) { s->dmax = v; s->imax = iv; }
        s->has_val = 1; break;
    }
}

static PyObject *agg_result(AggState *s, Table *t, Agg *a) {
    switch (a->func) {
    case AGG_COUNT:
        return PyLong_FromSsize_t(a->col < 0 ? s->size : s->count);
    case AGG_SUM:
        if (s->count == 0) {
            /* RelPy sum of no values -> 0 (int) for int col, 0.0? Actually
               RelPy returns 0 for empty int sum. Match with None-safe 0. */
            if (s->is_int) return PyLong_FromLong(0);
            return PyFloat_FromDouble(0.0);
        }
        if (s->is_int) return PyLong_FromLongLong(s->isum);
        return PyFloat_FromDouble(s->dsum);
    case AGG_AVG:
        if (s->count == 0) Py_RETURN_NONE;
        return PyFloat_FromDouble(s->dsum / (double)s->count);
    case AGG_MIN:
        if (!s->has_val) Py_RETURN_NONE;
        if (a->col >= 0 && t->cols[a->col].type == COL_INT) return PyLong_FromLongLong(s->imin);
        return PyFloat_FromDouble(s->dmin);
    case AGG_MAX:
        if (!s->has_val) Py_RETURN_NONE;
        if (a->col >= 0 && t->cols[a->col].type == COL_INT) return PyLong_FromLongLong(s->imax);
        return PyFloat_FromDouble(s->dmax);
    }
    Py_RETURN_NONE;
}

/* parse agg spec list: [(alias, func_name, colname_or_None), ...] */
static Agg *parse_aggs(Table *t, PyObject *spec, PyObject ***aliases_out, int *nagg) {
    PyObject *s = PySequence_Fast(spec, "aggs must be a sequence");
    if (!s) return NULL;
    int n = (int)PySequence_Fast_GET_SIZE(s);
    Agg *aggs = malloc(n * sizeof(Agg));
    PyObject **aliases = malloc(n * sizeof(PyObject*));
    for (int i = 0; i < n; i++) {
        PyObject *item = PySequence_Fast_GET_ITEM(s, i);
        PyObject *alias = PyTuple_GET_ITEM(item, 0);
        const char *fn = PyUnicode_AsUTF8(PyTuple_GET_ITEM(item, 1));
        PyObject *cn = PyTuple_GET_ITEM(item, 2);
        int func;
        if      (!strcmp(fn,"count")) func = AGG_COUNT;
        else if (!strcmp(fn,"sum"))   func = AGG_SUM;
        else if (!strcmp(fn,"avg"))   func = AGG_AVG;
        else if (!strcmp(fn,"min"))   func = AGG_MIN;
        else if (!strcmp(fn,"max"))   func = AGG_MAX;
        else { PyErr_Format(PyExc_ValueError,"bad agg '%s'",fn); free(aggs);free(aliases);Py_DECREF(s);return NULL; }
        int col = -1;
        if (cn != Py_None) {
            const char *cnm = PyUnicode_AsUTF8(cn);
            col = cnm ? find_col(t, cnm) : -1;
            if (col < 0) { PyErr_Format(PyExc_KeyError,"column '%s'",cnm?cnm:"?"); free(aggs);free(aliases);Py_DECREF(s);return NULL; }
        }
        aggs[i].func = func; aggs[i].col = col;
        aliases[i] = alias;  /* borrowed */
    }
    Py_DECREF(s);
    *aliases_out = aliases; *nagg = n;
    return aggs;
}

/* aggregate(table, plan, aggs) -> dict{alias: value} */
static PyObject *db_aggregate(DatabaseObject *self, PyObject *args) {
    const char *tname; PyObject *plan, *spec;
    if (!PyArg_ParseTuple(args, "sOO", &tname, &plan, &spec)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError,"table '%s'",tname); return NULL; }
    PyObject **aliases; int nagg;
    Agg *aggs = parse_aggs(t, spec, &aliases, &nagg);
    if (!aggs) return NULL;

    Py_ssize_t n; int err;
    Py_ssize_t *sel = select_rows(t, plan, &n, &err);
    if (err) { free(aggs); free(aliases); return NULL; }

    AggState *states = malloc(nagg * sizeof(AggState));
    for (int a = 0; a < nagg; a++) agg_init(&states[a], t, &aggs[a]);
    for (Py_ssize_t i = 0; i < n; i++)
        for (int a = 0; a < nagg; a++) agg_step(&states[a], t, &aggs[a], sel[i]);

    PyObject *out = PyDict_New();
    for (int a = 0; a < nagg; a++) {
        PyObject *v = agg_result(&states[a], t, &aggs[a]);
        PyDict_SetItem(out, aliases[a], v);
        Py_DECREF(v);
    }
    free(states); free(sel); free(aggs); free(aliases);
    return out;
}

/* group(table, plan, group_names, aggs) -> list[dict] */
static PyObject *db_group(DatabaseObject *self, PyObject *args) {
    const char *tname; PyObject *plan, *gnames, *spec;
    if (!PyArg_ParseTuple(args, "sOOO", &tname, &plan, &gnames, &spec)) return NULL;
    Table *t = find_table(self, tname);
    if (!t) { PyErr_Format(PyExc_KeyError,"table '%s'",tname); return NULL; }

    /* resolve group columns */
    PyObject *gs = PySequence_Fast(gnames, "group cols must be a sequence");
    if (!gs) return NULL;
    int ng = (int)PySequence_Fast_GET_SIZE(gs);
    int *gidx = malloc(ng * sizeof(int));
    for (int i = 0; i < ng; i++) {
        const char *nm = PyUnicode_AsUTF8(PySequence_Fast_GET_ITEM(gs, i));
        int ci = nm ? find_col(t, nm) : -1;
        if (ci < 0) { PyErr_Format(PyExc_KeyError,"column '%s'",nm?nm:"?"); free(gidx); Py_DECREF(gs); return NULL; }
        gidx[i] = ci;
    }
    Py_DECREF(gs);

    PyObject **aliases; int nagg;
    Agg *aggs = parse_aggs(t, spec, &aliases, &nagg);
    if (!aggs) { free(gidx); return NULL; }

    Py_ssize_t n; int err;
    Py_ssize_t *sel = select_rows(t, plan, &n, &err);
    if (err) { free(gidx); free(aggs); free(aliases); return NULL; }

    /* Hash groups using a Python dict keyed by a tuple of group values,
       mapping to an integer group index; states held in a growable array. */
    PyObject *keymap = PyDict_New();
    AggState *states = NULL; PyObject **keys = NULL;
    Py_ssize_t ngroups = 0, gcap = 0;

    for (Py_ssize_t i = 0; i < n; i++) {
        Py_ssize_t r = sel[i];
        PyObject *key = PyTuple_New(ng);
        for (int g = 0; g < ng; g++) {
            PyObject *v = load_cell(&t->cols[gidx[g]], r);
            PyTuple_SET_ITEM(key, g, v);  /* steals */
        }
        PyObject *gi = PyDict_GetItem(keymap, key);  /* borrowed */
        Py_ssize_t g_index;
        if (gi == NULL) {
            g_index = ngroups++;
            if (ngroups > gcap) {
                gcap = gcap ? gcap*2 : 16;
                states = realloc(states, gcap * sizeof(AggState) * nagg);
                keys   = realloc(keys, gcap * sizeof(PyObject*));
            }
            for (int a = 0; a < nagg; a++) agg_init(&states[g_index*nagg + a], t, &aggs[a]);
            keys[g_index] = key;  /* keep ref */
            PyObject *gv = PyLong_FromSsize_t(g_index);
            PyDict_SetItem(keymap, key, gv);
            Py_DECREF(gv);
        } else {
            g_index = PyLong_AsSsize_t(gi);
            Py_DECREF(key);
        }
        for (int a = 0; a < nagg; a++)
            agg_step(&states[g_index*nagg + a], t, &aggs[a], r);
    }

    PyObject *out = PyList_New(ngroups);
    for (Py_ssize_t g = 0; g < ngroups; g++) {
        PyObject *d = PyDict_New();
        for (int gc = 0; gc < ng; gc++)
            PyDict_SetItemString(d, t->cols[gidx[gc]].name, PyTuple_GET_ITEM(keys[g], gc));
        for (int a = 0; a < nagg; a++) {
            PyObject *v = agg_result(&states[g*nagg + a], t, &aggs[a]);
            PyDict_SetItem(d, aliases[a], v);
            Py_DECREF(v);
        }
        PyList_SET_ITEM(out, g, d);
        Py_DECREF(keys[g]);
    }
    free(states); free(keys); free(gidx); free(sel); free(aggs); free(aliases);
    Py_DECREF(keymap);
    return out;
}

/* Build a tuple of key values for row r over the given column indices.
 * Returns NULL and sets *has_null=1 if any key column is NULL (caller skips). */
static PyObject *build_key(Table *t, int *keycols, int nk, Py_ssize_t r, int *has_null) {
    *has_null = 0;
    for (int k = 0; k < nk; k++) {
        if (t->cols[keycols[k]].nulls[r]) { *has_null = 1; return NULL; }
    }
    PyObject *key = PyTuple_New(nk);
    if (!key) return NULL;
    for (int k = 0; k < nk; k++) {
        PyObject *v = load_cell(&t->cols[keycols[k]], r);
        if (!v) { Py_DECREF(key); return NULL; }
        PyTuple_SET_ITEM(key, k, v);
    }
    return key;
}

/* resolve a list of column names to indices */
static int *resolve_cols(Table *t, PyObject *names, int *n_out) {
    PyObject *s = PySequence_Fast(names, "expected a sequence of column names");
    if (!s) return NULL;
    int n = (int)PySequence_Fast_GET_SIZE(s);
    int *idx = malloc((n ? n : 1) * sizeof(int));
    for (int i = 0; i < n; i++) {
        const char *nm = PyUnicode_AsUTF8(PySequence_Fast_GET_ITEM(s, i));
        int ci = nm ? find_col(t, nm) : -1;
        if (ci < 0) { PyErr_Format(PyExc_KeyError, "column '%s'", nm ? nm : "?"); free(idx); Py_DECREF(s); return NULL; }
        idx[i] = ci;
    }
    Py_DECREF(s);
    *n_out = n;
    return idx;
}

/* parse an output map [(src_name, out_name), ...] into parallel arrays */
static int parse_outmap(Table *t, PyObject *omap, int **src_idx, PyObject ***out_names, int *n_out) {
    PyObject *s = PySequence_Fast(omap, "outmap must be a sequence");
    if (!s) return -1;
    int n = (int)PySequence_Fast_GET_SIZE(s);
    int *idx = malloc((n ? n : 1) * sizeof(int));
    PyObject **names = malloc((n ? n : 1) * sizeof(PyObject*));
    for (int i = 0; i < n; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(s, i);
        const char *src = PyUnicode_AsUTF8(PyTuple_GET_ITEM(pair, 0));
        int ci = src ? find_col(t, src) : -1;
        if (ci < 0) { PyErr_Format(PyExc_KeyError, "column '%s'", src ? src : "?"); free(idx); free(names); Py_DECREF(s); return -1; }
        idx[i] = ci;
        names[i] = PyTuple_GET_ITEM(pair, 1);   /* borrowed */
    }
    Py_DECREF(s);
    *src_idx = idx; *out_names = names; *n_out = n;
    return 0;
}

/* hash_join(left_table, left_plan, left_keys, right_table, right_plan,
 *           right_keys, left_outmap, right_outmap) -> list[dict]
 * Inner equi-join. Rows whose join key is NULL are skipped (SQL semantics). */
static PyObject *db_hash_join(DatabaseObject *self, PyObject *args) {
    const char *lname, *rname;
    PyObject *lplan, *lkeys, *rplan, *rkeys, *lout, *rout;
    if (!PyArg_ParseTuple(args, "sOOsOOOO", &lname, &lplan, &lkeys,
                          &rname, &rplan, &rkeys, &lout, &rout))
        return NULL;
    Table *lt = find_table(self, lname);
    Table *rt = find_table(self, rname);
    if (!lt || !rt) { PyErr_SetString(PyExc_KeyError, "unknown table"); return NULL; }

    int nlk, nrk, nlo, nro;
    int *lkey = resolve_cols(lt, lkeys, &nlk); if (!lkey) return NULL;
    int *rkey = resolve_cols(rt, rkeys, &nrk); if (!rkey) { free(lkey); return NULL; }
    if (nlk != nrk) { PyErr_SetString(PyExc_ValueError, "join key count mismatch"); free(lkey); free(rkey); return NULL; }
    int *lsrc, *rsrc; PyObject **lnm, **rnm;
    if (parse_outmap(lt, lout, &lsrc, &lnm, &nlo) < 0) { free(lkey); free(rkey); return NULL; }
    if (parse_outmap(rt, rout, &rsrc, &rnm, &nro) < 0) { free(lkey); free(rkey); free(lsrc); free(lnm); return NULL; }

    /* build hash index on the (filtered) right side */
    Py_ssize_t rn; int err;
    Py_ssize_t *rsel = select_rows(rt, rplan, &rn, &err);
    if (err) { free(lkey); free(rkey); free(lsrc); free(lnm); free(rsrc); free(rnm); return NULL; }

    PyObject *index = PyDict_New();   /* key_tuple -> list[right_row_index] */
    for (Py_ssize_t i = 0; i < rn; i++) {
        Py_ssize_t r = rsel[i];
        int has_null;
        PyObject *key = build_key(rt, rkey, nrk, r, &has_null);
        if (has_null) continue;
        if (!key) goto fail_index;
        PyObject *bucket = PyDict_GetItem(index, key);
        if (bucket == NULL) {
            bucket = PyList_New(0);
            PyDict_SetItem(index, key, bucket);
            Py_DECREF(bucket);
            bucket = PyDict_GetItem(index, key);
        }
        PyObject *ri = PyLong_FromSsize_t(r);
        PyList_Append(bucket, ri);
        Py_DECREF(ri);
        Py_DECREF(key);
    }

    /* probe with the (filtered) left side */
    Py_ssize_t ln;
    Py_ssize_t *lsel = select_rows(lt, lplan, &ln, &err);
    if (err) { Py_DECREF(index); free(rsel); free(lkey); free(rkey); free(lsrc); free(lnm); free(rsrc); free(rnm); return NULL; }

    PyObject *out = PyList_New(0);
    for (Py_ssize_t i = 0; i < ln; i++) {
        Py_ssize_t lr = lsel[i];
        int has_null;
        PyObject *key = build_key(lt, lkey, nlk, lr, &has_null);
        if (has_null) continue;
        if (!key) goto fail_probe;
        PyObject *bucket = PyDict_GetItem(index, key);  /* borrowed */
        Py_DECREF(key);
        if (bucket == NULL) continue;
        Py_ssize_t nb = PyList_GET_SIZE(bucket);
        for (Py_ssize_t b = 0; b < nb; b++) {
            Py_ssize_t rr = PyLong_AsSsize_t(PyList_GET_ITEM(bucket, b));
            PyObject *d = PyDict_New();
            for (int c = 0; c < nlo; c++) {
                PyObject *v = load_cell(&lt->cols[lsrc[c]], lr);
                PyDict_SetItem(d, lnm[c], v); Py_DECREF(v);
            }
            for (int c = 0; c < nro; c++) {
                PyObject *v = load_cell(&rt->cols[rsrc[c]], rr);
                PyDict_SetItem(d, rnm[c], v); Py_DECREF(v);
            }
            PyList_Append(out, d); Py_DECREF(d);
        }
    }

    Py_DECREF(index); free(rsel); free(lsel);
    free(lkey); free(rkey); free(lsrc); free(lnm); free(rsrc); free(rnm);
    return out;

fail_probe:
    Py_DECREF(out); free(lsel);
fail_index:
    Py_DECREF(index); free(rsel);
    free(lkey); free(rkey); free(lsrc); free(lnm); free(rsrc); free(rnm);
    return NULL;
}

/* hash_join_group(lt, lplan, lkeys, rt, rplan, rkeys, group_specs, agg_specs)
 *   group_specs: list of (side:int, src_name:str, out_name:str)   side 0=left,1=right
 *   agg_specs:   list of (alias:str, func:str, side:int, src_name_or_None)
 * Inner equi-join + GROUP BY + aggregates in one pass. Returns list[dict].
 * Avoids materializing the joined rows. */
static PyObject *db_hash_join_group(DatabaseObject *self, PyObject *args) {
    const char *lname, *rname;
    PyObject *lplan, *lkeys, *rplan, *rkeys, *gspecs, *aspecs;
    if (!PyArg_ParseTuple(args, "sOOsOOOO", &lname, &lplan, &lkeys,
                          &rname, &rplan, &rkeys, &gspecs, &aspecs))
        return NULL;
    Table *lt = find_table(self, lname);
    Table *rt = find_table(self, rname);
    if (!lt || !rt) { PyErr_SetString(PyExc_KeyError, "unknown table"); return NULL; }

    int nlk, nrk;
    int *lkey = resolve_cols(lt, lkeys, &nlk); if (!lkey) return NULL;
    int *rkey = resolve_cols(rt, rkeys, &nrk); if (!rkey) { free(lkey); return NULL; }
    if (nlk != nrk) { PyErr_SetString(PyExc_ValueError, "join key count mismatch"); free(lkey); free(rkey); return NULL; }

    /* group specs */
    PyObject *gs = PySequence_Fast(gspecs, "group specs must be a sequence");
    if (!gs) { free(lkey); free(rkey); return NULL; }
    int ng = (int)PySequence_Fast_GET_SIZE(gs);
    int *g_side = malloc((ng?ng:1)*sizeof(int));
    int *g_col = malloc((ng?ng:1)*sizeof(int));
    PyObject **g_out = malloc((ng?ng:1)*sizeof(PyObject*));
    for (int i = 0; i < ng; i++) {
        PyObject *sp = PySequence_Fast_GET_ITEM(gs, i);
        int side = (int)PyLong_AsLong(PyTuple_GET_ITEM(sp, 0));
        const char *src = PyUnicode_AsUTF8(PyTuple_GET_ITEM(sp, 1));
        Table *t = side ? rt : lt;
        int ci = src ? find_col(t, src) : -1;
        if (ci < 0) { PyErr_Format(PyExc_KeyError, "column '%s'", src?src:"?"); goto gfail; }
        g_side[i] = side; g_col[i] = ci; g_out[i] = PyTuple_GET_ITEM(sp, 2);
    }
    Py_DECREF(gs); gs = NULL;

    /* agg specs -> parallel arrays */
    PyObject *as = PySequence_Fast(aspecs, "agg specs must be a sequence");
    if (!as) goto gfail2;
    int na = (int)PySequence_Fast_GET_SIZE(as);
    Agg *aggs = malloc((na?na:1)*sizeof(Agg));
    int *a_side = malloc((na?na:1)*sizeof(int));
    PyObject **a_alias = malloc((na?na:1)*sizeof(PyObject*));
    for (int i = 0; i < na; i++) {
        PyObject *sp = PySequence_Fast_GET_ITEM(as, i);
        a_alias[i] = PyTuple_GET_ITEM(sp, 0);
        const char *fn = PyUnicode_AsUTF8(PyTuple_GET_ITEM(sp, 1));
        int side = (int)PyLong_AsLong(PyTuple_GET_ITEM(sp, 2));
        PyObject *cn = PyTuple_GET_ITEM(sp, 3);
        int func;
        if (!strcmp(fn,"count")) func=AGG_COUNT; else if(!strcmp(fn,"sum")) func=AGG_SUM;
        else if(!strcmp(fn,"avg")) func=AGG_AVG; else if(!strcmp(fn,"min")) func=AGG_MIN;
        else if(!strcmp(fn,"max")) func=AGG_MAX;
        else { PyErr_Format(PyExc_ValueError,"bad agg '%s'",fn); free(aggs);free(a_side);free(a_alias);Py_DECREF(as);goto gfail2; }
        int col = -1;
        Table *t = side ? rt : lt;
        if (cn != Py_None) {
            const char *cnm = PyUnicode_AsUTF8(cn);
            col = cnm ? find_col(t, cnm) : -1;
            if (col < 0) { PyErr_Format(PyExc_KeyError,"column '%s'",cnm?cnm:"?"); free(aggs);free(a_side);free(a_alias);Py_DECREF(as);goto gfail2; }
        }
        aggs[i].func = func; aggs[i].col = col; a_side[i] = side;
    }
    Py_DECREF(as); as = NULL;

    /* build right hash index (filtered, skip null keys) */
    Py_ssize_t rn; int err;
    Py_ssize_t *rsel = select_rows(rt, rplan, &rn, &err);
    if (err) goto gfail3;
    PyObject *index = PyDict_New();
    for (Py_ssize_t i = 0; i < rn; i++) {
        Py_ssize_t r = rsel[i]; int hn;
        PyObject *key = build_key(rt, rkey, nrk, r, &hn);
        if (hn) continue; if (!key) { Py_DECREF(index); free(rsel); goto gfail3; }
        PyObject *bucket = PyDict_GetItem(index, key);
        if (!bucket) { bucket = PyList_New(0); PyDict_SetItem(index, key, bucket); Py_DECREF(bucket); bucket = PyDict_GetItem(index, key); }
        PyObject *ri = PyLong_FromSsize_t(r); PyList_Append(bucket, ri); Py_DECREF(ri);
        Py_DECREF(key);
    }

    /* probe left, group + aggregate on the fly */
    Py_ssize_t ln;
    Py_ssize_t *lsel = select_rows(lt, lplan, &ln, &err);
    if (err) { Py_DECREF(index); free(rsel); goto gfail3; }

    PyObject *keymap = PyDict_New();
    AggState *states = NULL; PyObject **gkeys = NULL;
    Py_ssize_t ngroups = 0, gcap = 0;

    for (Py_ssize_t i = 0; i < ln; i++) {
        Py_ssize_t lr = lsel[i]; int hn;
        PyObject *lk = build_key(lt, lkey, nlk, lr, &hn);
        if (hn) continue; if (!lk) goto probe_fail;
        PyObject *bucket = PyDict_GetItem(index, lk);
        Py_DECREF(lk);
        if (!bucket) continue;
        Py_ssize_t nb = PyList_GET_SIZE(bucket);
        for (Py_ssize_t b = 0; b < nb; b++) {
            Py_ssize_t rr = PyLong_AsSsize_t(PyList_GET_ITEM(bucket, b));
            /* group key from group_specs (read left or right) */
            PyObject *gkey = PyTuple_New(ng);
            for (int g = 0; g < ng; g++) {
                Table *t = g_side[g] ? rt : lt;
                Py_ssize_t row = g_side[g] ? rr : lr;
                PyObject *v = load_cell(&t->cols[g_col[g]], row);
                PyTuple_SET_ITEM(gkey, g, v);
            }
            PyObject *gi = PyDict_GetItem(keymap, gkey);
            Py_ssize_t gidx;
            if (!gi) {
                gidx = ngroups++;
                if (ngroups > gcap) {
                    gcap = gcap ? gcap*2 : 16;
                    states = realloc(states, gcap*sizeof(AggState)*(na?na:1));
                    gkeys = realloc(gkeys, gcap*sizeof(PyObject*));
                }
                for (int a = 0; a < na; a++) {
                    Table *t = a_side[a] ? rt : lt;
                    agg_init(&states[gidx*na + a], t, &aggs[a]);
                }
                gkeys[gidx] = gkey;
                PyObject *gv = PyLong_FromSsize_t(gidx);
                PyDict_SetItem(keymap, gkey, gv); Py_DECREF(gv);
            } else {
                gidx = PyLong_AsSsize_t(gi);
                Py_DECREF(gkey);
            }
            for (int a = 0; a < na; a++) {
                Table *t = a_side[a] ? rt : lt;
                Py_ssize_t row = a_side[a] ? rr : lr;
                agg_step(&states[gidx*na + a], t, &aggs[a], row);
            }
        }
    }

    PyObject *out = PyList_New(ngroups);
    for (Py_ssize_t g = 0; g < ngroups; g++) {
        PyObject *d = PyDict_New();
        for (int gc = 0; gc < ng; gc++)
            PyDict_SetItem(d, g_out[gc], PyTuple_GET_ITEM(gkeys[g], gc));
        for (int a = 0; a < na; a++) {
            Table *t = a_side[a] ? rt : lt;
            PyObject *v = agg_result(&states[g*na + a], t, &aggs[a]);
            PyDict_SetItem(d, a_alias[a], v); Py_DECREF(v);
        }
        PyList_SET_ITEM(out, g, d);
        Py_DECREF(gkeys[g]);
    }
    free(states); free(gkeys); Py_DECREF(keymap); Py_DECREF(index);
    free(lsel); free(rsel);
    free(lkey); free(rkey); free(g_side); free(g_col); free(g_out);
    free(aggs); free(a_side); free(a_alias);
    return out;

probe_fail:
    Py_DECREF(keymap); Py_DECREF(index); free(lsel); free(rsel);
    free(aggs); free(a_side); free(a_alias);
    free(lkey); free(rkey); free(g_side); free(g_col); free(g_out);
    return NULL;
gfail3:
    free(aggs); free(a_side); free(a_alias);
gfail2:
    /* fallthrough cleanup for group arrays */
    free(lkey); free(rkey); free(g_side); free(g_col); free(g_out);
    return NULL;
gfail:
    if (gs) Py_DECREF(gs);
    free(lkey); free(rkey); free(g_side); free(g_col); free(g_out);
    return NULL;
}

/* ==================== type boilerplate ================================ */

static void Database_dealloc(DatabaseObject *self) {
    for (int i = 0; i < self->ntables; i++)
        free_table_contents(&self->tables[i]);
    free(self->tables);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject *Database_new(PyTypeObject *type, PyObject *a, PyObject *k) {
    DatabaseObject *self = (DatabaseObject*)type->tp_alloc(type, 0);
    if (self) { self->tables = NULL; self->ntables = 0; self->tcap = 0; }
    return (PyObject*)self;
}

static PyMethodDef Database_methods[] = {
    {"create_table", (PyCFunction)db_create_table, METH_VARARGS, "create_table(name)"},
    {"add_column",   (PyCFunction)db_add_column,   METH_VARARGS, "add_column(table, name, typecode)"},
    {"insert_many",  (PyCFunction)db_insert_many,  METH_VARARGS, "insert_many(table, rows)"},
    {"row_count",    (PyCFunction)db_row_count,    METH_VARARGS, "row_count(table)"},
    {"drop_table",   (PyCFunction)db_drop_table,   METH_VARARGS, "drop_table(table)"},
    {"filter_count", (PyCFunction)db_filter_count, METH_VARARGS, "filter_count(table, plan)"},
    {"materialize",  (PyCFunction)db_materialize,  METH_VARARGS, "materialize(table, plan, select)"},
    {"aggregate",    (PyCFunction)db_aggregate,    METH_VARARGS, "aggregate(table, plan, aggs)"},
    {"group",        (PyCFunction)db_group,        METH_VARARGS, "group(table, plan, group_cols, aggs)"},
    {"hash_join",    (PyCFunction)db_hash_join,    METH_VARARGS, "hash_join(lt, lplan, lkeys, rt, rplan, rkeys, lout, rout)"},
    {"hash_join_group", (PyCFunction)db_hash_join_group, METH_VARARGS, "fused inner-join + group-by"},
    {NULL, NULL, 0, NULL}
};

static PyTypeObject DatabaseType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_relpyengine.Database",
    .tp_doc = "Native C columnar table store + query engine",
    .tp_basicsize = sizeof(DatabaseObject),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = Database_new,
    .tp_dealloc = (destructor)Database_dealloc,
    .tp_methods = Database_methods,
};

static struct PyModuleDef enginemodule = {
    PyModuleDef_HEAD_INIT, "_relpyengine",
    "RelPyDB native C columnar engine.", -1, NULL
};

PyMODINIT_FUNC PyInit__relpyengine(void) {
    if (PyType_Ready(&DatabaseType) < 0) return NULL;
    PyObject *m = PyModule_Create(&enginemodule);
    if (!m) return NULL;
    Py_INCREF(&DatabaseType);
    if (PyModule_AddObject(m, "Database", (PyObject*)&DatabaseType) < 0) {
        Py_DECREF(&DatabaseType); Py_DECREF(m); return NULL;
    }
    return m;
}
