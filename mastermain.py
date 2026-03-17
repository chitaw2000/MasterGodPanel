cat << 'EOF_ADMIN_V4_1' > /root/qito_admin.py
from flask import Flask, render_template_string, request, redirect, session, url_for, send_file, jsonify
import json, os, subprocess, time, uuid, base64, re
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "qito_super_secret_admin_key"
USERS_DB = "/root/qito_master/users_db.json"
NODES_LIST = "/root/qito_master/nodes_list.txt"
CONFIG_FILE = "/root/qito_master/config.json"
ADMIN_PASS = "admin123"

USER_ACTIVITY = {}
ACTIVE_WINDOW = 60

def get_nodes():
    nodes = {}
    if os.path.exists(NODES_LIST):
        with open(NODES_LIST, "r") as f:
            for line in f:
                if line.strip() and len(line.split()) >= 2:
                    nodes[line.split()[0]] = line.split()[1]
    return nodes

def check_live_status(db):
    current_time = time.time()
    active_set = set()
    for uname, info in db.items():
        try: curr_bytes = float(info.get('used_bytes') or 0)
        except: curr_bytes = 0.0
        if uname not in USER_ACTIVITY:
            USER_ACTIVITY[uname] = {'bytes': curr_bytes, 'time': 0}
        else:
            if curr_bytes > USER_ACTIVITY[uname]['bytes']:
                USER_ACTIVITY[uname]['bytes'] = curr_bytes
                USER_ACTIVITY[uname]['time'] = current_time
        if (current_time - USER_ACTIVITY[uname]['time']) <= ACTIVE_WINDOW:
            active_set.add(uname)
    return active_set

def load_config():
    config = {"interval": 12, "bot_token": "", "admin_ids": [], "mod_ids": [], "disabled_nodes": []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
                config.update(loaded)
                if not isinstance(config.get('admin_ids'), list): config['admin_ids'] = []
                if not isinstance(config.get('mod_ids'), list): config['mod_ids'] = []
                if not isinstance(config.get('disabled_nodes'), list): config['disabled_nodes'] = []
        except: pass
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f)

def get_safe_delete_cmd(username, protocol, port):
    py_script = f"""
import json
try:
    path = '/usr/local/etc/xray/config.json'
    with open(path, 'r') as f: d = json.load(f)
    changed = False
    new_inbounds = []
    for ib in d.get('inbounds', []):
        if '{protocol}' == 'out' and str(ib.get('port')) == str('{port}'):
            changed = True
            continue
        if '{protocol}' == 'v2' and 'settings' in ib and 'clients' in ib['settings']:
            orig_len = len(ib['settings']['clients'])
            ib['settings']['clients'] = [c for c in ib['settings']['clients'] if c.get('email') != '{username}']
            if len(ib['settings']['clients']) != orig_len: changed = True
        new_inbounds.append(ib)
    if changed:
        d['inbounds'] = new_inbounds
        with open(path, 'w') as f: json.dump(d, f, indent=2)
except Exception as e: pass
"""
    b64_script = base64.b64encode(py_script.encode()).decode()
    return f"echo {b64_script} | base64 -d | python3"

@app.before_request
def check_auth():
    if request.endpoint not in ['login', 'static', 'api_stats'] and not session.get('logged_in'):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASS:
            session['logged_in'] = True; return redirect(url_for('dashboard'))
        else: error = "❌ Password မှားယွင်းနေပါသည်။"
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
def dashboard():
    nodes = get_nodes(); db = {}
    if os.path.exists(USERS_DB):
        try:
            with open(USERS_DB, 'r') as f: db = json.load(f)
        except: pass
    config = load_config()
    active_users = check_live_status(db)
    node_stats = []
    all_users = []
    for n_name, n_ip in nodes.items():
        total_count = 0; live_count = 0
        for uname, info in db.items():
            if info.get('node') == n_name:
                total_count += 1
                if uname in active_users and not info.get('is_blocked'): live_count += 1
                all_users.append({'username': uname, 'node': n_name, 'key': info.get('key', 'No Key')})
        node_stats.append({"name": n_name, "ip": n_ip, "total": total_count, "live": live_count, "disabled": n_name in config['disabled_nodes']})
    return render_template_string(DASHBOARD_HTML, nodes=node_stats, all_users=all_users, config=config)

@app.route('/node/<node_name>')
def node_view(node_name):
    db = {}
    if os.path.exists(USERS_DB):
        try:
            with open(USERS_DB, 'r') as f: db = json.load(f)
        except: pass
    config = load_config()
    active_users = check_live_status(db)
    users = []
    for uname, info in db.items():
        if info.get('node') == node_name:
            try: used_b = float(info.get('used_bytes') or 0)
            except: used_b = 0.0
            try: tot_gb = float(info.get('total_gb') or 0)
            except: tot_gb = 0.0
            info['used_bytes'] = used_b; info['total_gb'] = tot_gb
            info['used_gb_str'] = f"{(used_b / (1024**3)):.2f}"
            info['username'] = uname
            info['actual_key'] = info.get('key') or info.get('key_val') or "No Key Found"
            info['is_active'] = uname in active_users and not info.get('is_blocked')
            info['is_blocked'] = info.get('is_blocked', False)
            users.append(info)
    return render_template_string(NODE_HTML, node_name=node_name, users=users, config=config)

@app.route('/api/stats/<node_name>')
def api_stats(node_name):
    if not session.get('logged_in'): return jsonify({"status": "error"})
    nodes = get_nodes(); node_ip = nodes.get(node_name)
    if not node_ip: return jsonify({"status": "error"})
    try:
        cmd = f"ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no root@{node_ip} \"/usr/local/bin/xray api statsquery --server=127.0.0.1:10085\""
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if res.stdout.strip():
            stats = json.loads(res.stdout).get("stat", [])
            user_bytes = {}
            for s in stats:
                parts = s.get("name", "").split(">>>")
                if len(parts) >= 4:
                    uname = parts[1]
                    val = s.get("value", 0)
                    user_bytes[uname] = user_bytes.get(uname, 0) + val
            return jsonify({"status": "ok", "data": user_bytes})
    except: pass
    return jsonify({"status": "error"})

@app.route('/toggle_node/<node_name>', methods=['POST'])
def toggle_node(node_name):
    config = load_config()
    nodes = get_nodes()
    node_ip = nodes.get(node_name)
    if node_name in config['disabled_nodes']:
        config['disabled_nodes'].remove(node_name)
        if node_ip: os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} 'systemctl start xray'")
    else:
        config['disabled_nodes'].append(node_name)
        if node_ip: os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} 'systemctl stop xray'")
    save_config(config)
    return redirect(f'/node/{node_name}')

@app.route('/add_node', methods=['POST'])
def add_node():
    n_name = request.form.get('node_name'); n_ip = request.form.get('node_ip')
    if n_name and n_ip:
        with open(NODES_LIST, 'a') as f: f.write(f"\n{n_name} {n_ip}")
    return redirect(url_for('dashboard'))

@app.route('/add_user_manual', methods=['POST'])
def add_user_manual():
    creation_mode = request.form.get('creation_mode', 'single')
    usernames = []
    if creation_mode == 'single':
        u = request.form.get('single_username', '').strip()
        if u: usernames.append(u)
    elif creation_mode == 'list':
        raw = request.form.get('list_usernames', '')
        usernames = [u.strip() for u in re.split(r'[,\n]+', raw) if u.strip()]
    elif creation_mode == 'pattern':
        base = request.form.get('base_name', '').strip()
        start = int(request.form.get('start_num', 1))
        qty = int(request.form.get('qty', 1))
        for i in range(qty): usernames.append(f"{base}{start+i}")

    n_name = request.form.get('node_name')
    gb = float(request.form.get('total_gb', 0)); exp = request.form.get('expire_date')
    proto = request.form.get('protocol', 'v2')
    nodes = get_nodes(); n_ip = nodes.get(n_name)
    if not n_ip or not usernames: return redirect(f'/node/{n_name}')
    
    db = {}
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        
    max_port = 10000
    for u, info in db.items():
        if info.get('protocol') == 'out':
            try:
                p = int(info.get('port', 10000))
                if p > max_port: max_port = p
            except: pass
    
    commands = []; current_port = max_port
    for uname in usernames:
        if uname in db: continue
        uid = str(uuid.uuid4())
        if proto == 'v2':
            port = "443"
            key_str = f"vless://{uid}@{n_ip}:8080?path=%2Fvless&security=none&encryption=none&type=ws#{uname}"
            commands.append(f"/usr/local/bin/v2ray-node-add-vless {uname} {uid}")
        else:
            current_port += 1; port = str(current_port)
            ss_conf = base64.b64encode(f"chacha20-ietf-poly1305:{uid}".encode()).decode()
            key_str = f"ss://{ss_conf}@{n_ip}:{port}#{uname}"
            commands.append(f"/usr/local/bin/v2ray-node-add-out {uname} {uid} {port}")
            commands.append(f"ufw allow {port}/tcp && ufw allow {port}/udp")
            
        db[uname] = {"node": n_name, "protocol": proto, "uuid": uid, "port": port, "total_gb": gb, "expire_date": exp, "used_bytes": 0, "last_raw_bytes": 0, "is_blocked": False, "key": key_str}
    
    if commands:
        commands.append("systemctl restart xray")
        os.system(f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{n_ip} \"{' ; '.join(commands)}\"")
        with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(f'/node/{n_name}')

@app.route('/toggle_user/<username>', methods=['POST'])
def toggle_user(username):
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        if username in db:
            user = db[username]
            user['is_blocked'] = not user.get('is_blocked', False)
            nodes = get_nodes(); node_ip = nodes.get(user.get('node'))
            if node_ip:
                if user['is_blocked']:
                    safe_cmd = get_safe_delete_cmd(username, user.get('protocol', 'v2'), user.get('port', '443'))
                    os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} \"{safe_cmd}\"")
                else:
                    uid = user['uuid']
                    if user['protocol'] == 'v2': os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} '/usr/local/bin/v2ray-node-add-vless {username} {uid}'")
                    else: os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} '/usr/local/bin/v2ray-node-add-out {username} {uid} {user['port']}'")
                
                os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} 'systemctl restart xray'")
            with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(request.referrer)

@app.route('/bulk_delete', methods=['POST'])
def bulk_delete():
    node_name = request.form.get('node_name')
    usernames = request.form.getlist('usernames')
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        nodes = get_nodes(); node_ip = nodes.get(node_name)
        commands = []
        modified = False
        for uname in usernames:
            if uname in db:
                user = db[uname]
                safe_cmd = get_safe_delete_cmd(uname, user.get('protocol', 'v2'), user.get('port', '443'))
                commands.append(safe_cmd)
                del db[uname]
                modified = True
        if modified:
            if node_ip and commands:
                commands.append("systemctl restart xray")
                os.system(f"ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no root@{node_ip} \"{' ; '.join(commands)}\"")
            with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(f'/node/{node_name}')

@app.route('/edit_user/<username>', methods=['POST'])
def edit_user(username):
    new_gb = float(request.form.get('total_gb', 0)); new_date = request.form.get('expire_date', '')
    node_name = request.form.get('node_name', '')
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        user = db.get(username)
        if user:
            user['total_gb'] = new_gb; user['expire_date'] = new_date
            with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(url_for('node_view', node_name=node_name))

@app.route('/renew_user/<username>', methods=['POST'])
def renew_user(username):
    add_gb = float(request.form.get('add_gb', 50)); add_days = int(request.form.get('add_days', 30))
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        user = db.get(username)
        if user:
            user['total_gb'] = add_gb; user['days'] = add_days
            user['expire_date'] = (datetime.now() + timedelta(days=add_days)).strftime("%Y-%m-%d")
            user['used_bytes'] = 0; user['last_raw_bytes'] = 0; user['is_blocked'] = False
            with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(request.referrer)

@app.route('/delete_user/<username>', methods=['POST'])
def delete_user(username):
    if os.path.exists(USERS_DB):
        with open(USERS_DB, 'r') as f: db = json.load(f)
        if username in db:
            user = db[username]; nodes = get_nodes(); node_ip = nodes.get(user.get('node'))
            if node_ip:
                safe_cmd = get_safe_delete_cmd(username, user.get('protocol', 'v2'), user.get('port', '443'))
                os.system(f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{node_ip} \"{safe_cmd} ; systemctl restart xray\"")
            del db[username]
            with open(USERS_DB, 'w') as f: json.dump(db, f)
    return redirect(request.referrer)

@app.route('/download_backup')
def download_backup():
    if os.path.exists(USERS_DB): return send_file(USERS_DB, as_attachment=True, download_name=f"qito_db_backup.json")
    return "No DB found."

@app.route('/upload_backup', methods=['POST'])
def upload_backup():
    file = request.files.get('backup_file')
    if file: file.save(USERS_DB)
    return redirect(url_for('dashboard'))

@app.route('/save_settings_basic', methods=['POST'])
def save_settings_basic():
    config = load_config()
    config['interval'] = int(request.form.get('interval', 12))
    config['bot_token'] = request.form.get('bot_token', '')
    save_config(config)
    return redirect(url_for('dashboard'))

@app.route('/config_action', methods=['POST'])
def config_action():
    config = load_config()
    ctype = request.form.get('type'); action = request.form.get('action')
    val = request.form.get('val', '').strip()
    target_list = 'admin_ids' if ctype == 'admin' else 'mod_ids'
    if action == 'add' and val:
        if val not in config[target_list]: config[target_list].append(val)
    elif action == 'del' and val:
        if val in config[target_list]: config[target_list].remove(val)
    save_config(config)
    return redirect(url_for('dashboard'))

# --- HTML TEMPLATES ---
LOGIN_HTML = """<!DOCTYPE html><html><head><title>Admin Login</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gradient-to-br from-slate-900 to-blue-900 flex items-center justify-center h-screen font-sans"><div class="bg-white/10 backdrop-blur-lg p-10 rounded-3xl shadow-2xl w-96 text-center border border-white/20"><h2 class="text-3xl font-black text-white mb-8 tracking-widest uppercase shadow-black/50 drop-shadow-lg">QITO Master</h2><form method="POST"><input type="password" name="password" placeholder="Passcode..." class="w-full bg-black/30 border border-white/10 p-4 rounded-xl mb-6 text-white placeholder-white/50 focus:outline-none focus:ring-2 focus:ring-blue-400"><button class="w-full bg-blue-500 hover:bg-blue-400 text-white p-4 rounded-xl font-bold transition-all transform hover:scale-105 shadow-[0_0_15px_rgba(59,130,246,0.5)]">ACCESS PANEL</button></form></div></body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html><html><head><title>Dashboard</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css"></head>
<body class="bg-[#f4f7f6] p-4 md:p-8 font-sans"><div class="max-w-6xl mx-auto">
<div class="flex justify-between items-center mb-8"><h1 class="text-3xl font-black text-slate-800 tracking-tight">QITO <span class="text-transparent bg-clip-text bg-gradient-to-r from-blue-600 to-cyan-500">PRO</span></h1>
<div class="flex gap-3"><button onclick="document.getElementById('settingsModal').classList.remove('hidden')" class="bg-slate-800 text-white px-5 py-2.5 rounded-full font-bold shadow-md hover:bg-black transition"><i class="fa-solid fa-gear mr-2"></i> Settings</button>
<a href="/logout" class="bg-white border border-slate-200 text-red-500 px-5 py-2.5 rounded-full font-bold hover:bg-red-50 transition shadow-sm"><i class="fa-solid fa-power-off"></i></a></div></div>
<div class="mb-8 bg-white p-6 rounded-3xl shadow-sm border border-slate-100 relative overflow-hidden"><div class="absolute right-0 top-0 opacity-5 pointer-events-none"><i class="fa-solid fa-globe text-9xl"></i></div><h3 class="text-xs font-black text-slate-400 mb-4 uppercase tracking-widest">Global Search</h3><input type="text" id="globalSearch" onkeyup="searchEverything()" placeholder="Search users or keys..." class="w-full bg-slate-50 border-none p-4 rounded-2xl focus:ring-2 focus:ring-blue-500 transition outline-none"><div id="searchResults" class="mt-4 space-y-2 hidden max-h-60 overflow-y-auto"></div></div>
<div class="grid grid-cols-1 md:grid-cols-3 gap-6">
{% for n in nodes %}<a href="/node/{{ n.name }}" class="block bg-white rounded-3xl p-6 border border-slate-100 shadow-sm hover:shadow-xl hover:-translate-y-1 transition-all group relative {% if n.disabled %}opacity-60 grayscale{% endif %}">
<div class="absolute -top-3 -left-3 bg-blue-600 text-white w-8 h-8 flex items-center justify-center rounded-full font-black shadow-lg border-2 border-white">{{ loop.index }}</div>
<div class="flex justify-between items-start mb-6 mt-1"><div class="bg-gradient-to-br from-blue-500 to-cyan-400 p-3 rounded-2xl shadow-lg shadow-blue-500/30 text-white"><i class="fa-solid fa-server text-xl"></i></div>
{% if n.disabled %}<span class="flex items-center gap-1.5 bg-red-50 text-red-600 text-[10px] font-black px-3 py-1 rounded-full uppercase border border-red-100"><i class="fa-solid fa-ban"></i> Disabled</span>
{% else %}<span class="flex items-center gap-1.5 bg-emerald-50 text-emerald-600 text-[10px] font-black px-3 py-1 rounded-full uppercase border border-emerald-100"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> Online</span>{% endif %}</div>
<h2 class="text-2xl font-black text-slate-800 mb-1 group-hover:text-blue-600 transition">{{ n.name }}</h2><p class="text-xs text-slate-400 font-mono mb-6">{{ n.ip }}</p>
<div class="grid grid-cols-2 gap-3"><div class="bg-slate-50 p-4 rounded-2xl text-center border border-slate-100"><p class="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-1">Total</p><p class="text-xl font-black text-slate-700">{{ n.total }}</p></div><div class="bg-emerald-50 p-4 rounded-2xl text-center border border-emerald-100 relative overflow-hidden"><div class="absolute top-0 right-0 p-1.5"><span class="flex h-2 w-2"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span><span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span></span></div><p class="text-[10px] font-bold text-emerald-600 uppercase tracking-widest mb-1">Active</p><p class="text-xl font-black text-emerald-700">{{ n.live }}</p></div></div></a>{% endfor %}
<div onclick="document.getElementById('addNodeModal').classList.remove('hidden')" class="cursor-pointer border-2 border-dashed border-slate-300 rounded-3xl flex flex-col items-center justify-center p-6 text-slate-400 hover:border-blue-500 hover:text-blue-500 hover:bg-blue-50/50 transition-all min-h-[220px] mt-2"><i class="fa-solid fa-plus text-4xl mb-3"></i><p class="font-black uppercase tracking-widest text-sm">Add New Node</p></div>
</div></div>
<div id="settingsModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4 z-50 overflow-y-auto py-10"><div class="bg-white rounded-3xl p-8 w-full max-w-lg shadow-2xl border border-slate-100 my-auto"><div class="flex justify-between items-center mb-6"><h2 class="text-xl font-black text-slate-800"><i class="fa-solid fa-sliders text-blue-500 mr-2"></i> Settings & Roles</h2><button onclick="document.getElementById('settingsModal').classList.add('hidden')" class="text-slate-400 hover:text-red-500"><i class="fa-solid fa-xmark text-xl"></i></button></div>
<div class="space-y-6"><form action="/save_settings_basic" method="POST" class="bg-slate-50 p-5 rounded-2xl border border-slate-100 space-y-4"><div><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-1">Telegram Bot Token</label><input type="text" name="bot_token" value="{{ config.bot_token }}" class="w-full bg-white border border-slate-200 p-3 rounded-xl focus:ring-2 focus:ring-blue-500 outline-none text-sm" placeholder="12345:ABCdef..."></div><div><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-1">Auto-Backup Interval (Hours)</label><input type="number" name="interval" value="{{ config.interval }}" class="w-full bg-white border border-slate-200 p-3 rounded-xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></div><button class="w-full bg-slate-800 hover:bg-black text-white py-3 rounded-xl font-bold transition shadow-md">Save Core Settings</button></form>
<div class="grid grid-cols-2 gap-4"><div class="bg-slate-50 p-5 rounded-2xl border border-slate-100"><h3 class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-3"><i class="fa-solid fa-crown text-amber-500"></i> Admins</h3><ul class="space-y-2 mb-3 max-h-24 overflow-y-auto pr-1">{% for aid in config.admin_ids %}<li class="flex justify-between items-center bg-white border border-slate-200 px-3 py-2 rounded-lg text-sm font-bold"><span class="truncate">{{ aid }}</span><form action="/config_action" method="POST"><input type="hidden" name="type" value="admin"><input type="hidden" name="action" value="del"><input type="hidden" name="val" value="{{ aid }}"><button class="text-red-400 hover:text-red-600"><i class="fa-solid fa-trash"></i></button></form></li>{% endfor %}</ul><form action="/config_action" method="POST" class="flex gap-2"><input type="hidden" name="type" value="admin"><input type="hidden" name="action" value="add"><input type="text" name="val" placeholder="TG ID..." class="flex-1 min-w-0 bg-white border border-slate-200 p-2 rounded-lg text-xs outline-none"><button class="bg-amber-500 text-white px-3 py-2 rounded-lg text-xs font-bold"><i class="fa-solid fa-plus"></i></button></form></div>
<div class="bg-slate-50 p-5 rounded-2xl border border-slate-100"><h3 class="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-3"><i class="fa-solid fa-shield text-blue-500"></i> Moderators</h3><ul class="space-y-2 mb-3 max-h-24 overflow-y-auto pr-1">{% for mid in config.mod_ids %}<li class="flex justify-between items-center bg-white border border-slate-200 px-3 py-2 rounded-lg text-sm font-bold"><span class="truncate">{{ mid }}</span><form action="/config_action" method="POST"><input type="hidden" name="type" value="mod"><input type="hidden" name="action" value="del"><input type="hidden" name="val" value="{{ mid }}"><button class="text-red-400 hover:text-red-600"><i class="fa-solid fa-trash"></i></button></form></li>{% endfor %}</ul><form action="/config_action" method="POST" class="flex gap-2"><input type="hidden" name="type" value="mod"><input type="hidden" name="action" value="add"><input type="text" name="val" placeholder="TG ID..." class="flex-1 min-w-0 bg-white border border-slate-200 p-2 rounded-lg text-xs outline-none"><button class="bg-blue-500 text-white px-3 py-2 rounded-lg text-xs font-bold"><i class="fa-solid fa-plus"></i></button></form></div></div>
<div class="bg-slate-50 p-5 rounded-2xl border border-slate-100"><h3 class="text-xs font-black text-slate-500 uppercase tracking-widest mb-3">Database Backup</h3><div class="flex gap-2"><a href="/download_backup" class="flex-1 text-center bg-emerald-500 hover:bg-emerald-400 text-white py-3 rounded-xl font-bold shadow-md transition text-sm"><i class="fa-solid fa-download mr-1"></i> Download</a><form action="/upload_backup" method="POST" enctype="multipart/form-data" class="flex-1 flex gap-2"><label class="flex-1 cursor-pointer bg-white text-emerald-600 border border-emerald-200 py-3 rounded-xl font-bold text-center transition text-sm hover:bg-emerald-50"><i class="fa-solid fa-upload mr-1"></i> Upload<input type="file" name="backup_file" class="hidden" onchange="this.form.submit()"></label></form></div></div></div></div></div>
<div id="addNodeModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4 z-50"><div class="bg-white rounded-3xl p-8 w-80 shadow-2xl border border-slate-100"><h2 class="text-lg font-black mb-6 text-slate-800">Add New Node</h2><form action="/add_node" method="POST"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Node Name</label><input type="text" name="node_name" placeholder="e.g. Test6" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-4 focus:ring-2 focus:ring-blue-500 outline-none" required><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Node IP Address</label><input type="text" name="node_ip" placeholder="e.g. 192.168.1.1" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-8 focus:ring-2 focus:ring-blue-500 outline-none" required><div class="flex justify-end gap-3"><button type="button" onclick="document.getElementById('addNodeModal').classList.add('hidden')" class="px-5 py-3 font-bold text-slate-400 hover:text-slate-600">Cancel</button><button class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-3 rounded-xl font-bold shadow-lg shadow-blue-500/30 transition">Add Node</button></div></form></div></div>
<script>
const allUsers = {{ all_users | tojson }};
function searchEverything() {
    const term = document.getElementById('globalSearch').value.toLowerCase();
    const resultsDiv = document.getElementById('searchResults');
    if (term.length < 2) { resultsDiv.classList.add('hidden'); return; }
    resultsDiv.classList.remove('hidden');
    const filtered = allUsers.filter(u => u.username.toLowerCase().includes(term) || u.key.toLowerCase().includes(term));
    resultsDiv.innerHTML = filtered.length === 0 ? '<p class="text-slate-400 p-4 text-sm font-bold">No results found.</p>' : filtered.map(u => `<div class="flex items-center justify-between p-4 bg-white border border-slate-100 rounded-2xl hover:bg-slate-50 transition"><div><span class="font-black text-slate-800">${u.username}</span><span class="ml-3 text-[10px] bg-blue-50 text-blue-600 border border-blue-100 font-black px-3 py-1 rounded-full uppercase">${u.node}</span></div><div class="flex gap-2"><button onclick="copyRawKey('${u.key}')" class="text-xs font-bold bg-slate-800 text-white px-4 py-2 rounded-xl hover:bg-black transition shadow-md">Copy Key</button><a href="/node/${u.node}" class="text-xs font-bold bg-white border border-slate-200 text-slate-600 px-4 py-2 rounded-xl hover:bg-slate-100 transition">View Node</a></div></div>`).join('');
}
function copyRawKey(val) { if(!val || val === 'No Key') { alert("❌ Key Not Found!"); return; } var t = document.createElement("textarea"); document.body.appendChild(t); t.value = val; t.select(); document.execCommand("copy"); document.body.removeChild(t); alert("✅ Key Copied!"); }
</script></body></html>"""

NODE_HTML = """<!DOCTYPE html><html><head><title>Node Details</title><script src="https://cdn.tailwindcss.com"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css"></head>
<body class="bg-[#f4f7f6] p-4 md:p-8 font-sans"><div class="max-w-7xl mx-auto"><div class="flex justify-between items-center mb-8">
<div class="flex items-center gap-4"><a href="/" class="bg-white border border-slate-200 w-10 h-10 flex items-center justify-center rounded-full text-slate-400 hover:text-slate-800 hover:bg-slate-50 transition"><i class="fa-solid fa-arrow-left"></i></a>
<h1 class="text-2xl font-black text-slate-800">Node: <span class="text-transparent bg-clip-text bg-gradient-to-r from-blue-600 to-cyan-500 uppercase mr-3">{{ node_name }}</span></h1>
<form action="/toggle_node/{{ node_name }}" method="POST" class="inline m-0 p-0">
    {% if config.disabled_nodes and node_name in config.disabled_nodes %}<button class="bg-red-100 text-red-600 border border-red-200 px-4 py-2 rounded-full font-bold shadow-sm hover:bg-red-200 transition text-xs uppercase tracking-widest"><i class="fa-solid fa-lock mr-2"></i>Disabled (Click to Enable)</button>
    {% else %}<button class="bg-emerald-100 text-emerald-600 border border-emerald-200 px-4 py-2 rounded-full font-bold shadow-sm hover:bg-emerald-200 transition text-xs uppercase tracking-widest"><i class="fa-solid fa-lock-open mr-2"></i>Active (Click to Disable)</button>{% endif %}
</form></div>
<div class="flex gap-2 items-center">
<div class="bg-white border border-slate-200 rounded-full px-4 py-2 flex items-center gap-2 mr-2 shadow-sm">
    <label class="relative inline-flex items-center cursor-pointer" title="Enable Live Speed">
        <input type="checkbox" id="speedToggle" class="sr-only peer" onchange="toggleSpeedMonitor()">
        <div class="w-8 h-4 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:bg-blue-500 shadow-inner"></div>
    </label>
    <span class="text-[10px] font-black text-slate-500 uppercase tracking-widest"><i class="fa-solid fa-gauge-high mr-1"></i> Live Speed</span>
</div>
<button id="delSelectedBtn" form="bulkForm" class="hidden bg-red-500 hover:bg-red-600 text-white px-6 py-2.5 rounded-full font-bold shadow-lg shadow-red-500/30 transition"><i class="fa-solid fa-trash-can mr-2"></i>Delete Selected</button>
<button onclick="document.getElementById('addUserModal').classList.remove('hidden')" class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-2.5 rounded-full font-bold shadow-lg shadow-blue-500/30 transition"><i class="fa-solid fa-plus mr-2"></i> Create Key(s)</button></div></div>

<form id="bulkForm" action="/bulk_delete" method="POST" onsubmit="return confirm('Are you sure you want to permanently delete the selected keys?');"><input type="hidden" name="node_name" value="{{ node_name }}">
<div class="bg-white rounded-3xl shadow-sm border border-slate-100 overflow-x-auto"><table class="w-full text-left whitespace-nowrap">
<thead><tr class="bg-slate-50 border-b border-slate-100 text-[10px] font-black uppercase tracking-widest text-slate-400">
<th class="p-6 rounded-tl-3xl w-10 text-center"><input type="checkbox" id="selectAll" class="w-4 h-4 cursor-pointer accent-blue-600" onclick="const cb = document.querySelectorAll('input[name=\\'usernames\\']'); cb.forEach(c => c.checked = this.checked); toggleDelBtn();"></th>
<th class="p-6">No.</th><th class="p-6">User Profile</th><th class="p-6">Traffic Usage</th><th class="p-6">Expiry</th><th class="p-6 text-center">VPN Key</th><th class="p-6 text-center rounded-tr-3xl">Actions</th></tr></thead>
<tbody class="divide-y divide-slate-50">
{% for u in users %}<tr class="hover:bg-slate-50/50 transition-colors {% if u.is_blocked %}opacity-60 bg-red-50/20{% endif %}">
<td class="p-6 text-center"><input type="checkbox" name="usernames" value="{{ u.username }}" class="w-4 h-4 cursor-pointer accent-blue-600" onchange="toggleDelBtn()"></td>
<td class="p-6 text-slate-400 font-mono text-sm">{{ loop.index }}</td>
<td class="p-6"><div class="flex items-center gap-3">
    {% if u.is_blocked %}<div class="relative flex items-center justify-center w-10 h-10 rounded-full bg-red-100 border border-red-200 text-red-600"><i class="fa-solid fa-ban"></i></div>
    {% elif u.is_active %}<div class="relative flex items-center justify-center w-10 h-10 rounded-full bg-emerald-100 border border-emerald-200 text-emerald-600 shadow-inner"><i class="fa-solid fa-bolt"></i><span class="absolute top-0 right-0 w-3 h-3 bg-emerald-500 border-2 border-white rounded-full"></span></div>
    {% elif u.used_bytes == 0 %}<div class="relative flex items-center justify-center w-10 h-10 rounded-full bg-amber-100 border border-amber-200 text-amber-600"><i class="fa-solid fa-hourglass-half"></i></div>
    {% else %}<div class="relative flex items-center justify-center w-10 h-10 rounded-full bg-slate-100 border border-slate-200 text-slate-400"><i class="fa-solid fa-user"></i></div>{% endif %}
    <div><div class="flex items-center gap-2"><p class="font-black text-slate-800 {% if u.is_blocked %}line-through{% endif %}">{{ u.username }}</p><span id="speed_{{ u.username }}" class="live-speed-tag hidden bg-blue-50 text-blue-500 border border-blue-100 px-2 py-0.5 rounded text-[10px] font-black">0 KB/s</span></div>
        {% if u.is_blocked %}<p class="text-[10px] font-bold text-red-500 uppercase tracking-widest">🚫 Blocked</p>
        {% elif u.is_active %}<p class="text-[10px] font-bold text-emerald-500 uppercase tracking-widest">🟢 Online</p>
        {% elif u.used_bytes == 0 %}<p class="text-[10px] font-bold text-amber-500 uppercase tracking-widest">🟡 Pending</p>
        {% else %}<p class="text-[10px] font-bold text-slate-400 uppercase tracking-widest">⚪ Offline</p>{% endif %}
    </div></div>
</td>
<td class="p-6"><div class="flex flex-col"><span class="font-black text-slate-700 text-sm">{{ u.used_gb_str }} <span class="text-slate-400 text-xs">/ {{ u.total_gb }} GB</span></span>
<div class="w-24 h-1.5 bg-slate-100 rounded-full mt-2 overflow-hidden"><div class="h-full bg-blue-500 rounded-full" style="width: {{ (u.used_bytes / (u.total_gb * 1024**3) * 100) if u.total_gb > 0 else 0 }}%"></div></div></div></td>
<td class="p-6 font-mono text-xs font-bold text-slate-500">{{ u.expire_date }}</td>
<td class="p-6 text-center"><textarea id="k_{{ loop.index }}" class="hidden">{{ u.actual_key }}</textarea><button type="button" onclick="copyK('k_{{ loop.index }}')" class="bg-slate-800 text-white px-5 py-2.5 rounded-xl text-[10px] font-black uppercase tracking-widest hover:bg-black transition shadow-md hover:shadow-lg" {% if u.is_blocked %}disabled{% endif %}><i class="fa-solid fa-key mr-1.5"></i> Copy</button></td>
<td class="p-6 flex justify-center gap-4 items-center">
<label class="relative inline-flex items-center cursor-pointer" title="{% if u.is_blocked %}Unblock User{% else %}Block User{% endif %}">
    <input type="checkbox" class="sr-only peer" {% if not u.is_blocked %}checked{% endif %} onchange="submitToggle('{{ u.username }}')">
    <div class="w-10 h-5 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-emerald-500 shadow-inner"></div>
</label>
<button type="button" onclick="openRenew('{{ u.username }}')" class="text-emerald-500 hover:text-emerald-700 transition text-lg" title="Renew"><i class="fa-solid fa-rotate"></i></button>
<button type="button" onclick="openM('{{ u.username }}', '{{ u.total_gb }}', '{{ u.expire_date }}')" class="text-blue-500 hover:text-blue-700 transition text-lg" title="Edit"><i class="fa-solid fa-pen"></i></button>
<button type="button" onclick="if(confirm('Delete {{ u.username }}?')) { submitDelete('{{ u.username }}'); }" class="text-red-400 hover:text-red-600 transition text-lg" title="Delete"><i class="fa-solid fa-trash"></i></button></td></tr>{% endfor %}
</tbody></table></div></form></div>

<form id="actionToggleForm" action="" method="POST" class="hidden"></form><form id="actionDeleteForm" action="" method="POST" class="hidden"></form>
<div id="addUserModal" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4 z-50"><div class="bg-white rounded-3xl p-8 w-[420px] shadow-2xl border border-slate-100"><h2 class="text-lg font-black mb-6 text-slate-800">Generate Keys</h2>
<form action="/add_user_manual" method="POST"><input type="hidden" name="node_name" value="{{ node_name }}">
<label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Creation Mode</label><select id="creation_mode" name="creation_mode" onchange="toggleMode()" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl mb-4 focus:ring-2 focus:ring-blue-500 outline-none text-sm font-bold text-slate-700"><option value="single">Single Key (1 User)</option><option value="list">Bulk List (Multiple Users)</option><option value="pattern">Auto-Number (e.g. ab1, ab2...)</option></select>
<div id="mode_single" class="block mb-4"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Username</label><input type="text" name="single_username" placeholder="e.g. aung123" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></div><div id="mode_list" class="hidden mb-4"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Usernames (Comma or newline separated)</label><textarea name="list_usernames" rows="3" placeholder="user1, user2&#10;user3" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></textarea></div><div id="mode_pattern" class="hidden mb-4 grid grid-cols-3 gap-2"><div class="col-span-1"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Prefix</label><input type="text" name="base_name" placeholder="ab" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></div><div class="col-span-1"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Start No.</label><input type="number" name="start_num" value="1" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></div><div class="col-span-1"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Quantity</label><input type="number" name="qty" value="5" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none text-sm"></div></div>
<label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Protocol</label><select name="protocol" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl mb-4 focus:ring-2 focus:ring-blue-500 outline-none text-sm"><option value="v2">VLESS</option><option value="out">Shadowsocks</option></select><div class="flex gap-3 mb-8"><div class="flex-1"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Total GB</label><input type="number" step="0.1" name="total_gb" value="50" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none"></div><div class="flex-1"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Expiry Date</label><input type="date" name="expire_date" class="w-full bg-slate-50 border border-slate-100 p-3 rounded-2xl focus:ring-2 focus:ring-blue-500 outline-none" required></div></div><div class="flex justify-end gap-3"><button type="button" onclick="document.getElementById('addUserModal').classList.add('hidden')" class="px-5 py-3 font-bold text-slate-400 hover:text-slate-600">Cancel</button><button class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-3 rounded-xl font-bold shadow-lg shadow-blue-500/30 transition">Generate</button></div></form></div></div>
<div id="m" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4 z-50"><div class="bg-white rounded-3xl p-8 w-80 shadow-2xl border border-slate-100"><h2 class="text-lg font-black mb-6 text-slate-800" id="mu"></h2><form id="ef" method="POST"><input type="hidden" name="node_name" value="{{ node_name }}"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Total Limit (GB)</label><input type="number" step="0.1" name="total_gb" id="mg" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-4 focus:ring-2 focus:ring-blue-500 outline-none"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Expiry Date</label><input type="text" name="expire_date" id="md" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-8 focus:ring-2 focus:ring-blue-500 outline-none"><div class="flex justify-end gap-3"><button type="button" onclick="closeM()" class="px-5 py-3 font-bold text-slate-400 hover:text-slate-600">Cancel</button><button class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-3 rounded-xl font-bold shadow-lg shadow-blue-500/30 transition">Save</button></div></form></div></div>
<div id="rm" class="hidden fixed inset-0 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4 z-50"><div class="bg-white rounded-3xl p-8 w-80 shadow-2xl border border-slate-100"><h2 class="text-lg font-black mb-6 text-emerald-600">Renew: <span id="ru"></span></h2><form id="rf" method="POST"><input type="hidden" name="node_name" value="{{ node_name }}"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">New Data (GB)</label><input type="number" step="0.1" name="add_gb" value="50" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-4 focus:ring-2 focus:ring-emerald-500 outline-none"><label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-2">Add Duration (Days)</label><input type="number" name="add_days" value="30" class="w-full bg-slate-50 border border-slate-100 p-4 rounded-2xl mb-8 focus:ring-2 focus:ring-emerald-500 outline-none"><div class="flex justify-end gap-3"><button type="button" onclick="closeRenew()" class="px-5 py-3 font-bold text-slate-400 hover:text-slate-600">Cancel</button><button class="bg-emerald-500 hover:bg-emerald-400 text-white px-6 py-3 rounded-xl font-bold shadow-lg shadow-emerald-500/30 transition">Renew</button></div></form></div></div>

<script>
let speedInterval = null;
let lastStats = {};
let lastTime = 0;

// 🔥 LocalStorage ကို သုံးပြီး Speed Toggle အခြေအနေကို မှတ်သားခြင်း
document.addEventListener('DOMContentLoaded', () => {
    const savedState = localStorage.getItem('speedToggleState_{{ node_name }}');
    if (savedState === 'true') {
        document.getElementById('speedToggle').checked = true;
        toggleSpeedMonitor();
    }
});

function toggleSpeedMonitor() {
    const isEnabled = document.getElementById('speedToggle').checked;
    localStorage.setItem('speedToggleState_{{ node_name }}', isEnabled);
    
    const speedTags = document.querySelectorAll('.live-speed-tag');
    if(isEnabled) {
        speedTags.forEach(el => el.classList.remove('hidden'));
        lastTime = Date.now();
        fetchStats();
        speedInterval = setInterval(fetchStats, 2000);
    } else {
        speedTags.forEach(el => el.classList.add('hidden'));
        clearInterval(speedInterval);
        lastStats = {};
    }
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats/{{ node_name }}');
        const json = await res.json();
        if(json.status === 'ok') {
            const now = Date.now();
            const timeDiff = (now - lastTime) / 1000;
            const currentStats = json.data;
            for(const user in currentStats) {
                if(lastStats[user] !== undefined) {
                    const bytesDiff = currentStats[user] - lastStats[user];
                    let speed = 0;
                    if(bytesDiff > 0 && timeDiff > 0) speed = bytesDiff / timeDiff;
                    
                    let speedStr = "0 B/s";
                    if(speed > 1024*1024) speedStr = (speed / (1024*1024)).toFixed(2) + " MB/s";
                    else if(speed > 1024) speedStr = (speed / 1024).toFixed(2) + " KB/s";
                    else if(speed > 0) speedStr = speed.toFixed(0) + " B/s";
                    
                    const el = document.getElementById('speed_' + user);
                    if(el) { el.innerText = speedStr; el.classList.add('animate-pulse'); setTimeout(() => el.classList.remove('animate-pulse'), 500); }
                }
            }
            lastStats = currentStats;
            lastTime = now;
        }
    } catch(e) {}
}

function copyK(id){ var t = document.getElementById(id); if(!t.value || t.value==='No Key Found') { alert("❌ Key Not Found in Database!"); return; } var dummy = document.createElement("textarea"); document.body.appendChild(dummy); dummy.value = t.value; dummy.select(); document.execCommand("copy"); document.body.removeChild(dummy); alert("✅ Key Copied Successfully!"); }
function openM(u,g,d){ document.getElementById('mu').innerText="Edit: "+u; document.getElementById('mg').value=g; document.getElementById('md').value=d; document.getElementById('ef').action="/edit_user/"+u; document.getElementById('m').classList.remove('hidden'); }
function closeM(){ document.getElementById('m').classList.add('hidden'); }
function openRenew(u){ document.getElementById('ru').innerText=u; document.getElementById('rf').action="/renew_user/"+u; document.getElementById('rm').classList.remove('hidden'); }
function closeRenew(){ document.getElementById('rm').classList.add('hidden'); }
function toggleMode() { let mode = document.getElementById('creation_mode').value; document.getElementById('mode_single').classList.toggle('hidden', mode !== 'single'); document.getElementById('mode_list').classList.toggle('hidden', mode !== 'list'); document.getElementById('mode_pattern').classList.toggle('hidden', mode !== 'pattern'); }
function toggleDelBtn() { const anyChecked = document.querySelectorAll('input[name="usernames"]:checked').length > 0; document.getElementById('delSelectedBtn').classList.toggle('hidden', !anyChecked); }
function submitToggle(u) { let f = document.getElementById('actionToggleForm'); f.action = '/toggle_user/' + u; f.submit(); }
function submitDelete(u) { let f = document.getElementById('actionDeleteForm'); f.action = '/delete_user/' + u; f.submit(); }
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888)
EOF_ADMIN_V4_1

pm2 restart qito-admin
