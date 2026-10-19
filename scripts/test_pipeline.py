import subprocess
import time
import requests
from pathlib import Path

TEAM_K = 10
API_BASE = 'http://127.0.0.1:8000'
TOKEN = 'blueagent_secret_token_123'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}

CONF_PATH = Path('/tmp/wgplayer10.conf')
NETNS = f'player-ns-{TEAM_K}'
VETH_HOST = f'veth-h-{TEAM_K}'
VETH_CLIENT = f'veth-c-{TEAM_K}'
HOST_VETH_IP = '192.168.210.1'
CLIENT_VETH_IP = '192.168.210.2'

def cleanup():
    print('[*] Cleaning up player network namespace and veth...')
    subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'wg-quick', 'down', str(CONF_PATH)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['sudo', 'ip', 'link', 'delete', 'dev', VETH_HOST], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['sudo', 'ip', 'netns', 'delete', NETNS], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    CONF_PATH.unlink(missing_ok=True)

try:
    cleanup()

    print(f'[*] Step 1: Create Team {TEAM_K}')
    res = requests.put(f'{API_BASE}/teams/{TEAM_K}', json={'name': 'Test Team 10'}, headers=HEADERS)
    print(f'Create team response: {res.status_code} {res.text}')

    print(f'[*] Step 2: Register Member on Team {TEAM_K}')
    res = requests.put(f'{API_BASE}/teams/{TEAM_K}/members/101', json={'name': 'Player 10'}, headers=HEADERS)
    print(f'Add member response: {res.status_code} {res.text}')

    print(f'[*] Step 3: Fetch WireGuard client configuration')
    res = requests.get(f'{API_BASE}/teams/{TEAM_K}/members/101/config', headers=HEADERS)
    print(f'Config status: {res.status_code}')
    wg_conf = res.text
    print('--- WireGuard Client Config ---')
    print(wg_conf)
    print('-------------------------------')

    print(f'[*] Step 4: Deploy challenge machines')
    chal_payload = {
        'challenge_id': 'ssh-challenge',
        'team_k': TEAM_K,
        'containers': [
            {
                'name': 'target',
                'image': 'ubuntu-distro',
                'ip_suffix': 10,
                'limits_cpu': '1',
                'limits_memory': '512MB'
            }
        ],
        'readiness_commands': [
            {
                'target': 'container',
                'container_name': 'target',
                'cmd': ['ip', 'addr', 'show', 'dev', 'eth0'],
                'timeout': 15
            }
        ]
    }
    t0 = time.time()
    res = requests.post(f'{API_BASE}/teams/{TEAM_K}/machines', json=chal_payload, headers=HEADERS)
    print(f'Deploy machines response in {time.time()-t0:.2f}s: {res.status_code} {res.text}')

    print(f'[*] Step 5: Setup isolated competitor namespace ({NETNS})')
    subprocess.run(['sudo', 'ip', 'netns', 'add', NETNS], check=True)
    subprocess.run(['sudo', 'ip', 'link', 'add', VETH_HOST, 'type', 'veth', 'peer', 'name', VETH_CLIENT], check=True)
    subprocess.run(['sudo', 'ip', 'link', 'set', VETH_CLIENT, 'netns', NETNS], check=True)
    subprocess.run(['sudo', 'ip', 'addr', 'add', f'{HOST_VETH_IP}/24', 'dev', VETH_HOST], check=True)
    subprocess.run(['sudo', 'ip', 'link', 'set', VETH_HOST, 'up'], check=True)

    subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ip', 'addr', 'add', f'{CLIENT_VETH_IP}/24', 'dev', VETH_CLIENT], check=True)
    subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ip', 'link', 'set', VETH_CLIENT, 'up'], check=True)
    subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ip', 'link', 'set', 'lo', 'up'], check=True)
    subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ip', 'route', 'add', 'default', 'via', HOST_VETH_IP], check=True)

    # Point WireGuard client config to HOST_VETH_IP for local simulation
    wg_port = 51820 + TEAM_K
    sim_conf = wg_conf.replace(f'192.168.1.107:{wg_port}', f'{HOST_VETH_IP}:{wg_port}').replace(f'127.0.0.1:{wg_port}', f'{HOST_VETH_IP}:{wg_port}')
    CONF_PATH.write_text(sim_conf, encoding='utf-8')

    print(f'[*] Step 6: Bring up WireGuard client in {NETNS}')
    up_res = subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'wg-quick', 'up', str(CONF_PATH)], capture_output=True, text=True)
    print('wg-quick output:', up_res.stdout, up_res.stderr)

    time.sleep(1)

    print('[*] Step 7: Check WireGuard status')
    wg_show = subprocess.run(['sudo', 'wg', 'show', f'wg-{TEAM_K}'], capture_output=True, text=True)
    print('Server wg show:\n', wg_show.stdout)

    target_ip = f'10.{TEAM_K}.1.10'
    print(f'[*] Step 8: Ping container at {target_ip} from {NETNS} across WireGuard')
    ping_res = subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ping', '-c', '3', '-W', '2', target_ip], capture_output=True, text=True)
    print('Ping output:\n', ping_res.stdout, ping_res.stderr)

    print(f'[*] Step 9: Try SSH access to {target_ip} from {NETNS} across WireGuard')
    ssh_res = subprocess.run(
        ['sudo', 'ip', 'netns', 'exec', NETNS, 'ssh', '-v', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5', f'ubuntu@{target_ip}'],
        capture_output=True, text=True
    )
    print('SSH returncode:', ssh_res.returncode)
    print('SSH stdout:', ssh_res.stdout)
    print('SSH stderr:\n', ssh_res.stderr)

    print(f'[*] Step 10: Check TCP port 22 on {target_ip}')
    nc_res = subprocess.run(
        ['sudo', 'ip', 'netns', 'exec', NETNS, 'python3', '-c', f'''
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect(({target_ip}, 22))
    print(TCP Port 22 is OPEN!)
except Exception as e:
    print(TCP Port 22 connection failed:, e)
finally:
    s.close()
'''],
        capture_output=True, text=True
    )
    print('TCP probe output:', nc_res.stdout, nc_res.stderr)

finally:
    print('[*] Script finished.')
