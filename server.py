from flask import Flask, request, jsonify, Response, send_from_directory, session, redirect
from flask_cors import CORS
import sqlite3, json, queue, threading, hashlib, os
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)

# SECRET_KEY fixo via env var — obrigatório no Render para sessões não expirarem no restart
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-local-apenas')
app.permanent_session_lifetime = timedelta(hours=8)
CORS(app)

# ── Credenciais via env var (configure no Render) ────────────
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
_raw_pass  = os.environ.get('ADMIN_PASS', 'inss2024')
ADMIN_PASS = hashlib.sha256(_raw_pass.encode()).hexdigest()

# ── Pasta do banco — /data se existir (Render disco persistente), senão pasta local ─
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_data_env = os.environ.get('DATA_DIR', '/data')
if os.path.isdir(_data_env) and os.access(_data_env, os.W_OK):
    DATA_DIR = _data_env
else:
    DATA_DIR = BASE_DIR

DB   = os.path.join(DATA_DIR, 'data.db')
_subs = []
_lock = threading.Lock()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS cadastros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT, cpf TEXT, telefone TEXT,
            tipo TEXT, mensagem TEXT, ip TEXT, criado_em TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS visitas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT, user_agent TEXT, pagina TEXT, criado_em TEXT
        )''')

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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

# ── Auth ────────────────────────────────────────────────────
@app.route('/login', methods=['GET','POST'])
def login():
    erro = ''
    if request.method == 'POST':
        user = request.form.get('usuario','').strip()
        pwd  = hashlib.sha256(request.form.get('senha','').encode()).hexdigest()
        if user == ADMIN_USER and pwd == ADMIN_PASS:
            session.permanent = True
            session['admin_ok'] = True
            session['admin_user'] = user
            return redirect('/admin')
        erro = 'Usuário ou senha incorretos.'
    return send_from_directory('.', 'login.html') if request.method == 'GET' and not erro else (
        send_from_directory('.', 'login.html'), 200
    )

@app.route('/login', methods=['POST'])
def login_post():
    pass  # handled above

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/api/login', methods=['POST'])
def api_login():
    d    = request.json or {}
    user = d.get('usuario','').strip()
    pwd  = hashlib.sha256(d.get('senha','').encode()).hexdigest()
    if user == ADMIN_USER and pwd == ADMIN_PASS:
        session.permanent = True
        session['admin_ok'] = True
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
    with db() as c:
        c.execute('INSERT INTO visitas (ip,user_agent,pagina,criado_em) VALUES(?,?,?,?)', (ip,ua,pg,ts))
        hoje  = c.execute("SELECT COUNT(*) FROM visitas WHERE criado_em LIKE ?", (hoje_str()+'%',)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM visitas").fetchone()[0]
    push('visita', {'ip': ip, 'pagina': pg, 'criado_em': ts, 'hoje': hoje, 'total': total})
    return jsonify(ok=True)

@app.route('/api/contato', methods=['POST'])
def contato():
    d  = request.json or {}
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with db() as c:
        c.execute('INSERT INTO cadastros (nome,cpf,telefone,tipo,mensagem,ip,criado_em) VALUES(?,?,?,?,?,?,?)',
                  (d.get('nome',''), d.get('cpf',''), d.get('telefone',''),
                   d.get('tipo',''), d.get('mensagem',''), ip, ts))
        lid = c.lastrowid
    push('cadastro', {'id': lid, 'nome': d.get('nome',''), 'cpf': d.get('cpf',''),
                      'telefone': d.get('telefone',''), 'tipo': d.get('tipo',''),
                      'mensagem': d.get('mensagem',''), 'ip': ip, 'criado_em': ts})
    return jsonify(ok=True, id=lid)

# ── API admin (protegida) ────────────────────────────────────
@app.route('/api/admin/dados')
@login_required
def dados():
    with db() as c:
        cads = [dict(r) for r in c.execute('SELECT * FROM cadastros ORDER BY id DESC LIMIT 200').fetchall()]
        vis  = [dict(r) for r in c.execute('SELECT * FROM visitas  ORDER BY id DESC LIMIT 50').fetchall()]
        tv   = c.execute("SELECT COUNT(*) FROM visitas").fetchone()[0]
        vh   = c.execute("SELECT COUNT(*) FROM visitas  WHERE criado_em LIKE ?", (hoje_str()+'%',)).fetchone()[0]
        tc   = c.execute("SELECT COUNT(*) FROM cadastros").fetchone()[0]
        ch   = c.execute("SELECT COUNT(*) FROM cadastros WHERE criado_em LIKE ?", (hoje_str()+'%',)).fetchone()[0]
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
    r.headers['Cache-Control'] = 'no-cache'
    r.headers['X-Accel-Buffering'] = 'no'
    return r

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5500))
    host = '0.0.0.0'
    print(f'✓  Site:  http://{host}:{port}')
    print(f'✓  Admin: http://{host}:{port}/admin')
    print(f'✓  Login: {ADMIN_USER} / (senha via env ADMIN_PASS)')
    app.run(host=host, port=port, debug=False, threaded=True)
