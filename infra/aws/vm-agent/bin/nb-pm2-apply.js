#!/usr/bin/env node
// Write <service_dir>/.nb-ecosystem.<name>.json and print "reload" when the running app
// matches it (zero-downtime `pm2 reload <file> --only <name>`), else "restart"
// (delete + start of THIS app only). Same contract as the nb_pm2_reload() helper in
// neonbee-deploy-prod.yml. Reads `pm2 jlist` JSON on stdin.
//   env: NB_NAME NB_SCRIPT NB_CWD NB_ECO NB_INST NB_MEM NB_PORT NB_EXEC NB_KILL NB_LISTEN NB_READY
//        NB_ARGS  (catalog `args`, JSON array of strings -> PM2 `args`; e.g. `next start -p <port>`)
//        NB_ENV   (catalog `env`, JSON object of extra string env vars; PORT/NODE_ENV always set)
'use strict';
const fs = require('fs');
const e = process.env;
function json(v, def, ok, what) {
  if (!v || v === 'null') return def;
  let x;
  try { x = JSON.parse(v); } catch (_) { x = undefined; }
  if (!ok(x)) { console.error(`nb-pm2-apply: ${what} must be ${def === null ? 'an array of strings' : 'an object of strings'}`); process.exit(2); }
  return x;
}
const args = json(e.NB_ARGS, null, x => Array.isArray(x) && x.every(a => typeof a === 'string'), 'args');
const extraEnv = json(e.NB_ENV, {}, x => x && typeof x === 'object' && !Array.isArray(x) &&
  Object.values(x).every(a => typeof a === 'string'), 'env');
const cluster = (e.NB_EXEC || 'cluster') !== 'fork';
const app = {
  name: e.NB_NAME, script: e.NB_SCRIPT, cwd: e.NB_CWD,
  exec_mode: cluster ? 'cluster' : 'fork',
  instances: cluster ? (Number(e.NB_INST) || 1) : 1,
  instance_var: 'NODE_APP_INSTANCE',
  max_memory_restart: e.NB_MEM || '1G',
  kill_timeout: Number(e.NB_KILL) || 10000,
  listen_timeout: Number(e.NB_LISTEN) || 15000,
  wait_ready: e.NB_READY === 'true',
  autorestart: true, merge_logs: true,
  env: { ...extraEnv, PORT: String(e.NB_PORT || ''), NODE_ENV: 'production', NB_SCHEDULER_DB_LOCK: 'true' },
};
if (args) app.args = args;
fs.writeFileSync(e.NB_ECO, JSON.stringify({ apps: [app] }, null, 2) + '\n', { mode: 0o640 });
const want = [app.exec_mode, app.instances, app.cwd, app.script, JSON.stringify(app.args || [])].join('|');

let s = '';
process.stdin.on('data', d => { s += d; }).on('end', () => {
  let list;
  try { list = JSON.parse(s.slice(Math.max(0, s.search(/^\[(\{|\])/m)))); } catch (_) { console.log('restart'); return; }
  const p = list.filter(x => x.name === app.name);
  if (!p.length) { console.log('restart'); return; }
  const v = p[0].pm2_env || {};
  const curArgs = Array.isArray(v.args) ? v.args : (v.args ? [String(v.args)] : []);
  const cur = [v.exec_mode === 'cluster_mode' ? 'cluster' : 'fork', p.length, v.pm_cwd, v.pm_exec_path,
    JSON.stringify(curArgs)].join('|');
  console.log(cur === want ? 'reload' : 'restart');
});
