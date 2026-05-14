import os
import sqlite3
import json
import requests
import subprocess
import time
import re
import base64
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'liosito_super_secret_key_pastel'

DATABASE = 'liosito.db'
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_usuario TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            github_user TEXT,
            puntos_totales INTEGER DEFAULT 0,
            skill_programacion INTEGER DEFAULT 1,
            skill_electronica INTEGER DEFAULT 1,
            skill_logistica INTEGER DEFAULT 1,
            skills_history TEXT DEFAULT '[]',
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            descripcion TEXT,
            categoria TEXT NOT NULL,
            peso INTEGER NOT NULL,
            estado TEXT DEFAULT 'pendiente',
            id_asignado INTEGER,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(id_asignado) REFERENCES usuarios(id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS historial_puntos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_usuario INTEGER NOT NULL,
            puntos_ganados INTEGER NOT NULL,
            descripcion_accion TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(id_usuario) REFERENCES usuarios(id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS conocimiento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proyecto_nombre TEXT NOT NULL,
            archivo_path TEXT,
            contenido TEXT NOT NULL,
            contenido_resumen TEXT,
            fuente TEXT NOT NULL,
            tipo TEXT DEFAULT 'general',
            fecha_indexado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS errores_frecuentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            descripcion TEXT NOT NULL,
            categoria TEXT DEFAULT 'general',
            autor TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS repositorios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT UNIQUE NOT NULL,
            id_usuario INTEGER,
            rama TEXT DEFAULT 'main',
            ultima_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(id_usuario) REFERENCES usuarios(id)
        )''')

        # Migraciones seguras
        try:
            cursor.execute("ALTER TABLE usuarios ADD COLUMN skills_history TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE conocimiento ADD COLUMN archivo_path TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE conocimiento ADD COLUMN contenido_resumen TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE conocimiento ADD COLUMN tipo TEXT DEFAULT 'general'")
        except Exception:
            pass

        db.commit()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# === AUTENTICACIÓN ===

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        nombre_usuario = request.form.get('nombre_usuario')
        password = request.form.get('password')
        db = get_db()
        user = db.execute('SELECT * FROM usuarios WHERE nombre_usuario = ?', (nombre_usuario,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['nombre_usuario'] = user['nombre_usuario']
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Credenciales incorrectas o usuario no existe.')
    return render_template('login.html')

@app.route('/register', methods=['POST'])
def register():
    nombre_usuario = request.form.get('nombre_usuario')
    password = request.form.get('password')
    github_user = request.form.get('github_user', '')
    s_prog = int(request.form.get('skill_programacion', 5) or 5)
    s_elec = int(request.form.get('skill_electronica', 3) or 3)
    s_log = int(request.form.get('skill_logistica', 4) or 4)

    db = get_db()
    if db.execute('SELECT id FROM usuarios WHERE nombre_usuario = ?', (nombre_usuario,)).fetchone():
        return render_template('login.html', error_register='Usuario ya existe')

    db.execute(
        'INSERT INTO usuarios (nombre_usuario, password_hash, github_user, skill_programacion, skill_electronica, skill_logistica) VALUES (?, ?, ?, ?, ?, ?)',
        (nombre_usuario, generate_password_hash(password), github_user, s_prog, s_elec, s_log)
    )
    db.commit()
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# === DASHBOARD ===

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user = db.execute('SELECT * FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()

    current_month = datetime.now().strftime('%Y-%m')
    top3 = db.execute('''
        SELECT u.nombre_usuario, SUM(h.puntos_ganados) as total_mes
        FROM historial_puntos h JOIN usuarios u ON u.id = h.id_usuario
        WHERE strftime('%Y-%m', h.fecha) = ?
        GROUP BY u.id ORDER BY total_mes DESC LIMIT 3
    ''', (current_month,)).fetchall()

    tareas_raw = db.execute('''
        SELECT t.*, u.nombre_usuario FROM tareas t
        LEFT JOIN usuarios u ON t.id_asignado = u.id
        ORDER BY t.estado != 'completado' DESC, t.fecha_creacion DESC
    ''').fetchall()

    historial = db.execute(
        'SELECT * FROM historial_puntos WHERE id_usuario = ? ORDER BY fecha ASC LIMIT 20',
        (session['user_id'],)
    ).fetchall()
    hist_labels = [row['fecha'].split(' ')[0] for row in historial]
    hist_data = []
    acum = 0
    for row in historial:
        acum += row['puntos_ganados']
        hist_data.append(acum)

    stats = db.execute('SELECT COUNT(id) as total_notas FROM conocimiento').fetchone()
    total_kb = stats['total_notas'] if stats else 0

    errores = db.execute(
        'SELECT * FROM errores_frecuentes ORDER BY fecha DESC LIMIT 6'
    ).fetchall()

    repos = db.execute('SELECT * FROM repositorios ORDER BY ultima_sync DESC').fetchall()
    todos_usuarios = [dict(u) for u in db.execute('SELECT id, nombre_usuario, skill_programacion, skill_electronica, skill_logistica FROM usuarios').fetchall()]

    return render_template(
        'dashboard.html',
        user=user, top3=top3, tareas=tareas_raw,
        hist_labels=hist_labels, hist_data=hist_data,
        total_kb=total_kb, errores=errores,
        repos=repos, todos_usuarios=todos_usuarios
    )

# === TAREAS ===

@app.route('/api/tarea/completar/<int:tarea_id>', methods=['POST'])
@login_required
def completar_tarea(tarea_id):
    db = get_db()
    tarea = db.execute('SELECT * FROM tareas WHERE id = ?', (tarea_id,)).fetchone()
    if not tarea:
        return jsonify({'error': 'Tarea no existe'}), 404
    if tarea['estado'] == 'completado':
        return jsonify({'error': 'Ya está completada'}), 400

    db.execute('UPDATE tareas SET estado = "completado" WHERE id = ?', (tarea_id,))
    if tarea['id_asignado']:
        db.execute('UPDATE usuarios SET puntos_totales = puntos_totales + ? WHERE id = ?',
                   (tarea['peso'], tarea['id_asignado']))
        db.execute('INSERT INTO historial_puntos (id_usuario, puntos_ganados, descripcion_accion) VALUES (?, ?, ?)',
                   (tarea['id_asignado'], tarea['peso'], f"Completó tarea: {tarea['titulo']}"))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/tarea/crear', methods=['POST'])
@login_required
def crear_tarea_manual():
    data = request.json
    titulo = data.get('titulo', '').strip()
    descripcion = data.get('descripcion', '').strip()
    categoria = data.get('categoria', 'programacion').lower()
    peso = int(data.get('peso', 3))
    id_asignado = data.get('id_asignado')

    if not titulo:
        return jsonify({'error': 'El título es obligatorio'}), 400
    if categoria not in ['programacion', 'electronica', 'logistica']:
        categoria = 'programacion'

    db = get_db()
    db.execute(
        'INSERT INTO tareas (titulo, descripcion, categoria, peso, id_asignado) VALUES (?, ?, ?, ?, ?)',
        (titulo, descripcion, categoria, peso, id_asignado if id_asignado else None)
    )
    db.commit()
    return jsonify({'success': True, 'message': 'Tarea creada correctamente'})

@app.route('/api/usuarios/lista', methods=['GET'])
@login_required
def lista_usuarios():
    db = get_db()
    usuarios = db.execute(
        'SELECT id, nombre_usuario, skill_programacion, skill_electronica, skill_logistica FROM usuarios'
    ).fetchall()
    return jsonify([dict(u) for u in usuarios])

# === SKILLS ===

@app.route('/api/usuario/skills', methods=['POST'])
@login_required
def actualizar_skills():
    data = request.json
    s_prog = max(1, min(10, int(data.get('skill_programacion', 1))))
    s_elec = max(1, min(10, int(data.get('skill_electronica', 1))))
    s_log = max(1, min(10, int(data.get('skill_logistica', 1))))

    db = get_db()
    user = db.execute('SELECT skills_history FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
    history = json.loads(user['skills_history'] or '[]')
    history.append({
        'fecha': datetime.now().strftime('%Y-%m-%d'),
        'prog': s_prog, 'elec': s_elec, 'log': s_log
    })
    # Guardar máximo últimas 20 entradas
    history = history[-20:]

    db.execute(
        'UPDATE usuarios SET skill_programacion=?, skill_electronica=?, skill_logistica=?, skills_history=? WHERE id=?',
        (s_prog, s_elec, s_log, json.dumps(history), session['user_id'])
    )
    db.commit()
    return jsonify({'success': True, 'prog': s_prog, 'elec': s_elec, 'log': s_log})

# === ERRORES FRECUENTES ===

@app.route('/api/errores', methods=['GET'])
@login_required
def get_errores():
    db = get_db()
    categoria = request.args.get('categoria')
    if categoria:
        rows = db.execute(
            'SELECT * FROM errores_frecuentes WHERE categoria=? ORDER BY fecha DESC LIMIT 20',
            (categoria,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM errores_frecuentes ORDER BY fecha DESC LIMIT 20'
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# === RAG & KNOWLEDGE BASE ===

@app.route('/api/conocimiento/github', methods=['POST'])
@login_required
def sync_github():
    data = request.json
    repo = data.get('repo', '').strip()
    pat = data.get('pat', '')
    if not repo:
        return jsonify({"error": "Repositorio no especificado"}), 400

    headers = {'Accept': 'application/vnd.github.v3+json'}
    if pat:
        headers['Authorization'] = f'token {pat}'

    try:
        # Intentar main y luego master
        url_tree = f"https://api.github.com/repos/{repo}/git/trees/main?recursive=1"
        res = requests.get(url_tree, headers=headers, timeout=15)
        rama = 'main'
        if res.status_code == 404:
            url_tree = f"https://api.github.com/repos/{repo}/git/trees/master?recursive=1"
            res = requests.get(url_tree, headers=headers, timeout=15)
            rama = 'master'

        if res.status_code != 200:
            return jsonify({"error": f"Error GitHub: {res.json().get('message', res.status_code)}"}), 400

        valid_exts = ('.py', '.ino', '.cpp', '.h', '.sql', '.js', '.html', '.md', '.txt', '.json')
        archivos_importados = 0
        db = get_db()

        # Borrar versión anterior del mismo repo para evitar duplicados
        db.execute('DELETE FROM conocimiento WHERE proyecto_nombre = ? AND fuente = "github"', (repo,))

        for item in res.json().get('tree', []):
            if item['type'] == 'blob' and item['path'].endswith(valid_exts):
                raw_url = f"https://raw.githubusercontent.com/{repo}/{rama}/{item['path']}"
                file_res = requests.get(raw_url, headers=headers, timeout=10)
                if file_res.status_code == 200:
                    contenido_completo = file_res.text
                    resumen = f"Archivo: {item['path']} | Proyecto: {repo}\n{contenido_completo[:300]}"
                    texto_completo = f"Archivo: {item['path']}, Proyecto: {repo}\n{contenido_completo}"
                    db.execute(
                        'INSERT INTO conocimiento (proyecto_nombre, archivo_path, contenido, contenido_resumen, fuente, tipo) VALUES (?, ?, ?, ?, ?, ?)',
                        (repo, item['path'], texto_completo, resumen, 'github', 'codigo')
                    )
                    archivos_importados += 1

        # Guardar/actualizar repo en tabla repositorios
        db.execute('''
            INSERT INTO repositorios (repo, id_usuario, rama, ultima_sync)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo) DO UPDATE SET ultima_sync=CURRENT_TIMESTAMP, rama=excluded.rama
        ''', (repo, session['user_id'], rama))

        db.commit()
        return jsonify({"success": True, "importados": archivos_importados, "rama": rama})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/conocimiento/resync/<path:repo>', methods=['POST'])
@login_required
def resync_github(repo):
    """Re-sincroniza un repo existente pidiendo el PAT de nuevo."""
    data = request.json or {}
    pat = data.get('pat', '')
    # Reutiliza la lógica de sync_github
    from flask import current_app
    with current_app.test_request_context(json={'repo': repo, 'pat': pat}):
        pass
    # Llamada directa a la lógica
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if pat:
        headers['Authorization'] = f'token {pat}'
    try:
        url_tree = f"https://api.github.com/repos/{repo}/git/trees/main?recursive=1"
        res = requests.get(url_tree, headers=headers, timeout=15)
        rama = 'main'
        if res.status_code == 404:
            url_tree = f"https://api.github.com/repos/{repo}/git/trees/master?recursive=1"
            res = requests.get(url_tree, headers=headers, timeout=15)
            rama = 'master'
        if res.status_code != 200:
            return jsonify({"error": f"Error GitHub: {res.json().get('message', res.status_code)}"}), 400

        valid_exts = ('.py', '.ino', '.cpp', '.h', '.sql', '.js', '.html', '.md', '.txt', '.json')
        archivos_importados = 0
        db = get_db()
        db.execute('DELETE FROM conocimiento WHERE proyecto_nombre = ? AND fuente = "github"', (repo,))

        for item in res.json().get('tree', []):
            if item['type'] == 'blob' and item['path'].endswith(valid_exts):
                raw_url = f"https://raw.githubusercontent.com/{repo}/{rama}/{item['path']}"
                file_res = requests.get(raw_url, headers=headers, timeout=10)
                if file_res.status_code == 200:
                    contenido_completo = file_res.text
                    resumen = f"Archivo: {item['path']} | Proyecto: {repo}\n{contenido_completo[:300]}"
                    texto_completo = f"Archivo: {item['path']}, Proyecto: {repo}\n{contenido_completo}"
                    db.execute(
                        'INSERT INTO conocimiento (proyecto_nombre, archivo_path, contenido, contenido_resumen, fuente, tipo) VALUES (?, ?, ?, ?, ?, ?)',
                        (repo, item['path'], texto_completo, resumen, 'github', 'codigo')
                    )
                    archivos_importados += 1

        db.execute('''
            INSERT INTO repositorios (repo, id_usuario, rama, ultima_sync)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo) DO UPDATE SET ultima_sync=CURRENT_TIMESTAMP, rama=excluded.rama
        ''', (repo, session['user_id'], rama))
        db.commit()
        return jsonify({"success": True, "importados": archivos_importados})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/conocimiento/manual', methods=['POST'])
@login_required
def add_manual():
    data = request.json
    titulo = data.get('titulo', 'Nota Manual')
    contenido = data.get('contenido', '')
    es_error = data.get('es_error', False)
    categoria = data.get('categoria', 'general')

    if not contenido:
        return jsonify({"error": "Contenido vacío"}), 400

    autor = session.get('nombre_usuario', 'Anónimo')
    prefijo = "LOG DE ERROR COMÚN:" if es_error else "NOTA TÉCNICA:"
    texto_ingresado = f"{prefijo} {titulo}. Añadido por: {autor}. Detalle: {contenido}"
    resumen = f"{prefijo} {titulo} | {contenido[:200]}"

    db = get_db()
    tipo = 'error' if es_error else 'manual'
    db.execute(
        'INSERT INTO conocimiento (proyecto_nombre, contenido, contenido_resumen, fuente, tipo) VALUES (?, ?, ?, ?, ?)',
        (f"Wiki-{autor}", texto_ingresado, resumen, 'manual', tipo)
    )

    if es_error:
        db.execute(
            'INSERT INTO errores_frecuentes (titulo, descripcion, categoria, autor) VALUES (?, ?, ?, ?)',
            (titulo, contenido, categoria, autor)
        )

    db.commit()
    return jsonify({"success": True, "message": "Conocimiento añadido a Liosito."})

# === CHATBOT RAG MEJORADO ===

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    mensaje_usuario = request.json.get('mensaje', '').strip()
    db = get_db()

    # 1. Buscar en errores frecuentes primero (alta prioridad)
    errores_ctx = ""
    palabras = [p for p in mensaje_usuario.split() if len(p) > 3]
    if palabras:
        query_e = " OR ".join(["titulo LIKE ? OR descripcion LIKE ?"] * len(palabras))
        params_e = []
        for p in palabras:
            params_e += [f"%{p}%", f"%{p}%"]
        rows_e = db.execute(
            f"SELECT titulo, descripcion, autor FROM errores_frecuentes WHERE {query_e} LIMIT 3",
            params_e
        ).fetchall()
        if rows_e:
            errores_ctx = "ERRORES/CONSEJOS SENIOR:\n" + "\n".join(
                [f"- [{r['titulo']}] (por {r['autor']}): {r['descripcion'][:200]}" for r in rows_e]
            )

    # 2. Buscar en conocimiento (código/notas)
    codigo_ctx = ""
    if palabras:
        query_k = " OR ".join(["contenido_resumen LIKE ? OR archivo_path LIKE ?"] * len(palabras))
        params_k = []
        for p in palabras:
            params_k += [f"%{p}%", f"%{p}%"]
        rows_k = db.execute(
            f"SELECT contenido_resumen, archivo_path FROM conocimiento WHERE {query_k} LIMIT 3",
            params_k
        ).fetchall()
        if rows_k:
            codigo_ctx = "CÓDIGO INDEXADO:\n" + "\n---\n".join(
                [f"[{r['archivo_path'] or 'nota'}]: {(r['contenido_resumen'] or '')[:400]}" for r in rows_k]
            )

    contexto_final = "\n\n".join(filter(None, [errores_ctx, codigo_ctx])) or "Sin contexto previo."

    prompt = f"""Eres LIOSITO, PM y oráculo técnico del equipo LIA. Responde en español, de forma concisa y útil.

CONTEXTO:
{contexto_final}

REGLAS:
- Si te piden una TAREA, genera un bloque ```json con {{"tareas":[{{"titulo","descripcion","peso","categoria"}}]}}
- Si piden tareas para varios, divide en subtareas (programacion/electronica/logistica)
- Cita fuentes del contexto cuando sea relevante
- Sé directo, no repitas el contexto en tu respuesta

Mensaje: "{mensaje_usuario}"
"""

    try:
        if GEMINI_API_KEY:
            # Usar Gemini Flash (rápido)
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600}
            }
            r = requests.post(GEMINI_URL, json=payload, timeout=30)
            r.raise_for_status()
            respuesta_total = r.json()['candidates'][0]['content']['parts'][0]['text']
        else:
            # Fallback: Ollama local
            OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate')
            r = requests.post(OLLAMA_URL, json={
                "model": "llama3.2:3b",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 500}
            }, timeout=60)
            respuesta_total = r.json().get('response', '')

        tareas_creadas = 0
        match = re.search(r'```json\s*(.*?)\s*```', respuesta_total, re.DOTALL)
        respuesta_limpia = respuesta_total

        if match:
            try:
                data_json = json.loads(match.group(1))
                for t in data_json.get("tareas", []):
                    cat = str(t.get('categoria', 'programacion')).lower()
                    col = f"skill_{cat if cat in ['programacion', 'electronica', 'logistica'] else 'programacion'}"
                    mejor = db.execute(f'SELECT id FROM usuarios ORDER BY {col} DESC LIMIT 1').fetchone()
                    db.execute(
                        'INSERT INTO tareas (titulo, descripcion, categoria, peso, id_asignado) VALUES (?, ?, ?, ?, ?)',
                        (t.get('titulo', 'Tarea'), t.get('descripcion', ''), cat, t.get('peso', 3),
                         mejor['id'] if mejor else None)
                    )
                db.commit()
                tareas_creadas = len(data_json.get("tareas", []))
                respuesta_limpia = respuesta_total.replace(
                    match.group(0),
                    f"\n\n🐻 **He organizado {tareas_creadas} tareas en el tablero.**"
                )
            except Exception as e:
                print(f"Error parseando JSON: {e}")

        return jsonify({"respuesta": respuesta_limpia, "tareas_creadas": tareas_creadas})
    except Exception as e:
        return jsonify({"respuesta": f"Error de conexión con la IA: {e}"})

# === REPOS ===

@app.route('/api/repositorios', methods=['GET'])
@login_required
def get_repositorios():
    db = get_db()
    repos = db.execute('SELECT * FROM repositorios ORDER BY ultima_sync DESC').fetchall()
    return jsonify([dict(r) for r in repos])

def start_ollama_if_needed():
    try:
        r = requests.get('http://localhost:11434/', timeout=2)
    except requests.exceptions.ConnectionError:
        print("🚀 Servidor Ollama inactivo. Iniciando daemon localmente...")
        subprocess.Popen(['ollama', 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3) 

if __name__ == '__main__':
    ini_port = int(os.environ.get('PORT', 5000))
    init_db()
    start_ollama_if_needed()
    app.run(debug=True, port=ini_port)