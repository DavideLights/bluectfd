import asyncio
import os
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, '/home/debian/bluectfd/blueagent/src')
os.environ['BLUEAGENT_CTFD_API_TOKEN'] = 'secret-token'

from tests.usability.client import BlueAgentTestClient

async def main():
    client = BlueAgentTestClient(base_url='http://127.0.0.1:8000', token='secret-token')
    
    print('[*] Checking server health...')
    ready = await client.wait_server_ready(max_seconds=5.0)
    print(f'[+] Server ready: {ready}')
    
    team_k = 1
    print(f'[*] Creating team {team_k}...')
    r = await client.create_team(team_k=team_k, name='Smoke Team')
    print('Create team status:', r.status_code, r.text[:100])
    
    payload = {
        'challenge_id': 'smoke-ch',
        'team_k': team_k,
        'containers': [
            {'name': 'web', 'image': 'alpine', 'ip_suffix': 2}
        ],
        'readiness_commands': [],
        'verify_commands': [
            {'cmd': ['echo', 'smoke_pass'], 'target': 'container', 'container_name': 'web'}
        ],
        'verify_internet_access': False
    }
    
    print('[*] Deploying challenge...')
    t0 = time.perf_counter()
    r = await client.deploy_challenge(payload, team_k=team_k)
    deploy_sec = time.perf_counter() - t0
    print(f'Deploy status: {r.status_code} in {deploy_sec:.3f}s')
    if r.status_code in (200, 201):
        d = r.json()
        print('Deploy step timings:', d.get('step_timings'))
    else:
        print('Deploy error:', r.text)
        return

    print('[*] Verifying challenge...')
    t0 = time.perf_counter()
    r = await client.verify_challenge(team_k=team_k, config_payload=payload)
    verify_sec = time.perf_counter() - t0
    print(f'Verify status: {r.status_code} in {verify_sec:.3f}s')
    if r.status_code in (200, 201):
        v = r.json()
        print('Passed:', v.get('passed'))
        print('Verify step timings:', v.get('details', {}).get('step_timings'))
    else:
        print('Verify error:', r.text)

    print('[*] Cleaning up...')
    await client.teardown_challenge(team_k=team_k, config_payload=payload)
    await client.delete_team(team_k=team_k)
    print('[+] Cleanup complete.')

if __name__ == '__main__':
    asyncio.run(main())
