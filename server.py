from flask import Flask, request, jsonify, Response, send_from_directory, session, redirect
from flask_cors import CORS
import json, queue, threading, hashlib, os
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-local-apenas')
app.permanent_session_lifetime = timedelta(hours=8)
CORS(app)

# ── Credenciais admin via env var ────────────────────────────
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
_raw_pass  = os.environ.get('ADMIN_PASS', 'inss2024')
ADMIN_PASS = hashlib.sha256(_raw_pass.encode()).hexdigest()

# ── Banco: Supabase (PostgreSQL) ou SQLite local ─────────────
DATABASE_URL = os.environ.get('DATABASE_URL')  # seta no Render

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    USE_PG = True
    print('✓ Usando PostgreSQL (Supabase)')
else:
    import sqlite3
    USE_PG = False
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    SQLITE_DB = os.path.join(BASE_DIR, 'data.db')
    print('✓ Usando SQLite local')

_subs = []
_lock = threading.Lock()

# ── Conexão ──────────────────────────────────────────────────
def get_conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    else:
        c = sqlite3.connect(SQLITE_DB)
        c.row_factory = sqlite3.Row
        return c

def fetchall(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    if USE_PG:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]

def fetchone(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    if USE_PG:
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    return dict(row)

def execute(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    lid = None
    if USE_PG:
        if cur.description:
            lid = cur.fetchone()[0]
    else:
        lid = cur.lastrowid
    conn.commit()
    return lid

def scalar(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchone()[0]

def ph(n=1):
    """Placeholder: %s para PG, ? para SQLite"""
    return '%s' if USE_PG else '?'

def phs(*args):
    p = '%s' if USE_PG else '?'
    return ','.join([p]*len(args))

def init_db():
    conn = get_conn()
    cur  = conn.cursor()
    if USE_PG:
        cur.execute('''CREATE TABLE IF NOT EXISTS cadastros (
            id SERIAL PRIMARY KEY,
            nome TEXT, cpf TEXT, telefone TEXT,
            tipo TEXT, mensagem TEXT, ip TEXT, criado_em TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS visitas (
            id SERIAL PRIMARY KEY,
            ip TEXT, user_agent TEXT, pagina TEXT, criado_em TEXT
        )''')
    else:
        cur.execute('''CREATE TABLE IF NOT EXISTS cadastros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT, cpf TEXT, telefone TEXT,
            tipo TEXT, mensagem TEXT, ip TEXT, criado_em TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS visitas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT, user_agent TEXT, pagina TEXT, criado_em TEXT
        )''')
    conn.commit()
    conn.close()

# ── SSE ──────────────────────────────────────────────────────
def push(tipo, dados):
    msg = json.dumps({'type': tipo, 'data': dados})
    with _lock:
        mortos = []
        for q in _subs:
            try: q.put_nowait(msg)
            except: mortos.append(q)
        for q in mortos: _subs.remove(q)

def hoje_str():
    return datetime.now().strftime('%Y-%m-%d')

# ── Auth ─────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET'])
def login():
    return send_from_directory('.', 'login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/api/login', methods=['POST'])
def api_login():
    d    = request.json or {}
    user = d.get('usuario', '').strip()
    pwd  = hashlib.sha256(d.get('senha', '').encode()).hexdigest()
    if user == ADMIN_USER and pwd == ADMIN_PASS:
        session.permanent  = True
        session['admin_ok']   = True
        session['admin_user'] = user
        return jsonify(ok=True)
    return jsonify(ok=False, erro='Usuário ou senha incorretos.'), 401

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify(ok=True)

@app.route('/api/me')
def me():
    if session.get('admin_ok'):
        return jsonify(ok=True, user=session.get('admin_user'))
    return jsonify(ok=False), 401

# ── Páginas ──────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/admin')
@login_required
def admin():
    return send_from_directory('.', 'admin.html')

# ── API pública ──────────────────────────────────────────────
@app.route('/api/visita', methods=['POST'])
def visita():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ua = request.headers.get('User-Agent', '')[:200]
    pg = (request.json or {}).get('pagina', '/')
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    p  = ph()
    conn = get_conn()
    execute(conn, f'INSERT INTO visitas (ip,user_agent,pagina,criado_em) VALUES({p},{p},{p},{p})', (ip,ua,pg,ts))
    hoje  = scalar(conn, f"SELECT COUNT(*) FROM visitas WHERE criado_em LIKE {p}", (hoje_str()+'%',))
    total = scalar(conn, "SELECT COUNT(*) FROM visitas")
    conn.close()
    push('visita', {'ip': ip, 'pagina': pg, 'criado_em': ts, 'hoje': hoje, 'total': total})
    return jsonify(ok=True)

@app.route('/api/contato', methods=['POST'])
def contato():
    d  = request.json or {}
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    p  = ph()
    conn = get_conn()
    if USE_PG:
        lid = execute(conn,
            f'INSERT INTO cadastros (nome,cpf,telefone,tipo,mensagem,ip,criado_em) VALUES({p},{p},{p},{p},{p},{p},{p}) RETURNING id',
            (d.get('nome',''), d.get('cpf',''), d.get('telefone',''),
             d.get('tipo',''), d.get('mensagem',''), ip, ts))
    else:
        lid = execute(conn,
            f'INSERT INTO cadastros (nome,cpf,telefone,tipo,mensagem,ip,criado_em) VALUES({p},{p},{p},{p},{p},{p},{p})',
            (d.get('nome',''), d.get('cpf',''), d.get('telefone',''),
             d.get('tipo',''), d.get('mensagem',''), ip, ts))
    conn.close()
    push('cadastro', {'id': lid, 'nome': d.get('nome',''), 'cpf': d.get('cpf',''),
                      'telefone': d.get('telefone',''), 'tipo': d.get('tipo',''),
                      'mensagem': d.get('mensagem',''), 'ip': ip, 'criado_em': ts})
    return jsonify(ok=True, id=lid)

# ── API admin (protegida) ────────────────────────────────────
@app.route('/api/admin/dados')
@login_required
def dados():
    p    = ph()
    conn = get_conn()
    cads = fetchall(conn, 'SELECT * FROM cadastros ORDER BY id DESC LIMIT 200')
    vis  = fetchall(conn, 'SELECT * FROM visitas  ORDER BY id DESC LIMIT 50')
    tv   = scalar(conn, "SELECT COUNT(*) FROM visitas")
    vh   = scalar(conn, f"SELECT COUNT(*) FROM visitas  WHERE criado_em LIKE {p}", (hoje_str()+'%',))
    tc   = scalar(conn, "SELECT COUNT(*) FROM cadastros")
    ch   = scalar(conn, f"SELECT COUNT(*) FROM cadastros WHERE criado_em LIKE {p}", (hoje_str()+'%',))
    conn.close()
    return jsonify(cadastros=cads, visitas_recentes=vis,
                   stats=dict(total_visitas=tv, visitas_hoje=vh, total_cadastros=tc, cadastros_hoje=ch))

# ── SSE (protegida) ──────────────────────────────────────────
@app.route('/api/stream')
@login_required
def stream():
    q = queue.Queue(maxsize=50)
    with _lock: _subs.append(q)
    def gen():
        yield 'data: {"type":"ping"}\n\n'
        while True:
            try:    msg = q.get(timeout=25); yield f'data: {msg}\n\n'
            except queue.Empty: yield 'data: {"type":"ping"}\n\n'
    r = Response(gen(), mimetype='text/event-stream')
    r.headers['Cache-Control']      = 'no-cache'
    r.headers['X-Accel-Buffering']  = 'no'
    return r

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5500))
    print(f'✓  Site:  http://0.0.0.0:{port}')
    print(f'✓  Admin: http://0.0.0.0:{port}/admin')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
