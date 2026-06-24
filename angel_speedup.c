/*
 * angel_speedup.c — Angel Native Stack & O(1) Reentrancy Guard
 * =============================================================
 *
 * Two thread-local structures are maintained in sync mode:
 *
 *   native_stack[]   — Ordered push/pop array.  Preserved unchanged
 *                      because get_stack() / _tl_stack() reads it to
 *                      build the caller-inference tuple.  Removing it
 *                      would break caller attribution in send_trace.
 *
 *   node_counts[]    — Open-addressing hash map keyed on PyObject*
 *                      POINTER IDENTITY (not string content).
 *                      Python interns AST-injected string literals at
 *                      compile time, so the same node_id literal always
 *                      has the same address — pointer equality is
 *                      therefore safe and ~3x faster than PyUnicode_Compare.
 *                      Replaces the old O(N) linear scan in enter_sync,
 *                      reducing per-call overhead from O(depth) to O(1).
 *
 * Deletion strategy
 * -----------------
 * We do NOT remove individual entries when their count drops to 0.
 * Instead, the entire map is bulk-reset (memset) when native_stack_size
 * returns to 0 — i.e. at the natural request boundary in a sync worker.
 * This avoids all linear-probing deletion complexity (tombstones, backward
 * shift) while keeping the map fresh between requests.
 *
 * Fork safety
 * -----------
 * reset_thread_state() is exposed as a Python-callable so that
 * AngelTracer._handle_fork_in_child() can clear both the C-level
 * native_stack and node_counts alongside its Python-level resets.
 * Without this, a forked child inherits stale depth counts and may
 * suppress traces that should be recorded.
 */

#include <Python.h>
#include <time.h>
#include <string.h>

/* ── Ordered call stack (unchanged — required by get_stack) ──────── */
#define MAX_STACK 1024
static __thread PyObject *native_stack[MAX_STACK];
static __thread int       native_stack_size = 0;

/* ── O(1) counter map ────────────────────────────────────────────── */
/*
 * MAP_SIZE must be a power of 2.
 * Load factor stays well below 0.5 for any realistic call tree depth
 * (the number of *unique* node_ids on the stack at once is typically
 * a few dozen even in deep call chains, never anywhere near 512).
 */
#define MAP_SIZE 512
#define MAP_MASK (MAP_SIZE - 1)

typedef struct {
    PyObject *key;   /* NULL  → empty slot (sentinel) */
    int       count;
} CountEntry;

/*
 * C spec guarantees thread-local storage with static duration is
 * zero-initialised before first use, so node_counts starts all-NULL.
 */
static __thread CountEntry node_counts[MAP_SIZE];

/* ContextVar reference for async mode */
static PyObject *cv_stack = NULL;

/* ── Pointer-identity hash (Fibonacci hashing for distribution) ─── */
static inline unsigned int ptr_hash(PyObject *ptr)
{
    uintptr_t v = (uintptr_t)ptr >> 3;           /* strip tag/alignment bits */
    v = (v ^ (v >> 16)) * (uintptr_t)0x45d9f3bU; /* mix */
    return (unsigned int)(v & MAP_MASK);
}

/*
 * count_map_inc — increment depth counter for key.
 * Returns the new count (>= 1), or -1 if the map is pathologically full
 * (safe fallback: caller allows the trace).
 *
 * IMPORTANT: keys are compared by pointer identity, not by string value.
 * This is correct because node_id strings are compile-time constants
 * injected by AngelASTTransformer and interned by CPython.
 */
static int count_map_inc(PyObject *key)
{
    unsigned int h = ptr_hash(key);
    for (unsigned int i = 0; i < MAP_SIZE; i++) {
        unsigned int idx = (h + i) & MAP_MASK;
        if (node_counts[idx].key == key) {
            /* Found — increment in place. */
            return ++node_counts[idx].count;
        }
        if (node_counts[idx].key == NULL) {
            /* Empty slot — insert new entry. */
            node_counts[idx].key   = key;
            node_counts[idx].count = 1;
            return 1;
        }
        /* Collision with a different key — continue linear probe. */
    }
    return -1; /* map full — shouldn't happen; safe fallback */
}

/*
 * count_map_dec — decrement depth counter for key.
 *
 * We intentionally leave count==0 entries in place rather than removing
 * them.  The whole map is bulk-reset via memset once the stack is fully
 * empty (see exit_sync below).  This avoids linear-probing deletion
 * hazards (tombstones, backward-shift rehashing) at zero cost in practice
 * because the map is always reset at request boundaries in sync workers.
 */
static void count_map_dec(PyObject *key)
{
    unsigned int h = ptr_hash(key);
    for (unsigned int i = 0; i < MAP_SIZE; i++) {
        unsigned int idx = (h + i) & MAP_MASK;
        if (node_counts[idx].key == key) {
            if (node_counts[idx].count > 0)
                node_counts[idx].count--;
            return;
        }
        if (node_counts[idx].key == NULL)
            return; /* not found — shouldn't happen in correctly paired enter/exit */
    }
}

/* Full map reset — O(MAP_SIZE), called at stack-empty and from fork handler. */
static void count_map_reset(void)
{
    memset(node_counts, 0, sizeof(node_counts));
}

/* ── Python-callable reset (fork handler hook) ─────────────────── */
/*
 * Called from AngelTracer._handle_fork_in_child() after fork().
 * Resets BOTH C-level thread-local structures so the child process
 * starts with a clean slate — no stale depth counts inherited from
 * the parent's call tree at the moment of fork.
 */
static PyObject *reset_thread_state(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    native_stack_size = 0;
    count_map_reset();
    Py_RETURN_NONE;
}

/* ── ContextVar init ─────────────────────────────────────────────── */
static PyObject *init(PyObject *self, PyObject *args)
{
    PyObject *cv = NULL;
    if (!PyArg_ParseTuple(args, "O", &cv))
        return NULL;
    Py_XINCREF(cv);
    Py_XDECREF(cv_stack);
    cv_stack = cv;
    Py_RETURN_NONE;
}

/* ════════════════════════════════════════════════════════════════════
 * SYNC MODE — hot path, runs on every instrumented call
 * ════════════════════════════════════════════════════════════════════ */

static PyObject *enter_sync(PyObject *self, PyObject *node_id)
{
    if (native_stack_size >= MAX_STACK) {
        PyErr_SetString(PyExc_RuntimeError, "Angel stack overflow");
        return NULL;
    }

    /* 1. Push onto the ordered stack.
     *    This is still needed — get_stack() walks native_stack[] to build
     *    the caller-inference tuple consumed by send_trace. */
    native_stack[native_stack_size++] = node_id;

    /* 2. O(1) depth check via counter map.
     *    Previously: O(N) linear scan of native_stack[] counting pointer
     *    occurrences — cost grew with recursion depth (e.g. fib(20) hit
     *    avg ~10 iterations per call × 21,890 calls = ~218,900 comparisons).
     *    Now: single hash lookup + at most a few linear-probe steps. */
    int count = count_map_inc(node_id);

    /* 3. Gate: allow the first two recursive re-entries, suppress deeper ones.
     *    count < 0  → map full (safe fallback: allow the trace).
     *    count <= 2 → first or second entry of this node_id: record time.
     *    count >  2 → deeper re-entry: return None so the AST wrapper skips
     *                 the send_trace call entirely. */
    if (count < 0 || count <= 2) {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        unsigned long long ns =
            (unsigned long long)ts.tv_sec * 1000000000ULL +
            (unsigned long long)ts.tv_nsec;
        return PyLong_FromUnsignedLongLong(ns);
    }

    Py_RETURN_NONE; /* suppressed */
}

static PyObject *exit_sync(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    if (native_stack_size > 0) {
        /* Peek the top node_id BEFORE decrementing — we need it to
         * know which counter map entry to decrement. */
        PyObject *node_id = native_stack[native_stack_size - 1];
        native_stack_size--;

        count_map_dec(node_id);

        /* Bulk-reset the counter map when the stack fully unwinds.
         * In a sync Gunicorn worker every request fully unwinds its
         * call stack, so this fires at the natural request boundary.
         * It avoids entry accumulation across requests and eliminates
         * the need for per-entry deletion / tombstone logic. */
        if (native_stack_size == 0)
            count_map_reset();
    }
    Py_RETURN_NONE;
}

static PyObject *get_stack(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    if (native_stack_size > 0) {
        PyObject *t = PyTuple_New(native_stack_size);
        if (!t) return NULL;
        for (int i = 0; i < native_stack_size; i++) {
            Py_INCREF(native_stack[i]);
            PyTuple_SET_ITEM(t, i, native_stack[i]);
        }
        return t;
    }
    Py_RETURN_NONE;
}

/* ════════════════════════════════════════════════════════════════════
 * ASYNC MODE — unchanged; async tasks use the ContextVar list stack
 * (which is already task-isolated, so the counter-map optimisation
 * doesn't apply here — async keeps the O(N) scan for correctness).
 * ════════════════════════════════════════════════════════════════════ */

static PyObject *enter_async(PyObject *self, PyObject *node_id)
{
    if (!cv_stack) {
        PyErr_SetString(PyExc_RuntimeError, "cv_stack not initialized");
        return NULL;
    }

    PyObject *stack = NULL;
    if (PyContextVar_Get(cv_stack, NULL, &stack) < 0 || !stack || stack == Py_None) {
        stack = PyList_New(0);
        if (!stack) return NULL;
        if (PyContextVar_Set(cv_stack, stack) < 0) {
            Py_DECREF(stack);
            return NULL;
        }
    }

    if (PyList_Append(stack, node_id) < 0) {
        Py_DECREF(stack);
        return NULL;
    }

    Py_ssize_t size  = PyList_GET_SIZE(stack);
    Py_ssize_t count = 0;
    for (Py_ssize_t i = 0; i < size; i++) {
        PyObject *item = PyList_GET_ITEM(stack, i);
        if (item == node_id || PyUnicode_Compare(item, node_id) == 0)
            count++;
    }

    Py_DECREF(stack);

    if (count <= 2) {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        unsigned long long ns =
            (unsigned long long)ts.tv_sec * 1000000000ULL +
            (unsigned long long)ts.tv_nsec;
        return PyLong_FromUnsignedLongLong(ns);
    }
    Py_RETURN_NONE;
}

static PyObject *exit_async(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    if (!cv_stack)
        Py_RETURN_NONE;

    PyObject *stack = NULL;
    if (PyContextVar_Get(cv_stack, NULL, &stack) == 0 && stack && stack != Py_None) {
        Py_ssize_t size = PyList_GET_SIZE(stack);
        if (size > 0) {
            if (PySequence_DelItem(stack, size - 1) < 0) {
                Py_DECREF(stack);
                return NULL;
            }
        }
        Py_DECREF(stack);
    }
    Py_RETURN_NONE;
}

/* ── Method table ──────────────────────────────────────────────────── */
static PyMethodDef Methods[] = {
    {"init",               init,               METH_VARARGS, "Store ContextVar ref for async mode"},
    {"enter_sync",         enter_sync,         METH_O,       "Sync enter: O(1) depth check + timestamp"},
    {"exit_sync",          exit_sync,          METH_NOARGS,  "Sync exit: pop stack, O(1) counter dec"},
    {"get_stack",          get_stack,          METH_NOARGS,  "Return current stack as tuple (caller inference)"},
    {"enter_async",        enter_async,        METH_O,       "Async enter (ContextVar list)"},
    {"exit_async",         exit_async,         METH_NOARGS,  "Async exit (ContextVar list)"},
    {"reset_thread_state", reset_thread_state, METH_NOARGS,  "Reset C thread-locals (call from fork handler)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "angel_speedup", NULL, -1, Methods
};

PyMODINIT_FUNC PyInit_angel_speedup(void)
{
    return PyModule_Create(&module);
}
