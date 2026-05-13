#!/bin/sh
set -e

# docker-entrypoint.sh
# Wait for critical dependencies (Postgres, Redis, optional Dhan /profile)
# then exec the container CMD (usually `uv run python -m yukti`).

WAIT_TIMEOUT=${WAIT_TIMEOUT:-120}
WAIT_INTERVAL=${WAIT_INTERVAL:-2}

echo "entrypoint: waiting for dependencies (timeout=${WAIT_TIMEOUT}s interval=${WAIT_INTERVAL}s)"

python - <<'PY'
import os, sys, time, socket, urllib.parse
try:
    import httpx
except Exception:
    httpx = None

def parse_postgres_url(url):
    if not url:
        return 'postgres', 5432
    try:
        # Example: postgresql+psycopg://user:pass@postgres:5432/db
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or 'postgres'
        port = parsed.port or 5432
        return host, port
    except Exception:
        return 'postgres', 5432

def parse_redis_url(url):
    if not url:
        return 'redis', 6379
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or 'redis'
        port = parsed.port or 6379
        return host, port
    except Exception:
        return 'redis', 6379

def wait_tcp(host, port, retries, interval, name):
    for i in range(retries):
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"{name} reachable at {host}:{port}")
                return True
        except Exception as e:
            print(f"{name} not reachable at {host}:{port} ({e}), retrying ({i+1}/{retries})")
            time.sleep(interval)
    print(f"{name} not reachable after {retries} attempts")
    return False

def check_dhan(base_url, token, client_id, retries, interval):
    if not base_url:
        print("DHAN base url not set; skipping DHAN check")
        return True
    if not httpx:
        print("httpx library not available; skipping DHAN check")
        return True
    url = base_url.rstrip('/') + '/profile'
    headers = {}
    if token:
        headers['access-token'] = token
    if client_id:
        headers['dhanClientId'] = client_id
    for i in range(retries):
        try:
            r = httpx.get(url, headers=headers, timeout=5.0)
            print(f"DHAN /profile status={r.status_code}")
            if r.status_code == 200:
                return True
            else:
                print('DHAN profile returned non-200, body:', r.text[:200])
        except Exception as e:
            print('DHAN check error:', e)
        time.sleep(interval)
    print('DHAN check failed after retries')
    return False

def main():
    wait_timeout = int(os.environ.get('WAIT_TIMEOUT', '120'))
    wait_interval = int(os.environ.get('WAIT_INTERVAL', '2'))
    retries = max(1, wait_timeout // wait_interval)

    pg_url = os.environ.get('POSTGRES_URL') or os.environ.get('DATABASE_URL')
    pg_host, pg_port = parse_postgres_url(pg_url)
    if not wait_tcp(pg_host, pg_port, retries, wait_interval, 'Postgres'):
        sys.exit(1)

    redis_url = os.environ.get('REDIS_URL')
    redis_host, redis_port = parse_redis_url(redis_url)
    if not wait_tcp(redis_host, redis_port, retries, wait_interval, 'Redis'):
        sys.exit(1)

    wait_for_dhan = os.environ.get('WAIT_FOR_DHAN', 'true').lower() not in ('0','false','no')
    dhan_base = os.environ.get('DHAN_BASE_URL') or os.environ.get('DHAN_URL') or ''
    dhan_token = os.environ.get('DHAN_ACCESS_TOKEN') or ''
    dhan_client_id = os.environ.get('DHAN_CLIENT_ID') or ''
    if wait_for_dhan and dhan_token and dhan_client_id:
        ok = check_dhan(dhan_base, dhan_token, dhan_client_id, retries, wait_interval)
        if not ok:
            sys.exit(1)

    print('All dependency checks passed')

if __name__ == '__main__':
    main()
PY

echo "entrypoint: launching command: $@"

# If the user invoked `python` directly (e.g. docker run ... python -m yukti ...)
# prefix the command with `uv run` so it runs inside the uv-managed environment
# where dependencies from pyproject.toml were installed.
first_cmd=$(basename "$1" 2>/dev/null || echo "")
if [ "$first_cmd" = "python" ] || [ "$first_cmd" = "python3" ]; then
    echo "entrypoint: prefixing python command with 'uv run' to use uv virtualenv"
    exec uv run "$@"
else
    exec "$@"
fi
