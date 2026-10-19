import requests
import subprocess
import time

API_BASE = 'http://127.0.0.1:8000'
TOKEN = 'blueagent_secret_token_123'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
TEAM_K = 10
NETNS = f'player-ns-{TEAM_K}'
TARGET_IP = f'10.{TEAM_K}.1.10'

print('[*] 1. Teardown existing challenge machines...')
requests.delete(f'{API_BASE}/teams/{TEAM_K}/machines', headers=HEADERS)
time.sleep(1)

print('[*] 2. Deploy fresh challenge container from ubuntu-ssh...')
chal_payload = {
    'challenge_id': 'ssh-challenge-fresh',
    'team_k': TEAM_K,
    'containers': [
        {
            'name': 'target',
            'image': 'ubuntu-ssh',
            'ip_suffix': 10,
            'limits_cpu': '1',
            'limits_memory': '512MB'
        }
    ],
    'readiness_commands': [
        {
            'target': 'container',
            'container_name': 'target',
            'cmd': ['systemctl', 'is-active', 'ssh'],
            'timeout': 15,
            'interval': 1,
            'retry_timeout': 30
        }
    ]
}
t0 = time.time()
res = requests.post(f'{API_BASE}/teams/{TEAM_K}/machines', json=chal_payload, headers=HEADERS)
print(f'Deploy status in {time.time()-t0:.2f}s: {res.status_code}')
if res.status_code not in (200, 201):
    print('Deploy error:', res.text)
    exit(1)

print('[*] 3. Test ping across WireGuard from player-ns-10...')
ping_res = subprocess.run(['sudo', 'ip', 'netns', 'exec', NETNS, 'ping', '-c', '2', '-W', '2', TARGET_IP], capture_output=True, text=True)
print('Ping result:\n', ping_res.stdout)

print('[*] 4. Test SSH login across WireGuard from player-ns-10...')
ssh_res = subprocess.run(
    ['sudo', 'ip', 'netns', 'exec', NETNS, 'ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-i', '/home/debian/.ssh/id_player', f'ubuntu@{TARGET_IP}', 'uptime && whoami && ip -brief addr show dev eth0'],
    capture_output=True, text=True
)
print('SSH exit code:', ssh_res.returncode)
print('SSH stdout:\n', ssh_res.stdout)
if ssh_res.stderr:
    print('SSH stderr:\n', ssh_res.stderr)

