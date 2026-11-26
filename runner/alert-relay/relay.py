"""nbci alert relay (Cloud Run, stock python image, no build): Pub/Sub push from topic nbci-alerts ->
one comment on the Pulse task "[nbci] alerts". Sources: Cloud Monitoring incidents (uptime check of the
controller's /healthz: controller or its VM down) and the project budget. Dedupe without state of its own:
a marker object in gs://$BUCKET created with ifGenerationMatch=0 (only the first writer posts).
Env: PULSE_TOKEN (secret nbci-pulse-token), TASK, PULSE_URL, BUCKET. Never logs the token."""
import base64, hashlib, http.server, json, os, time, urllib.error, urllib.parse, urllib.request

TASK = os.environ.get('TASK', '71b8be5b')
URL = os.environ.get('PULSE_URL', 'https://pulse.moonhive.in/mcp')
BUCKET = os.environ.get('BUCKET', '')


def _post(url, body, headers):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(headers, **{'Content-Type': 'application/json', 'User-Agent': 'nbci-alert-relay/1'}), method='POST')
    with urllib.request.urlopen(r, timeout=20) as f:
        return f.headers.get('Mcp-Session-Id'), f.read().decode()


def comment(text):
    h = {'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + os.environ['PULSE_TOKEN']}
    sid, _ = _post(URL, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'nbci-alert-relay', 'version': '1'}}}, h)
    if sid:
        h['Mcp-Session-Id'] = sid
    _, raw = _post(URL, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
        'name': 'add_comment', 'arguments': {'task_id': TASK, 'message': text}}}, h)
    if '"error"' in raw[:400] and '"result"' not in raw[:400]:
        raise RuntimeError('pulse error')


def first(key):
    """True once per key (GCS create-if-absent); True as well when no bucket is configured."""
    if not BUCKET:
        return True
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(
        'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
        headers={'Metadata-Flavor': 'Google'}), timeout=5).read())['access_token']
    u = 'https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=media&ifGenerationMatch=0&name=%s' % (
        BUCKET, urllib.parse.quote('dedupe/' + key, safe=''))
    try:
        urllib.request.urlopen(urllib.request.Request(u, data=b'1', method='POST', headers={
            'Authorization': 'Bearer ' + tok, 'Content-Type': 'text/plain'}), timeout=10)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 412:
            return False
        raise


def forget(key):
    if not BUCKET:
        return
    try:
        tok = json.loads(urllib.request.urlopen(urllib.request.Request(
            'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
            headers={'Metadata-Flavor': 'Google'}), timeout=5).read())['access_token']
        urllib.request.urlopen(urllib.request.Request('https://storage.googleapis.com/storage/v1/b/%s/o/%s' % (
            BUCKET, urllib.parse.quote('dedupe/' + key, safe='')), method='DELETE', headers={'Authorization': 'Bearer ' + tok}), timeout=10)
    except Exception:
        pass


def render(d):
    """(dedupe key, text) for one Pub/Sub payload, or (None, None) to ignore it."""
    inc = d.get('incident')
    if inc:
        st = inc.get('state', '?')
        return ('inc-%s-%s' % (inc.get('incident_id', '?'), st),
                '[nbci] %s: %s. %s (%s)' % ('ALERT OPEN' if st == 'open' else 'resolved', inc.get('policy_name', ''),
                                             inc.get('summary', ''), inc.get('url', '')))
    if 'budgetDisplayName' in d:
        thr = d.get('alertThresholdExceeded')
        if thr is None:
            return None, None
        month = (d.get('costIntervalStart') or time.strftime('%Y-%m'))[:7]
        return ('budget-%s-%s' % (month, thr),
                '[nbci] budget "%s": %d%% reached, cost %s of %s %s this month' % (
                    d['budgetDisplayName'], round(float(thr) * 100), d.get('costAmount'), d.get('budgetAmount'), d.get('currencyCode', '')))
    if d.get('text'):
        return 'msg-' + hashlib.sha1(d['text'].encode()).hexdigest()[:16], '[nbci] ' + d['text']
    return None, None


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            env = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}')
            data = json.loads(base64.b64decode((env.get('message') or {}).get('data') or b'e30=') or b'{}')
            key, text = render(data)
            if key and first(key):
                try:
                    comment(text[:3500]); print('posted', key)
                except Exception:
                    forget(key); raise          # the retry must be able to post
            code = 204
        except Exception as e:      # 500 -> Pub/Sub retries with backoff
            print('relay error', type(e).__name__, str(e)[:160]); code = 500
        self.send_response(code); self.end_headers()


if __name__ == '__main__':
    http.server.ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), H).serve_forever()
