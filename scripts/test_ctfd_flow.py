import requests
import re

s = requests.Session()
r = s.get('http://127.0.0.1:4000/login')
nonce = re.search(r'[\'"]csrfNonce[\'"]:\s*[\'"]([a-f0-9]+)[\'"]', r.text).group(1)
login_res = s.post('http://127.0.0.1:4000/login', data={'name': 'admin', 'password': 'admin123', 'nonce': nonce, '_submit': 'Submit'})
print('Login URL:', login_res.url)

chals = s.get('http://127.0.0.1:4000/api/v1/challenges').json()
print('Challenges API response:', chals)

chal_detail = s.get('http://127.0.0.1:4000/api/v1/challenges/1').json()
print('Challenge 1 detail:', chal_detail)
