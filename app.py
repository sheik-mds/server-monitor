#!/usr/bin/env python3
"""
ServerPulse - Multi-Server Monitoring Dashboard
Lightweight SSH-based server monitoring with PM2 support and AI chatbot
"""

import json
import os
import re
import time
import threading
import logging
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path

import paramiko
from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO, emit
import anthropic

# ─── Configuration ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'serverpulse-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DATA_FILE = Path('data/servers.json')
DATA_FILE.parent.mkdir(exist_ok=True)

# In-memory metric history: {server_id: deque of {ts, cpu, ram, disk}}
metric_history = {}
# Cache of last collected metrics
metrics_cache = {}
cache_lock = threading.Lock()

POLL_INTERVAL = 30  # seconds

# ─── Persistence ──────────────────────────────────────────────────────────────
def load_servers():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return []

def save_servers(servers):
    DATA_FILE.write_text(json.dumps(servers, indent=2))

# ─── SSH Helpers ──────────────────────────────────────────────────────────────
def get_ssh_client(server: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=server['ip'],
        port=int(server.get('port', 22)),
        username=server['username'],
        timeout=10,
    )
    key_path = server.get('key_path', '').strip()
    password = server.get('password', '').strip()
    if key_path:
        connect_kwargs['key_filename'] = os.path.expanduser(key_path)
    elif password:
        connect_kwargs['password'] = password
    client.connect(**connect_kwargs)
    return client

def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 15) -> str:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace').strip()
    return out

# ─── Metric Collection ────────────────────────────────────────────────────────
METRICS_SCRIPT = r"""
python3 -c "
import subprocess, json, re, os

def run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except:
        return ''

# CPU usage (1-second sample)
cpu_line = run(\"top -bn1 | grep 'Cpu(s)'\")
cpu_match = re.search(r'(\d+\.?\d*)\s*id', cpu_line)
cpu = round(100 - float(cpu_match.group(1)), 1) if cpu_match else 0.0

# RAM
mem = run('free -b')
mem_lines = mem.split('\n')
for l in mem_lines:
    if l.startswith('Mem:'):
        parts = l.split()
        total, used = int(parts[1]), int(parts[2])
        break
else:
    total, used = 0, 0
ram_pct = round(used/total*100, 1) if total else 0

# Disk
disk = run(\"df -h / | tail -1\").split()
disk_total = disk[1] if len(disk)>1 else '?'
disk_used = disk[2] if len(disk)>2 else '?'
disk_pct = int(disk[4].replace('%','')) if len(disk)>4 else 0

# Load average
load = run('cat /proc/loadavg').split()[:3]

# Uptime
uptime = run('uptime -p')

# Network
net_raw = run(\"cat /proc/net/dev\")
rx_total, tx_total = 0, 0
for line in net_raw.split('\n')[2:]:
    if ':' in line:
        parts = line.split(':')[1].split()
        if parts:
            rx_total += int(parts[0])
            tx_total += int(parts[8]) if len(parts)>8 else 0

# Process count
proc_count = run('ps aux | wc -l')

print(json.dumps({
    'cpu': cpu,
    'ram_pct': ram_pct,
    'ram_used': used,
    'ram_total': total,
    'disk_pct': disk_pct,
    'disk_total': disk_total,
    'disk_used': disk_used,
    'load': load,
    'uptime': uptime,
    'net_rx': rx_total,
    'net_tx': tx_total,
    'proc_count': int(proc_count)-1,
}))
"
"""

PM2_SCRIPT = r"""
bash -c '
collect_pm2() {
    local user="$1"
    local sudo_prefix="$2"
    
    if ! ${sudo_prefix}pm2 list --no-color 2>/dev/null | grep -q "name"; then
        return
    fi
    
    ${sudo_prefix}pm2 jlist 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for p in data:
        print(json.dumps({
            \"name\": p.get(\"name\",\"\"),
            \"pm_id\": p.get(\"pm_id\",0),
            \"status\": p.get(\"pm2_env\",{}).get(\"status\",\"?\"),
            \"cpu\": p.get(\"monit\",{}).get(\"cpu\",0),
            \"memory\": p.get(\"monit\",{}).get(\"memory\",0),
            \"uptime\": p.get(\"pm2_env\",{}).get(\"pm_uptime\",0),
            \"restarts\": p.get(\"pm2_env\",{}).get(\"restart_time\",0),
            \"pid\": p.get(\"pid\",0),
            \"user\": \"'"$user"'\",
            \"is_root\": \"'"$user"'\" == \"root\",
            \"exec_mode\": p.get(\"pm2_env\",{}).get(\"exec_mode\",\"fork\"),
            \"script\": p.get(\"pm2_env\",{}).get(\"pm_exec_path\",\"\"),
        }))
except:
    pass
" 2>/dev/null
}

# Root PM2
collect_pm2 "root" "sudo "

# Current user PM2
CURRENT_USER=$(whoami)
if [ "$CURRENT_USER" != "root" ]; then
    collect_pm2 "$CURRENT_USER" ""
fi

# Check other users who might have PM2
for home_dir in /home/*/; do
    username=$(basename "$home_dir")
    if [ "$username" != "$CURRENT_USER" ] && [ "$username" != "root" ]; then
        if [ -f "$home_dir/.nvm/versions/node/$(ls $home_dir/.nvm/versions/node/ 2>/dev/null | head -1)/bin/pm2" ] || command -v pm2 >/dev/null 2>&1; then
            sudo -u "$username" pm2 jlist 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for p in data:
        print(json.dumps({\"name\": p.get(\"name\",\"\"), \"pm_id\": p.get(\"pm_id\",0), \"status\": p.get(\"pm2_env\",{}).get(\"status\",\"?\"), \"cpu\": p.get(\"monit\",{}).get(\"cpu\",0), \"memory\": p.get(\"monit\",{}).get(\"memory\",0), \"uptime\": p.get(\"pm2_env\",{}).get(\"pm_uptime\",0), \"restarts\": p.get(\"pm2_env\",{}).get(\"restart_time\",0), \"pid\": p.get(\"pid\",0), \"user\": \"$username\", \"is_root\": False, \"exec_mode\": p.get(\"pm2_env\",{}).get(\"exec_mode\",\"fork\"), \"script\": p.get(\"pm2_env\",{}).get(\"pm_exec_path\",\"\")}))
except:
    pass
" 2>/dev/null
        fi
    fi
done
'
"""

def collect_server_metrics(server: dict) -> dict:
    sid = server['id']
    result = {
        'id': sid,
        'name': server['name'],
        'ip': server['ip'],
        'status': 'offline',
        'last_updated': datetime.now().isoformat(),
        'metrics': {},
        'pm2': [],
        'error': None,
    }
    try:
        client = get_ssh_client(server)
        # System metrics
        raw = ssh_exec(client, METRICS_SCRIPT.strip())
        # Find JSON in output
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            result['metrics'] = json.loads(json_match.group())
            result['status'] = 'online'
        # PM2 processes
        pm2_raw = ssh_exec(client, PM2_SCRIPT.strip(), timeout=20)
        pm2_list = []
        for line in pm2_raw.strip().split('\n'):
            line = line.strip()
            if line.startswith('{'):
                try:
                    pm2_list.append(json.loads(line))
                except Exception:
                    pass
        result['pm2'] = pm2_list
        client.close()
    except Exception as e:
        result['status'] = 'offline'
        result['error'] = str(e)
        logger.warning(f"[{server['name']}] Collection failed: {e}")

    # Store history
    if result['status'] == 'online' and result['metrics']:
        m = result['metrics']
        if sid not in metric_history:
            metric_history[sid] = deque(maxlen=120)  # ~1hr at 30s
        metric_history[sid].append({
            'ts': time.time(),
            'cpu': m.get('cpu', 0),
            'ram': m.get('ram_pct', 0),
            'disk': m.get('disk_pct', 0),
        })

    return result

def polling_loop():
    """Background thread: poll all servers every POLL_INTERVAL seconds."""
    while True:
        servers = load_servers()
        for server in servers:
            try:
                data = collect_server_metrics(server)
                with cache_lock:
                    metrics_cache[server['id']] = data
                socketio.emit('server_update', data)
            except Exception as e:
                logger.error(f"Poll error for {server.get('name')}: {e}")
        time.sleep(POLL_INTERVAL)

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/servers', methods=['GET'])
def api_get_servers():
    servers = load_servers()
    # Strip sensitive fields
    safe = [{k: v for k, v in s.items() if k not in ('password',)} for s in servers]
    return jsonify(safe)

@app.route('/api/servers', methods=['POST'])
def api_add_server():
    data = request.json
    required = ('name', 'ip', 'username')
    if not all(data.get(k) for k in required):
        return jsonify({'error': 'name, ip, username required'}), 400
    servers = load_servers()
    server = {
        'id': f"srv_{int(time.time()*1000)}",
        'name': data['name'],
        'ip': data['ip'],
        'port': int(data.get('port', 22)),
        'username': data['username'],
        'key_path': data.get('key_path', ''),
        'password': data.get('password', ''),
        'tags': data.get('tags', []),
        'added_at': datetime.now().isoformat(),
    }
    servers.append(server)
    save_servers(servers)
    # Immediately collect metrics in background
    threading.Thread(target=_collect_and_emit, args=(server,), daemon=True).start()
    return jsonify({'ok': True, 'id': server['id']})

@app.route('/api/servers/<sid>', methods=['DELETE'])
def api_delete_server(sid):
    servers = [s for s in load_servers() if s['id'] != sid]
    save_servers(servers)
    with cache_lock:
        metrics_cache.pop(sid, None)
    metric_history.pop(sid, None)
    return jsonify({'ok': True})

@app.route('/api/servers/<sid>', methods=['PUT'])
def api_update_server(sid):
    data = request.json
    servers = load_servers()
    for s in servers:
        if s['id'] == sid:
            for k in ('name', 'ip', 'port', 'username', 'key_path', 'password', 'tags'):
                if k in data:
                    s[k] = data[k]
            break
    save_servers(servers)
    return jsonify({'ok': True})

@app.route('/api/metrics')
def api_metrics():
    with cache_lock:
        return jsonify(list(metrics_cache.values()))

@app.route('/api/metrics/<sid>')
def api_server_metrics(sid):
    with cache_lock:
        data = metrics_cache.get(sid)
    if not data:
        # Try fresh collect
        servers = load_servers()
        server = next((s for s in servers if s['id'] == sid), None)
        if not server:
            return jsonify({'error': 'Not found'}), 404
        data = collect_server_metrics(server)
        with cache_lock:
            metrics_cache[sid] = data
    return jsonify(data)

@app.route('/api/history/<sid>')
def api_history(sid):
    hist = list(metric_history.get(sid, []))
    return jsonify(hist)

@app.route('/api/refresh/<sid>', methods=['POST'])
def api_refresh(sid):
    servers = load_servers()
    server = next((s for s in servers if s['id'] == sid), None)
    if not server:
        return jsonify({'error': 'Not found'}), 404
    threading.Thread(target=_collect_and_emit, args=(server,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/pm2/logs', methods=['POST'])
def api_pm2_logs():
    """Fetch PM2 logs for a specific service."""
    data = request.json
    sid = data.get('server_id')
    service = data.get('service')
    lines = int(data.get('lines', 100))
    run_as = data.get('run_as', 'root')

    servers = load_servers()
    server = next((s for s in servers if s['id'] == sid), None)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    try:
        client = get_ssh_client(server)
        if run_as == 'root':
            cmd = f"sudo pm2 logs {service} --lines {lines} --nostream --no-color 2>&1 | tail -{lines}"
        else:
            cmd = f"pm2 logs {service} --lines {lines} --nostream --no-color 2>&1 | tail -{lines}"
        logs = ssh_exec(client, cmd, timeout=20)
        client.close()
        return jsonify({'logs': logs})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    data = request.json
    try:
        client = get_ssh_client(data)
        ssh_exec(client, 'echo ok')
        client.close()
        return jsonify({'ok': True, 'message': 'Connection successful'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 200

# ─── AI Chatbot ───────────────────────────────────────────────────────────────
ALLOWED_INTENTS = [
    'pm2 logs', 'pm2 status', 'cpu usage', 'ram usage', 'memory usage',
    'disk usage', 'load average', 'uptime', 'process list', 'network stats',
    'service status', 'server info', 'top processes', 'error logs',
    'system logs', 'pm2 restart count', 'pm2 process info',
    'past cpu', 'past ram', 'historical', 'last hour', 'last 30 minutes',
    'last 30 mins', 'last hour', 'performance', 'health check',
]

SYSTEM_PROMPT = """You are ServerPulse AI, an expert Linux server monitoring assistant.

STRICT RULES — you MUST follow these without exception:
1. You ONLY answer questions about server monitoring, diagnostics, and investigation.
2. You REFUSE any request to modify, restart, stop, start, delete, or change anything on the servers.
3. You REFUSE requests unrelated to server/service monitoring (no coding help, no general questions).
4. You answer ONLY using the data provided in the context below. Do not fabricate metrics.
5. When you refuse, be brief and professional: explain what you CAN help with.

You CAN help with:
- PM2 service logs, status, restarts, memory, CPU usage
- Server CPU, RAM, disk, load average, uptime
- Historical metric trends (last N minutes/hours)
- Identifying high resource usage or anomalies
- Explaining what the metrics mean
- Comparing metrics across servers

FORMAT: Use concise markdown. For metrics use inline code. For logs use code blocks.
Always cite which server and timestamp you're referring to.
"""

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    question = data.get('question', '').strip()
    server_id = data.get('server_id')  # optional: ask about specific server

    if not question:
        return jsonify({'error': 'No question provided'}), 400

    # Build context from current metrics
    with cache_lock:
        all_metrics = dict(metrics_cache)

    context_parts = []
    if server_id and server_id in all_metrics:
        servers_data = [all_metrics[server_id]]
    else:
        servers_data = list(all_metrics.values())

    for m in servers_data:
        sid = m['id']
        hist = list(metric_history.get(sid, []))
        part = f"""
## Server: {m['name']} ({m['ip']})
Status: {m['status']}
Last Updated: {m['last_updated']}
"""
        if m['status'] == 'online' and m['metrics']:
            mt = m['metrics']
            part += f"""
### Current Metrics
- CPU Usage: {mt.get('cpu', '?')}%
- RAM Usage: {mt.get('ram_pct', '?')}% ({_bytes_human(mt.get('ram_used',0))} / {_bytes_human(mt.get('ram_total',0))})
- Disk Usage: {mt.get('disk_pct', '?')}% ({mt.get('disk_used','?')} / {mt.get('disk_total','?')})
- Load Average: {', '.join(mt.get('load', ['?','?','?']))}
- Uptime: {mt.get('uptime', '?')}
- Process Count: {mt.get('proc_count', '?')}
"""
        if m['pm2']:
            part += "\n### PM2 Services\n"
            for p in m['pm2']:
                owner = "ROOT" if p.get('is_root') else p.get('user', 'user')
                part += (f"- [{owner}] {p['name']} (id:{p['pm_id']}) | "
                         f"status:{p['status']} | cpu:{p.get('cpu',0)}% | "
                         f"mem:{_bytes_human(p.get('memory',0))} | "
                         f"restarts:{p.get('restarts',0)}\n")

        if hist:
            recent = hist[-6:]  # last ~3 min
            part += "\n### Recent CPU/RAM History (last ~3 min)\n"
            for h in recent:
                ts = datetime.fromtimestamp(h['ts']).strftime('%H:%M:%S')
                part += f"  {ts} — CPU:{h['cpu']}% RAM:{h['ram']}% Disk:{h['disk']}%\n"

            # 30-min avg
            cutoff = time.time() - 1800
            last30 = [h for h in hist if h['ts'] >= cutoff]
            if last30:
                avg_cpu = round(sum(h['cpu'] for h in last30) / len(last30), 1)
                avg_ram = round(sum(h['ram'] for h in last30) / len(last30), 1)
                part += f"\n### Last 30 Minutes Averages\n- Avg CPU: {avg_cpu}%\n- Avg RAM: {avg_ram}%\n"

        context_parts.append(part)

    context = "\n---\n".join(context_parts) if context_parts else "No server data available."

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"<server_data>\n{context}\n</server_data>\n\nQuestion: {question}"
                }
            ]
        )
        answer = response.content[0].text
        return jsonify({'answer': answer})
    except Exception as e:
        logger.error(f"AI chat error: {e}")
        return jsonify({'error': f'AI error: {str(e)}'}), 500

# ─── Helpers ──────────────────────────────────────────────────────────────────
def _bytes_human(b):
    b = int(b or 0)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"

def _collect_and_emit(server):
    data = collect_server_metrics(server)
    with cache_lock:
        metrics_cache[server['id']] = data
    socketio.emit('server_update', data)

# ─── SocketIO Events ──────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    with cache_lock:
        for data in metrics_cache.values():
            emit('server_update', data)

@socketio.on('request_refresh')
def on_refresh(data):
    sid = data.get('server_id')
    servers = load_servers()
    server = next((s for s in servers if s['id'] == sid), None)
    if server:
        threading.Thread(target=_collect_and_emit, args=(server,), daemon=True).start()

# ─── Startup ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Start background polling
    poller = threading.Thread(target=polling_loop, daemon=True)
    poller.start()
    logger.info("ServerPulse starting on http://0.0.0.0:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)