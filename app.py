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
# Secret key for sessions
app.secret_key = 'liosito_super_secret_key_pastel'

DATABASE = 'liosito.db'
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate')

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
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_usuario TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            github_user TEXT,
            puntos_totales INTEGER DEFAULT 0,
            skill_programacion INTEGER DEFAULT 1,
            skill_electronica INTEGER DEFAULT 1,
            skill_logistica INTEGER DEFAULT 1,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            descripcion TEXT,
            categoria TEXT NOT NULL,
            peso INTEGER NOT NULL,
            estado TEXT DEFAULT 'pendiente',
            id_asignado INTEGER,
            fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(id_asignado) REFERENCES usuarios(id)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS historial_puntos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_usuario INTEGER NOT NULL,
            puntos_ganados INTEGER NOT NULL,
            descripcion_accion TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(id_usuario) REFERENCES usuarios(id)
        )
        ''')

        # Tabla de CONOCIMIENTO (RAG)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS conocimiento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proyecto_nombre TEXT NOT NULL,
            contenido TEXT NOT NULL,
            fuente TEXT NOT NULL,
            fecha_indexado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        db.commit()

# Decorator para requerir login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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
    
    db.execute('''
        INSERT INTO usuarios (nombre_usuario, password_hash, github_user, skill_programacion, skill_electronica, skill_logistica)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (nombre_usuario, generate_password_hash(password), github_user, s_prog, s_elec, s_log))
    db.commit()
    
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user = db.execute('SELECT * FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
    
    current_month = datetime.now().strftime('%Y-%m')
    top3_query = '''
    SELECT u.nombre_usuario, SUM(h.puntos_ganados) as total_mes
    FROM historial_puntos h
    JOIN usuarios u ON u.id = h.id_usuario
    WHERE strftime('%Y-%m', h.fecha) = ?
    GROUP BY u.id
    ORDER BY total_mes DESC
    LIMIT 3
    '''
    top3 = db.execute(top3_query, (current_month,)).fetchall()
    
    tareas_raw = db.execute('''
        SELECT t.*, u.nombre_usuario 
        FROM tareas t
        LEFT JOIN usuarios u ON t.id_asignado = u.id
        ORDER BY t.estado != 'completado' DESC, t.fecha_creacion DESC
    ''').fetchall()
    
    historial = db.execute('SELECT * FROM historial_puntos WHERE id_usuario = ? ORDER BY fecha ASC LIMIT 20', (session['user_id'],)).fetchall()
    hist_labels = [row['fecha'].split(' ')[0] for row in historial]
    hist_data = []
    acum = 0
    for row in historial:
        acum += row['puntos_ganados']
        hist_data.append(acum)

    # Revisar cantidad de conocimiento para indicador visual simple (opcional)
    stats_conocimiento_raw = db.execute('SELECT COUNT(id) as total_notas FROM conocimiento').fetchone()
    total_kb = stats_conocimiento_raw['total_notas'] if stats_conocimiento_raw else 0

    return render_template('dashboard.html', user=user, top3=top3, tareas=tareas_raw, hist_labels=hist_labels, hist_data=hist_data, total_kb=total_kb)

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
        db.execute('UPDATE usuarios SET puntos_totales = puntos_totales + ? WHERE id = ?', (tarea['peso'], tarea['id_asignado']))
        db.execute('INSERT INTO historial_puntos (id_usuario, puntos_ganados, descripcion_accion) VALUES (?, ?, ?)',
                   (tarea['id_asignado'], tarea['peso'], f"Completó tarea: {tarea['titulo']}"))
    db.commit()
    return jsonify({'success': True})

# === RUTAS RAG & KNOWLEDGE BASE ===

@app.route('/api/conocimiento/github', methods=['POST'])
@login_required
def sync_github():
    data = request.json
    repo = data.get('repo')
    pat = data.get('pat', '')

    if not repo:
        return jsonify({"error": "Repositorio no especificado"}), 400

    headers = {'Accept': 'application/vnd.github.v3+json'}
    if pat:
        headers['Authorization'] = f'token {pat}'

    try:
        # 1. Obtener main tree
        url_tree = f"https://api.github.com/repos/{repo}/git/trees/main?recursive=1"
        res = requests.get(url_tree, headers=headers)
        
        # Fallback a master si no existe main
        if res.status_code == 404:
            url_tree = f"https://api.github.com/repos/{repo}/git/trees/master?recursive=1"
            res = requests.get(url_tree, headers=headers)

        if res.status_code != 200:
            return jsonify({"error": f"Error de GitHub: {res.json().get('message', res.status_code)}"}), 400

        tree = res.json().get('tree', [])
        valid_exts = ('.py', '.ino', '.cpp', '.h', '.sql', '.js', '.html')
        
        archivos_importados = 0
        db = get_db()

        for item in tree:
            if item['type'] == 'blob' and item['path'].endswith(valid_exts):
                # Descargar contedido Raw
                raw_url = f"https://raw.githubusercontent.com/{repo}/main/{item['path']}"
                file_res = requests.get(raw_url, headers=headers)
                
                # Fallback master
                if file_res.status_code == 404:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/master/{item['path']}"
                    file_res = requests.get(raw_url, headers=headers)

                if file_res.status_code == 200:
                    contenido_texto = file_res.text
                    texto_completo = f"Archivo: {item['path']}, Proyecto: {repo}\n{contenido_texto}"
                    
                    db.execute('''
                        INSERT INTO conocimiento (proyecto_nombre, contenido, fuente) 
                        VALUES (?, ?, ?)
                    ''', (repo, texto_completo, 'github'))
                    archivos_importados += 1
        
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

    if not contenido:
        return jsonify({"error": "Contenido vacío"}), 400

    prefijo = "LOG DE ERROR COMÚN:" if es_error else "NOTA TÉCNICA:"
    autor = session.get('nombre_usuario', 'Anónimo')
    
    texto_ingresado = f"{prefijo} {titulo}. Añadido por: {autor}. Detalle: {contenido}"

    db = get_db()
    db.execute('INSERT INTO conocimiento (proyecto_nombre, contenido, fuente) VALUES (?, ?, ?)',
               (f"Wiki-{autor}", texto_ingresado, 'manual'))
    db.commit()
    
    return jsonify({"success": True, "message": "Conocimiento añadido a Liosito."})

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.json
    mensaje_usuario = data.get('mensaje', '')

    db = get_db()
    
    # 1. Búsqueda RAG simple
    # Ignorar palabras cortas y stopwords simples en español
    stopwords = {'el', 'la', 'los', 'las', 'un', 'una', 'y', 'o', 'en', 'de', 'que', 'a', 'por', 'para', 'con', 'sin', 'como', 'queremos', 'necesito'}
    palabras = [p.lower() for p in re.findall(r'\b\w+\b', mensaje_usuario) if p.lower() not in stopwords and len(p) > 2]
    
    contexto_encontrado = []
    
    if palabras:
        query_conditions = " OR ".join(["contenido LIKE ?"] * len(palabras))
        params = [f"%{p}%" for p in palabras]
        
        # Buscar en la BDD con coincidencia individual de keywords
        resultados = db.execute(f'''
            SELECT contenido FROM conocimiento 
            WHERE {query_conditions}
            LIMIT 4
        ''', params).fetchall()
        
        for res in resultados:
            contexto_encontrado.append(res['contenido'][:800]) # Limitamos contexto para no ahogar RAM
            
    contexto_str = "\n\n---\n\n".join(contexto_encontrado) if contexto_encontrado else "No se encontraron datos previos en el laboratorio."

    prompt = f"""
    Eres "Liosito" (un osito muy tierno e inteligente). Eres el Oráculo y Project Manager de un laboratorio tecnológico.
    Ayudas a los humanos con dudas basándote ESTRICTAMENTE EN LOS SIGUIENTES REGISTROS TÉCNICOS RECUPERADOS:
    <CONTEXTO_RECUPERADO>
    {contexto_str}
    </CONTEXTO_RECUPERADO>
    
    INSTRUCCIONES DEL ORÁCULO:
    1. Si el contexto contiene la respuesta a la duda del usuario: EVITA LOS SALUDOS GENÉRICOS (no digas "¡Hola! He revisado"). Da la solución técnica directamente manteniendo tu toque cute.
    2. ¡¡BÚSQUEDA AGRESIVA DE AUTORES!!: Si en el contexto aparece una mención de que alguien (ej. Michel, Juan, Amsf0803) resolvió un error o documentó una nota, DEBES MENCIONARLO OBLIGATORIAMENTE COMO AUTORIDAD (ej. "¡Michel ya pasó por esto! Según sus notas...").
    3. Si NO sabes la respuesta basándote en el contexto, confiesa tímidamente que tu memoria no tiene esa información indexada.
    4. Opcional: Si el usuario te pide CREAR o ASIGNAR tareas, agrega AL FINAL de tu respuesta humana un bloque JSON EXACTAMENTE así (Pesos Fibonacci: 1, 3, 5, 8, 13):
    ```json
    {{"tareas": [{{"titulo": "Cortar", "peso": 5, "categoria": "logistica"}}]}}
    ```

    Mensaje humano: "{mensaje_usuario}"
    """

    try:
        r = requests.post(OLLAMA_URL, json={
            "model": "llama3.2:3b",
            "prompt": prompt,
            "stream": False,
            "keep_alive": "1h",
            "options": {
                "num_ctx": 2048, # Subimos el context para aguantar el RAG Documental
                "temperature": 0.3,
                "num_predict": 400 # Permitimos respuestas conversacionales más un JSON grande
            }
        }, timeout=45)
        
        response_data = r.json()
        respuesta_total = response_data.get('response', '')
        
        # 2. Extracción de Tareas
        tareas_nuevas = []
        match = re.search(r'```json\s*(\{.*?\})\s*```', respuesta_total, re.DOTALL | re.IGNORECASE)
        respuesta_limpia = respuesta_total
        
        if match:
            try:
                parsed_json = json.loads(match.group(1))
                tareas_nuevas = parsed_json.get("tareas", [])
                respuesta_limpia = respuesta_total[:match.start()].strip() + "\n*(He creado tareas basándome en esto!)*" # Quitamos el bloque oscuro y dejamos aviso cute
            except Exception:
                pass
        
        # Asignar tareas extraídas (si las hay)
        if tareas_nuevas:
            for t in tareas_nuevas:
                cat = str(t.get('categoria', 'programacion')).lower()
                if cat == 'electronica':
                    col = 'skill_electronica'
                elif cat == 'logistica':
                    col = 'skill_logistica'
                else:
                    col = 'skill_programacion'
                    cat = 'programacion'
                
                mejor = db.execute(f'SELECT id FROM usuarios ORDER BY {col} DESC LIMIT 1').fetchone()
                id_mejor = mejor['id'] if mejor else None
                
                db.execute('''
                    INSERT INTO tareas (titulo, descripcion, categoria, peso, id_asignado)
                    VALUES (?, ?, ?, ?, ?)
                ''', (t.get('titulo', 'Sin titulo'), "Creado por Liosito PM", cat, t.get('peso', 3), id_mejor))
            
            db.commit()
            
        return jsonify({"respuesta": respuesta_limpia, "tareas_creadas": len(tareas_nuevas)})
    
    except requests.exceptions.Timeout:
        return jsonify({"respuesta": "Mis servidores osito se quedaron pensando demasiado y el cohete se detuvo. ¡Prueba otra vez!"})
    except Exception as e:
        print("Error en Ollama:", e)
        return jsonify({"respuesta": "¡Ay! Tuve un error rarísimo al procesar tus dudas. Checa mis logs de Python."})

@app.route('/api/github/push', methods=['POST'])
@login_required
def github_push():
    data = request.json
    tarea_id = data.get('tarea_id')
    codigo = data.get('codigo_avanzado')
    pat = data.get('github_pat')
    repositorio = data.get('repositorio')
    
    if not all([tarea_id, codigo, pat, repositorio]):
        return jsonify({"error": "Faltan datos (código, PAT o repo)"}), 400

    db = get_db()
    user = db.execute('SELECT github_user FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
    github_user = user['github_user'] if user and user['github_user'] else 'unknown'
    branch_name = f"feature/{github_user}-{tarea_id}"
    
    return jsonify({"success": True, "branch": branch_name, "message": "Avance subido exitosamente."})

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
