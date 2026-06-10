
const RAW_SOURCE_BASE = "https://raw.githubusercontent.com/mandelreouven2002/RelPyDB/main/relpy/";
const RELPY_FILES = ["__init__.py","tables.py","queries.py","grouping.py","joins.py","indexes.py","persistence.py","encryption.py","exceptions.py"];

/* ─── Nav toggle ─────────────────────────────────────────── */
function initNav() {
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.querySelector('.site-nav');
  if (!toggle || !nav) return;
  toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
}

/* ─── Docs sidebar filter ────────────────────────────────── */
function initDocsFilter() {
  const input = document.querySelector('#docs-filter');
  if (!input) return;
  const links = [...document.querySelectorAll('.docs-tree a')];
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    links.forEach(a => {
      a.style.display = a.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
}

/* ─── Syntax highlighting ────────────────────────────────── */
const PY_KEYWORDS = new Set([
  'from','import','class','def','return','if','else','elif',
  'for','while','in','not','and','or','True','False','None',
  'await','async','try','except','finally','with','as','pass',
  'raise','yield','lambda','is','del'
]);
const PY_BUILTINS = new Set([
  'print','len','str','int','float','bool','list','dict','tuple',
  'set','isinstance','type','range','enumerate','zip','map','filter',
  'sorted','reversed','any','all','sum','min','max','open','input',
  'repr','hasattr','getattr','setattr'
]);
const RELPY_API = new Set([
  'RelPy','AutoNumber','col','count','sum_','avg','min_','max_',
  'AND','OR','NOT','GroupedQuery','Query'
]);

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}

function highlightPython(text) {
  let result = '';
  let i = 0;

  while (i < text.length) {
    // Comment
    if (text[i] === '#') {
      let j = i;
      while (j < text.length && text[j] !== '\n') j++;
      result += `<span class="syn-cmt">${escapeHtml(text.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    // Triple-quoted string
    const tripleDouble = text.startsWith('"""', i);
    const tripleSingle = text.startsWith("'''", i);
    if (tripleDouble || tripleSingle) {
      const q = tripleDouble ? '"""' : "'''";
      let j = i + 3;
      while (j < text.length && !text.startsWith(q, j)) j++;
      j += 3;
      result += `<span class="syn-str">${escapeHtml(text.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    // Single-quoted string
    if (text[i] === '"' || text[i] === "'") {
      const quote = text[i];
      let j = i + 1;
      while (j < text.length && text[j] !== quote && text[j] !== '\n') {
        if (text[j] === '\\') j++;
        j++;
      }
      if (j < text.length && text[j] === quote) j++;
      result += `<span class="syn-str">${escapeHtml(text.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    // Number
    if (text[i] >= '0' && text[i] <= '9') {
      let j = i;
      while (j < text.length && /[\d._]/.test(text[j])) j++;
      result += `<span class="syn-num">${escapeHtml(text.slice(i, j))}</span>`;
      i = j;
      continue;
    }

    // Identifier, keyword, or name
    if (/[a-zA-Z_]/.test(text[i])) {
      let j = i;
      while (j < text.length && /[a-zA-Z0-9_]/.test(text[j])) j++;
      const word = text.slice(i, j);
      if (PY_KEYWORDS.has(word)) {
        result += `<span class="syn-kw">${escapeHtml(word)}</span>`;
      } else if (RELPY_API.has(word)) {
        result += `<span class="syn-lib">${escapeHtml(word)}</span>`;
      } else if (PY_BUILTINS.has(word)) {
        result += `<span class="syn-builtin">${escapeHtml(word)}</span>`;
      } else {
        result += escapeHtml(word);
      }
      i = j;
      continue;
    }

    result += escapeHtml(text[i]);
    i++;
  }

  return result;
}

function initSyntaxHighlight() {
  document.querySelectorAll('pre code').forEach(el => {
    // Only highlight blocks that look like Python
    const raw = el.textContent;
    if (/\bfrom\b|\bimport\b|\bdef\b|\bdb\b|\bRelPy\b/.test(raw)) {
      el.innerHTML = highlightPython(raw);
    }
  });
}

/* ─── Copy buttons ───────────────────────────────────────── */
function initCopyButtons() {
  document.querySelectorAll('pre').forEach(pre => {
    // Don't add to the output box
    if (pre.classList.contains('output-box')) return;

    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.setAttribute('aria-label', 'Copy code');
    btn.textContent = 'Copy';

    btn.addEventListener('click', () => {
      const code = pre.querySelector('code');
      const text = code ? code.textContent : pre.textContent;
      navigator.clipboard.writeText(text.trim()).then(() => {
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }, 1800);
      }).catch(() => {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }, 1800);
      });
    });

    pre.appendChild(btn);
  });
}

/* ─── Helpers ─────────────────────────────────────────────── */
function tableHTML(rows) {
  if (!rows || !rows.length) return '<p class="muted">No rows.</p>';
  const cols = Object.keys(rows[0]);
  return `<table class="data-table"><thead><tr>${cols.map(c=>`<th>${escapeHtml(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${formatCell(r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function formatCell(v) {
  if (v === null || v === undefined) return '<span class="muted">NULL</span>';
  return escapeHtml(String(v));
}

/* ─── Join lab data & init ───────────────────────────────── */
const ordersRows = [
  { id: 101, user_id: 1, amount: 120, status: "paid" },
  { id: 102, user_id: 1, amount: 80,  status: "paid" },
  { id: 103, user_id: 2, amount: 50,  status: "pending" },
  { id: 104, user_id: 9, amount: 33,  status: "orphan" },
];
const usersRows = [
  { id: 1, name: "Alice" },
  { id: 2, name: "Bob" },
  { id: 3, name: "Dana" },
];
const joinDemos = {
  inner: { title:"INNER JOIN", code:`rows = db.query("orders").join("users", method="inner").to_list()`, sql:`SELECT *\nFROM orders\nINNER JOIN users ON orders.user_id = users.id;`, note:"Only rows that match on both sides are returned.", rows:[
    {"orders.id":101,"orders.user_id":1,"orders.amount":120,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":102,"orders.user_id":1,"orders.amount":80,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":103,"orders.user_id":2,"orders.amount":50,"orders.status":"pending","users.id":2,"users.name":"Bob"},
  ]},
  left: { title:"LEFT JOIN", code:`rows = db.query("orders").join("users", method="left").to_list()`, sql:`SELECT *\nFROM orders\nLEFT JOIN users ON orders.user_id = users.id;`, note:"All left-side rows are returned. Missing right-side values become None.", rows:[
    {"orders.id":101,"orders.user_id":1,"orders.amount":120,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":102,"orders.user_id":1,"orders.amount":80,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":103,"orders.user_id":2,"orders.amount":50,"orders.status":"pending","users.id":2,"users.name":"Bob"},
    {"orders.id":104,"orders.user_id":9,"orders.amount":33,"orders.status":"orphan","users.id":null,"users.name":null},
  ]},
  right: { title:"RIGHT JOIN", code:`rows = db.query("orders").join("users", method="right").to_list()`, sql:`SELECT *\nFROM orders\nRIGHT JOIN users ON orders.user_id = users.id;`, note:"All right-side rows are returned. Missing left-side values become None.", rows:[
    {"orders.id":101,"orders.user_id":1,"orders.amount":120,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":102,"orders.user_id":1,"orders.amount":80,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":103,"orders.user_id":2,"orders.amount":50,"orders.status":"pending","users.id":2,"users.name":"Bob"},
    {"orders.id":null,"orders.user_id":null,"orders.amount":null,"orders.status":null,"users.id":3,"users.name":"Dana"},
  ]},
  full: { title:"FULL JOIN", code:`rows = db.query("orders").join("users", method="full").to_list()`, sql:`SELECT *\nFROM orders\nFULL OUTER JOIN users ON orders.user_id = users.id;`, note:"All rows from both sides are returned.", rows:[
    {"orders.id":101,"orders.user_id":1,"orders.amount":120,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":102,"orders.user_id":1,"orders.amount":80,"orders.status":"paid","users.id":1,"users.name":"Alice"},
    {"orders.id":103,"orders.user_id":2,"orders.amount":50,"orders.status":"pending","users.id":2,"users.name":"Bob"},
    {"orders.id":104,"orders.user_id":9,"orders.amount":33,"orders.status":"orphan","users.id":null,"users.name":null},
    {"orders.id":null,"orders.user_id":null,"orders.amount":null,"orders.status":null,"users.id":3,"users.name":"Dana"},
  ]},
  cross: { title:"CROSS JOIN", code:`rows = db.query("orders").join("users", method="cross").to_list()`, sql:`SELECT *\nFROM orders\nCROSS JOIN users;`, note:"Every order is paired with every user. 4 × 3 = 12 rows.", rows: ordersRows.flatMap(o => usersRows.map(u => ({"orders.id":o.id,"orders.user_id":o.user_id,"orders.amount":o.amount,"orders.status":o.status,"users.id":u.id,"users.name":u.name})))},
  natural: { title:"NATURAL JOIN", code:`rows = db.query("employees").natural_join("departments").to_list()`, sql:`SELECT *\nFROM employees\nNATURAL JOIN departments;`, note:"Uses columns with identical names. Explicit in RelPyDB because identical column names can be risky.", rows:[
    {employee_id:1,name:"Maya",department_id:10,department_name:"Data"},
    {employee_id:2,name:"Noam",department_id:20,department_name:"Operations"},
  ]}
};
function initJoinLab() {
  const root = document.querySelector('[data-join-lab]');
  if (!root) return;
  const buttons = [...root.querySelectorAll('[data-join-kind]')];
  const target = root.querySelector('[data-join-output]');
  function render(kind) {
    const demo = joinDemos[kind];
    buttons.forEach(b => b.classList.toggle('active', b.dataset.joinKind === kind));
    target.innerHTML = `
      <div class="panel">
        <h3>${demo.title}</h3>
        <p class="muted">${demo.note}</p>
        <div class="side-by-side">
          <div class="code-panel"><div class="panel-title">RelPyDB call</div><pre><code>${escapeHtml(demo.code)}</code></pre></div>
          <div class="code-panel"><div class="panel-title">SQL equivalent</div><pre><code>${escapeHtml(demo.sql)}</code></pre></div>
        </div>
        <h3>Result table</h3>
        <div class="result-table-wrap">${tableHTML(demo.rows)}</div>
      </div>`;
    // Highlight newly inserted code
    target.querySelectorAll('pre code').forEach(el => {
      el.innerHTML = highlightPython(el.textContent);
    });
    target.querySelectorAll('pre').forEach(pre => {
      if (!pre.querySelector('.copy-btn')) {
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'Copy';
        btn.addEventListener('click', () => {
          const code = pre.querySelector('code');
          navigator.clipboard.writeText((code ? code.textContent : pre.textContent).trim()).then(() => {
            btn.textContent = 'Copied!'; btn.classList.add('copied');
            setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
          });
        });
        pre.appendChild(btn);
      }
    });
  }
  buttons.forEach(b => b.addEventListener('click', () => render(b.dataset.joinKind)));
  render('inner');
}

/* ─── Export lab ─────────────────────────────────────────── */
const exportDemos = {
  to_list: { title:'to_list', code:`# Whole table\nusers = db.to_list("users")\n\n# One column\nnames = db.to_list("users", column_name="name")\n\n# One primary-key row\nalice = db.to_list("users", where_key={"id": 1})\n\n# Decrypt encrypted columns\nplain = db.to_list("users", decrypt=True)`, output:`Whole table:\n[{"id": 1, "name": "Alice", "email": "[ENCRYPTED]"},\n {"id": 2, "name": "Bob", "email": "[ENCRYPTED]"}]\n\nOne column:\n["Alice", "Bob"]\n\nOne row:\n[{"id": 1, "name": "Alice", "email": "[ENCRYPTED]"}]`, params:[['table_name','str','required','Table to export'],['column_name','str | None','None','Return only one column as a list'],['where_key','dict | None','None','Return a primary-key row inside a list'],['decrypt','bool','False','Decrypt encrypted values']]},
  to_json: { title:'to_json', code:`# Whole table\njson_text = db.to_json("users", indent=2)\n\n# One column\nemails_json = db.to_json("users", column_name="email")\n\n# One row\nalice_json = db.to_json("users", where_key={"id": 1})`, output:`Whole table JSON:\n[\n  {"id": 1, "name": "Alice", "email": "[ENCRYPTED]"},\n  {"id": 2, "name": "Bob", "email": "[ENCRYPTED]"}\n]\n\nOne column JSON:\n["[ENCRYPTED]", "[ENCRYPTED]"]`, params:[['table_name','str','required','Table to export'],['column_name','str | None','None','Export a single column'],['where_key','dict | None','None','Export one primary-key row'],['indent','int | None','2','Pretty-print JSON'],['ensure_ascii','bool','False','Keep Unicode readable'],['decrypt','bool','False','Decrypt encrypted values']]},
  to_pandas: { title:'to_pandas', code:`# Whole table\nusers_df = db.to_pandas("users")\n\n# One column DataFrame\nemail_df = db.to_pandas("users", column_name="email", decrypt=True)\n\n# One row DataFrame\nalice_df = db.to_pandas("users", where_key={"id": 1})`, output:`Whole table:\n   id   name        email\n0   1  Alice  [ENCRYPTED]\n1   2    Bob  [ENCRYPTED]\n\nOne decrypted column:\n               email\n0  alice@example.com\n1    bob@example.com`, params:[['table_name','str','required','Table to export'],['column_name','str | None','None','Export one column DataFrame'],['where_key','dict | None','None','Export one row DataFrame'],['decrypt','bool','False','Decrypt encrypted values if key is loaded']]},
  to_numpy: { title:'to_numpy', code:`# Whole table as 2D array\narr = db.to_numpy("users")\n\n# One numeric column as 1D array\namounts = db.to_numpy("orders", column_name="amount", dtype=float)\n\n# One row as 2D array\none = db.to_numpy("users", where_key={"id": 1})`, output:`Whole table:\narray([[1, 'Alice', '[ENCRYPTED]'],\n       [2, 'Bob', '[ENCRYPTED]']], dtype=object)\n\nOne numeric column:\narray([120., 80., 50.])`, params:[['table_name','str','required','Table to export'],['column_name','str | None','None','Return 1D array for one column'],['where_key','dict | None','None','Return one row as a 2D array'],['dtype','Any | None','None','Optional NumPy dtype'],['decrypt','bool','False','Decrypt encrypted values']]},
  print_table: { title:'print_table', code:`# Default preview (3 rows)\ndb.print_table("users")\n\n# More rows\ndb.print_table("users", limit=10)\n\n# Decrypted preview\nloaded_db.print_table("users", decrypt=True)`, output:`Table: users (showing 2 of 2 rows)\n┌─────────────────┬────────────┬────────────────────────┐\n│ id (AutoNumber) │ name (str) │ email (str, encrypted) │\n├─────────────────┼────────────┼────────────────────────┤\n│ 1               │ Alice      │ [ENCRYPTED]            │\n│ 2               │ Bob        │ [ENCRYPTED]            │\n└─────────────────┴────────────┴────────────────────────┘`, params:[['table_name','str','required','Table to preview'],['limit','int','3','Number of rows to preview'],['max_width','int','24','Maximum cell width'],['decrypt','bool','False','Show plaintext if key is loaded']]},
  to_sql: { title:'to_sql', code:`# Whole table\nsql = db.to_sql("users", decrypt=True)\n\n# Specific row by primary key\nalice_sql = db.to_sql("users", where_key={"id": 1}, decrypt=True)\n\n# One supplied row\nmanual_sql = db.to_sql("users", row={"id": 3, "name": "Dana", "email": "dana@example.com"})`, output:`INSERT INTO "users" ("id", "name", "email") VALUES (1, 'Alice', 'alice@example.com');\nINSERT INTO "users" ("id", "name", "email") VALUES (2, 'Bob', 'bob@example.com');`, params:[['table_name','str','required','Target table name'],['row','dict | None','None','Export one supplied row'],['where_key','dict | None','None','Export one primary-key row'],['decrypt','bool','False','Required for tables with encrypted columns']]}
};
function initExportLab() {
  const root = document.querySelector('[data-export-lab]');
  if (!root) return;
  const buttons = [...root.querySelectorAll('[data-export-kind]')];
  const target = root.querySelector('[data-export-output]');
  function paramTable(params) {
    return `<table class="param-table"><thead><tr><th>Parameter</th><th>Type</th><th>Default</th><th>Meaning</th></tr></thead><tbody>${params.map(p=>`<tr><td><code>${escapeHtml(p[0])}</code></td><td>${escapeHtml(p[1])}</td><td>${escapeHtml(p[2])}</td><td>${escapeHtml(p[3])}</td></tr>`).join('')}</tbody></table>`;
  }
  function render(kind) {
    const d = exportDemos[kind];
    buttons.forEach(b => b.classList.toggle('active', b.dataset.exportKind === kind));
    target.innerHTML = `<div class="side-by-side"><div class="code-panel"><div class="panel-title">Function call</div><pre><code>${escapeHtml(d.code)}</code></pre></div><div class="code-panel"><div class="panel-title">Output</div><pre><code>${escapeHtml(d.output)}</code></pre></div></div><div class="panel"><h3>${d.title} parameters</h3>${paramTable(d.params)}</div>`;
    target.querySelectorAll('pre code').forEach(el => { el.innerHTML = highlightPython(el.textContent); });
    target.querySelectorAll('pre').forEach(pre => {
      if (!pre.querySelector('.copy-btn')) {
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'Copy';
        btn.addEventListener('click', () => {
          const code = pre.querySelector('code');
          navigator.clipboard.writeText((code ? code.textContent : pre.textContent).trim()).then(() => {
            btn.textContent = 'Copied!'; btn.classList.add('copied');
            setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1800);
          });
        });
        pre.appendChild(btn);
      }
    });
  }
  buttons.forEach(b => b.addEventListener('click', () => render(b.dataset.exportKind)));
  render('to_list');
}

/* ─── IDE ────────────────────────────────────────────────── */
const IDE_EXAMPLES = {
  basic: `from relpy import RelPy, AutoNumber, col\n\ndb = RelPy()\ndb.create_table("users")\ndb.add_column("users", "id", AutoNumber, is_primary_key=True)\ndb.add_column("users", "name", str, nullable=False)\n\ndb.insert("users", {"name": "Alice"})\ndb.insert("users", {"name": "Bob"})\n\nprint(db.query("users").where(col("name") == "Alice").to_list())`,
  join: `from relpy import RelPy, AutoNumber, col\n\ndb = RelPy()\ndb.create_table("users")\ndb.add_column("users", "id", AutoNumber, is_primary_key=True)\ndb.add_column("users", "name", str, nullable=False)\n\ndb.create_table("orders")\ndb.add_column("orders", "id", AutoNumber, is_primary_key=True)\ndb.add_column("orders", "user_id", int, nullable=False, references="users.id")\ndb.add_column("orders", "amount", float, nullable=False)\n\nalice = db.insert("users", {"name": "Alice"})\ndb.insert("orders", {"user_id": alice["id"], "amount": 120.0})\n\nprint(db.query("orders").join("users").to_list())`,
  group: `from relpy import RelPy, AutoNumber, col, count, sum_\n\ndb = RelPy()\ndb.create_table("orders")\ndb.add_column("orders", "id", AutoNumber, is_primary_key=True)\ndb.add_column("orders", "status", str, nullable=False)\ndb.add_column("orders", "amount", float, nullable=False)\n\ndb.insert("orders", {"status": "paid", "amount": 120.0})\ndb.insert("orders", {"status": "paid", "amount": 80.0})\ndb.insert("orders", {"status": "pending", "amount": 50.0})\n\nprint(db.query("orders").group_by("status").aggregate(order_count=count(), total=sum_("amount")).to_list())`,
  encryption: `from relpy import RelPy, AutoNumber, col\n\nkey = RelPy.generate_encryption_key()\ndb = RelPy(encryption_key=key)\ndb.create_table("users")\ndb.add_column("users", "id", AutoNumber, is_primary_key=True)\ndb.add_column("users", "email", str, nullable=False, is_encrypted=True)\n\ndb.insert("users", {"email": "alice@example.com"})\ndb.create_index("users", "email")\n\nprint(db.to_list("users"))\nprint(db.query("users").where(col("email") == "alice@example.com").to_list(decrypt=True))`,
  sql: `from relpy import RelPy, AutoNumber\n\ndb = RelPy()\ndb.create_table("users")\ndb.add_column("users", "id", AutoNumber, is_primary_key=True)\ndb.add_column("users", "name", str, nullable=False)\ndb.insert("users", {"name": "Alice"})\nprint(db.to_sql("users"))`
};
function initIDE() {
  const editor = document.querySelector('#ide-editor');
  if (!editor) return;
  const out = document.querySelector('#ide-output');
  const run = document.querySelector('#run-ide');
  const buttons = [...document.querySelectorAll('[data-example]')];
  buttons.forEach(b => b.addEventListener('click', () => {
    buttons.forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    editor.value = IDE_EXAMPLES[b.dataset.example];
  }));
  if (buttons[0]) buttons[0].click();
  run.addEventListener('click', async () => {
    out.textContent = 'Loading Pyodide and RelPyDB from GitHub…\n';
    try {
      if (!window.loadPyodide) throw new Error('Pyodide script was not loaded. Check your internet connection.');
      if (!window.pyodideReady) window.pyodideReady = loadPyodide();
      const pyodide = await window.pyodideReady;
      await pyodide.loadPackage('micropip');
      await pyodide.runPythonAsync(`
import sys, os, pathlib, micropip
os.makedirs('/home/pyodide/relpy', exist_ok=True)
sys.path.insert(0, '/home/pyodide')
try:
    import cryptography
except Exception:
    await micropip.install('cryptography')
`);
      for (const file of RELPY_FILES) {
        const res = await fetch(RAW_SOURCE_BASE + file);
        if (!res.ok) throw new Error('Could not fetch ' + file + ' from GitHub.');
        pyodide.FS.writeFile('/home/pyodide/relpy/' + file, await res.text());
      }
      pyodide.globals.set('user_code', editor.value);
      const result = await pyodide.runPythonAsync(`
import io, contextlib, traceback
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        exec(user_code, {})
except Exception:
    traceback.print_exc(file=buf)
buf.getvalue()
`);
      out.textContent = result || '(No output)';
    } catch (err) {
      out.textContent = String(err);
    }
  });
}

/* ─── Boot ───────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initNav();
  initDocsFilter();
  initSyntaxHighlight();
  initCopyButtons();
  initJoinLab();
  initExportLab();
  initIDE();
});
