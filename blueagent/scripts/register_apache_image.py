from pathlib import Path
import sqlite3

db_path = Path(__file__).resolve().parent.parent / 'blueagent.db'
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()
cursor.execute('''
INSERT OR REPLACE INTO images (alias, yaml_sha256, yaml_text, status, fingerprint, error, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
''', (
    'ubuntu-apache',
    'sha256-ubuntu-apache',
    'image: ubuntu-apache',
    'ready',
    '78957608da6bca65cdb11aacb86d5dc4cbd8a7084e3fe5ee2983c317e332ed59',
    None,
    '2026-10-06T00:00:00Z',
    '2026-10-06T00:00:00Z'
))
conn.commit()
conn.close()
print("ubuntu-apache registered successfully!")
