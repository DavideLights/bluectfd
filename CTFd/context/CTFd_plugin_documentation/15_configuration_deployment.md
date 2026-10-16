# BlueAgent CTFd Plugin: Configuration & Deployment Guide

## Module: `CTFd/CTFd/plugins/blueagent/config.py`

---

## 1. Configuration Precedence

Settings are resolved dynamically using a three-tier hierarchy:

```
1. CTFd Dynamic Configuration Database (CTFd.utils.get_config)
   ▲ (Admin UI changes take immediate effect without container restarts)
   │
2. Environment Variables (e.g. BLUEAGENT_URL, BLUEAGENT_TOKEN)
   ▲ (Injected via Docker Compose / .env)
   │
3. Safe Hardcoded Defaults (http://127.0.0.1:8000)
```

---

## 2. Configuration Parameters

| Parameter | Environment Variable | Default Value | Description |
|---|---|---|---|
| `blueagent_url` | `BLUEAGENT_URL` | `http://127.0.0.1:8000` | HTTP root URL for BlueAgent v2 REST orchestrator. When running CTFd inside Docker, use Linux bridge IP `http://172.17.0.1:8000`. |
| `blueagent_token` | `BLUEAGENT_TOKEN` | `blueagent_secret_token_123` | Secret bearer token validated by BlueAgent auth middleware. |
| `blueagent_verify_cooldown`| `BLUEAGENT_VERIFY_COOLDOWN`| `30` | Global default seconds cooldown enforced per team between verification submissions. |
| `blueagent_connect_timeout`| `BLUEAGENT_CONNECT_TIMEOUT`| `5.0` | Connection timeout in seconds. |
| `blueagent_read_timeout` | `BLUEAGENT_READ_TIMEOUT` | `10.0` | HTTP read timeout for standard queries. |

---

## 3. Docker Compose Integration

When running CTFd in Docker and BlueAgent on the Debian 13 host:

```yaml
services:
  ctfd:
    build: .
    environment:
      - BLUEAGENT_URL=http://172.17.0.1:8000
      - BLUEAGENT_TOKEN=blueagent_secret_token_123
      - BLUEAGENT_VERIFY_COOLDOWN=30
    volumes:
      - ./CTFd/plugins/blueagent:/opt/CTFd/CTFd/plugins/blueagent:ro
    ports:
      - "4000:8000"
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### Verification
Test connectivity from inside CTFd container:
```bash
docker compose exec ctfd curl -sf http://172.17.0.1:8000/health
```
