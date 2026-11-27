"""I/O clients for the nb-ci controller: GitHub (App or fine-grained token, ETag cache, rate governor),
GCE + Secret Manager (attached service account via the metadata server; gcloud only off-GCP), Teams."""
import base64, json, os, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request

E = os.environ.get
META = 'http://metadata.google.internal/computeMetadata/v1/'


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def req(url, method='GET', body=None, headers=None, raw=False, timeout=30):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault('Content-Type', 'application/json')
    r = urllib.request.Request(url, method=method, headers=h, data=data)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            b = f.read()
            return f.status, (b if raw else (json.loads(b) if b else {})), f.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, e.headers
        raise RuntimeError('%s %s -> %s %s' % (method, url.split('?')[0], e.code, e.read()[:400].decode('utf-8', 'ignore')))


def meta(path):
    _, b, _ = req(META + path, headers={'Metadata-Flavor': 'Google'}, raw=True, timeout=5)
    return b.decode()


# ── GCP ─────────────────────────────────────────────────────────────────────────────────────────────
def token_cache_s(expires_in, margin_s=300, floor_s=30):
    """Seconds to reuse a fresh GCP access token: its remaining life less a margin, never below floor_s
    (unknown/short life -> re-read soon). Pure."""
    return max(floor_s, int(expires_in or 0) - margin_s)


class GCP:
    # Refresh a metadata token this long before GCP says it expires. The metadata server hands out its cached
    # token, which can have only minutes left: a fixed 25-min cache sent expired tokens for up to ~20 min
    # (2026-11-27: controller listed 0 VMs on 401s, idle VMs never deleted, the guard then stopped nbci).
    TOKEN_MARGIN_S = 300

    def __init__(self, project):
        self.project, self._tok = project, (0, '')     # (unix time the token stops being used, token)

    def token(self):
        if time.time() >= self._tok[0]:
            try:
                d = json.loads(meta('instance/service-accounts/default/token'))
                t, ttl = d['access_token'], int(d.get('expires_in', 0))
            except Exception:   # off-GCP read-only checks: GCP_ACCESS_TOKEN, else gcloud user credentials
                if E('GCP_ACCESS_TOKEN'):
                    self._tok = (time.time() + 1500, E('GCP_ACCESS_TOKEN')); return self._tok[1]
                t, ttl = subprocess.check_output([E('GCLOUD', 'gcloud'), 'auth', 'print-access-token'], text=True, timeout=60).strip(), 1800
            self._tok = (time.time() + token_cache_s(ttl, self.TOKEN_MARGIN_S), t)
        return self._tok[1]

    def invalidate(self):
        self._tok = (0, '')

    def _authed(self, url, method='GET', body=None, headers=None, timeout=30):
        """One GCP call; a 401 (token revoked/expired early) drops the cached token and retries once."""
        for attempt in (1, 2):
            try:
                return req(url, method, body, dict(headers or {}, Authorization='Bearer ' + self.token()), timeout=timeout)
            except RuntimeError as e:
                if attempt == 1 and ' -> 401 ' in str(e):
                    self.invalidate(); continue
                raise

    def compute(self, path, method='GET', body=None):
        return self._authed('https://compute.googleapis.com/compute/v1/projects/%s%s' % (self.project, path), method, body)[1]

    def secret(self, name):
        d = self._authed('https://secretmanager.googleapis.com/v1/projects/%s/secrets/%s/versions/latest:access' % (self.project, name))[1]
        return base64.b64decode(d['payload']['data']).decode()

    def storage_json(self, bucket, name):
        """One small JSON object from GCS (raises RuntimeError '... 404 ...' when absent)."""
        import urllib.parse
        return self._authed('https://storage.googleapis.com/storage/v1/b/%s/o/%s?alt=media' % (bucket, urllib.parse.quote(name, safe='')),
                            timeout=20)[1]

    def storage_exists(self, bucket, name):
        try:
            req('https://storage.googleapis.com/storage/v1/b/%s/o/%s' % (bucket, urllib.parse.quote(name, safe='')),
                headers={'Authorization': 'Bearer ' + self.token()}, timeout=20)
            return True
        except RuntimeError as e:
            if ' 404 ' in str(e):
                return False
            raise

    def storage_upload(self, bucket, name, data, ctype='application/gzip'):
        """Create-only upload (ifGenerationMatch=0): an object that already exists is left as it is."""
        try: req('https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=media&name=%s&ifGenerationMatch=0' % (
            bucket, urllib.parse.quote(name, safe='')), 'POST', data,
            {'Authorization': 'Bearer ' + self.token(), 'Content-Type': ctype}, timeout=120)
        except RuntimeError as e:
            if ' 412 ' not in str(e):
                raise

    def wait_op(self, op, zone=None, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            path = ('/zones/%s/operations/%s' % (zone, op['name'])) if zone else ('/global/operations/%s' % op['name'])
            op = self.compute(path)
            if op.get('status') == 'DONE':
                if op.get('error'):
                    raise RuntimeError(json.dumps(op['error'])[:400])
                return op
            time.sleep(3)
        raise RuntimeError('operation %s timed out' % op['name'])


# ── GitHub ──────────────────────────────────────────────────────────────────────────────────────────
def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


class GitHub:
    """App auth (preferred: no expiry, 6.7k req/h for 54 repos) or a fine-grained token (GH_TOKEN_FILE).
    The App private key is read from Secret Manager into memory; openssl signs from a 0600 tmpfs file
    that is removed right after signing."""

    def __init__(self, gcp=None):
        self.gcp, self._tok, self._etag = gcp, (0, ''), {}
        self.remaining, self.reset = 5000, 0
        self.calls, self.not_modified = 0, 0          # rate-limited calls vs free 304s (daily report)
        self.app_id, self.inst = E('GH_APP_ID'), E('GH_APP_INSTALLATION_ID')

    def jwt(self):
        now = int(time.time())
        head = _b64u(b'{"alg":"RS256","typ":"JWT"}')
        body = _b64u(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': self.app_id}).encode())
        key = self.gcp.secret(E('GH_APP_KEY_SECRET', 'nbci-github-app-key'))
        fd, path = tempfile.mkstemp(dir=E('RUNTIME_DIRECTORY', '/tmp'))
        try:
            os.write(fd, key.encode()); os.close(fd)
            sig = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', path], input=(head + '.' + body).encode(),
                                 capture_output=True, check=True).stdout
        finally:
            os.unlink(path)
        return head + '.' + body + '.' + _b64u(sig)

    def token(self):
        if E('GH_TOKEN'):                     # off-GCP read-only checks only
            return E('GH_TOKEN')
        if E('GH_TOKEN_FILE'):
            return open(E('GH_TOKEN_FILE')).read().strip()
        if time.time() - self._tok[0] > 2700:
            _, d, _ = req('https://api.github.com/app/installations/%s/access_tokens' % self.inst, 'POST', {},
                          {'Authorization': 'Bearer ' + self.jwt(), 'Accept': 'application/vnd.github+json'})
            self._tok = (time.time(), d['token'])
        return self._tok[1]

    def __call__(self, path, method='GET', body=None, cache=False, app=False):
        h = {'Authorization': 'Bearer ' + (self.jwt() if app else self.token()),
             'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if cache and path in self._etag:
            h['If-None-Match'] = self._etag[path][0]     # a 304 does not count against the rate limit
        code, data, hd = req('https://api.github.com/' + path, method, body, h)
        if hd and hd.get('X-RateLimit-Remaining'):
            self.remaining, self.reset = int(hd['X-RateLimit-Remaining']), int(hd.get('X-RateLimit-Reset', 0))
        if code == 304:
            self.not_modified += 1
        else:
            self.calls += 1
        if cache:
            if code == 304:
                return self._etag[path][1]
            if hd.get('ETag'):
                self._etag[path] = (hd['ETag'], data)
        return data

    def low(self, floor=1000):
        """Rate governor: below `floor` calls left, non-essential work (train reconcile, reports) waits."""
        return self.remaining < floor and time.time() < self.reset


def teams(text, dry):
    hook = E('TEAMS_WEBHOOK')
    log('TEAMS', text)
    if hook and not dry:
        try: req(hook, 'POST', {'text': '[nb-ci] ' + text})
        except Exception as e: log('teams failed', e)


def job_log(gh, org, repo, job_id, max_bytes=3_000_000):
    """Plain-text log of one Actions job (the API redirects to blob storage, fetched without auth)."""
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    url = 'https://api.github.com/repos/%s/%s/actions/jobs/%s/logs' % (org, repo, job_id)
    r = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + gh.token(), 'Accept': 'application/vnd.github+json'})
    try:
        urllib.request.build_opener(NoRedir).open(r, timeout=20)
        return ''
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            return ''
        loc = e.headers['Location']
    with urllib.request.urlopen(loc, timeout=30) as f:
        b = f.read(max_bytes)
    return b.decode('utf-8', 'ignore')
