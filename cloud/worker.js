import {HttpError, json} from './http.js';
import {analyzeTraining} from './training-analysis.js';

const WEEK = 7 * 86400;
const COOKIE = '__Host-steps_session';
const encoder = new TextEncoder();
const hex = bytes => Array.from(new Uint8Array(bytes), n => n.toString(16).padStart(2, '0')).join('');
const now = () => Math.floor(Date.now() / 1000);

function cookie(value, age) {
  return `${COOKIE}=${value}; Max-Age=${age}; Path=/; Secure; HttpOnly; SameSite=Strict`;
}

async function hashToken(token) {
  return hex(await crypto.subtle.digest('SHA-256', encoder.encode(token)));
}

async function passwordHash(password, salt, secret) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret),
    {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  const input = await crypto.subtle.sign('HMAC', key, encoder.encode(password));
  const material = await crypto.subtle.importKey('raw', input, 'PBKDF2', false, ['deriveBits']);
  return hex(await crypto.subtle.deriveBits({name: 'PBKDF2', hash: 'SHA-256',
    salt: encoder.encode(salt), iterations: 100000}, material, 256));
}

async function login(request, env, data, register) {
  const rate = await env.AUTH_LIMIT.limit({key: request.headers.get('CF-Connecting-IP') || 'local'});
  if (!rate.success) throw new HttpError(429, 'Too many attempts. Try again in a minute.');
  const email = typeof data.email === 'string' ? data.email.trim().toLowerCase() : '';
  if (email.length > 254 || !/^[^@\s\x00-\x1f]+@[^@\s\x00-\x1f]+$/u.test(email)) throw new HttpError(400, 'Enter a valid email.');
  if (typeof data.password !== 'string' || !data.password.length || data.password.length > 256)
    throw new HttpError(400, 'Enter a password of up to 256 characters.');
  if (!env.PASSWORD_PEPPER) throw new Error('Missing password secret');
  const token = hex(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await hashToken(token);
  let account;
  if (register) {
    const salt = hex(crypto.getRandomValues(new Uint8Array(16)));
    const verifier = await passwordHash(data.password, salt, env.PASSWORD_PEPPER);
    account = await env.DB.prepare('INSERT INTO accounts(email,salt,verifier) VALUES(?,?,?) ON CONFLICT(email) DO NOTHING RETURNING id,email')
      .bind(email, salt, verifier).first();
    if (!account) throw new HttpError(409, 'This email is already registered. Sign in to continue.');
  } else {
    const row = await env.DB.prepare('SELECT * FROM accounts WHERE email=?').bind(email).first();
    const verifier = await passwordHash(data.password, row?.salt || '0'.repeat(32), env.PASSWORD_PEPPER);
    if (!row || !crypto.subtle.timingSafeEqual(encoder.encode(verifier), encoder.encode(row.verifier)))
      throw new HttpError(401, 'Email or password is incorrect.');
    account = {id: row.id, email: row.email};
  }
  await env.DB.batch([
    env.DB.prepare('DELETE FROM logins WHERE expires_at<=?').bind(now()),
    env.DB.prepare('INSERT INTO logins VALUES(?,?,?)').bind(digest, account.id, now() + WEEK)
  ]);
  return json({ok:true, account}, 200, {'Set-Cookie': cookie(token, WEEK)});
}

async function identity(request, db) {
  const token = (request.headers.get('Cookie') || '').match(/(?:^|;\s*)__Host-steps_session=([a-f0-9]{64})(?:;|$)/)?.[1];
  if (!token) return null;
  const digest = await hashToken(token);
  const account = await db.prepare('SELECT a.id,a.email FROM accounts a JOIN logins l ON a.id=l.user_id WHERE l.digest=? AND l.expires_at>?')
    .bind(digest, now()).first();
  return account && {account, digest};
}

const uint32 = n => Number.isInteger(n) && n >= 0 && n <= 4294967295;
function capture(data) {
  const {capture_key, device_id, id, count, seconds, started_at, saved_at, timezone, training_mode, target} = data;
  if (typeof capture_key !== 'string' || !/^[0-9a-f-]{36}$/.test(capture_key) ||
      typeof device_id !== 'string' || !/^usb:[a-zA-Z0-9:-]{1,64}$/.test(device_id) ||
      !uint32(id) || !id || !uint32(count) || !uint32(seconds) ||
      !Number.isFinite(started_at) || started_at <= 0 || !Number.isFinite(saved_at) ||
      saved_at < started_at || saved_at > now() + 600 ||
      !['free','count','timed'].includes(training_mode) ||
      (training_mode === 'free' ? target !== null : (!uint32(target) || !target)))
    throw new HttpError(400, 'Invalid training summary.');
  if (typeof timezone !== 'string' || timezone.length > 100) throw new HttpError(400, 'Invalid time zone.');
  let day;
  try {
    const parts = new Intl.DateTimeFormat('en', {timeZone: timezone, year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(started_at * 1000);
    const value = key => parts.find(part => part.type === key).value;
    day = [value('year'),value('month'),value('day')].join('-');
  } catch { throw new HttpError(400, 'Invalid training date or time zone.'); }
  return [capture_key, device_id, id, training_mode, target, count, seconds, started_at, saved_at, day];
}

function dates(params) {
  const bounds = ['from','to'].map(key => params.get(key) || '');
  for (const value of bounds) {
    if (!value) continue;
    const date = new Date(value + 'T00:00:00Z');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || isNaN(date) || date.toISOString().slice(0,10) !== value)
      throw new HttpError(400, 'Choose valid training dates.');
  }
  if (bounds[0] && bounds[1] && bounds[0] > bounds[1]) throw new HttpError(400, 'Choose an end date on or after the start date.');
  return [bounds[0] || '0000-01-01', bounds[1] || '9999-12-31'];
}

function reports(history) {
  const groups = new Map();
  for (const row of history) {
    const key = row.training_mode + ':' + row.target;
    if (!groups.has(key)) groups.set(key, {training_mode:row.training_mode, target:row.target, days:new Map()});
    const days = groups.get(key).days;
    if (!days.has(row.training_day)) days.set(row.training_day, {day:row.training_day, sessions:0, count:0, seconds:0, timed_count:0, goals_met:0});
    const day = days.get(row.training_day);
    day.sessions++; day.count += row.count; day.seconds += row.seconds;
    if (row.seconds) day.timed_count += row.count;
    if (row.target !== null && (row.training_mode === 'timed' ? row.seconds : row.count) >= row.target) day.goals_met++;
  }
  return Array.from(groups.values(), group => {
    const days = Array.from(group.days.values()).sort((a,b) => a.day.localeCompare(b.day));
    for (const day of days) {
      day.pace = day.seconds ? day.timed_count * 60 / day.seconds : null;
      delete day.timed_count;
    }
    const baseline = days.find(d => d.pace !== null), latest = days.at(-1);
    return {...group, days, baseline_day:baseline?.day ?? null,
      delta:baseline && baseline !== latest && latest.pace !== null ? latest.pace - baseline.pace : null};
  });
}

function csv(history, account) {
  const fields = ['device_id','id','count','seconds','saved_at','source','mode','user_id','profile','training_mode','target','started_at','training_day'];
  const escape = value => '"' + String(value ?? '').replace(/^[=+@-]/, "'$&").replaceAll('"','""') + '"';
  const lines = history.map(row => fields.map(key => escape(key === 'profile' ? account.email : row[key])).join(','));
  return new Response('\uFEFF' + [fields.join(','), ...lines].join('\r\n') + '\r\n',
    {headers:{'Content-Type':'text/csv; charset=utf-8','Cache-Control':'no-store','Content-Disposition':'attachment; filename="my-training.csv"'}});
}

async function dispatch(request, env) {
  const url = new URL(request.url);
  if (url.hostname === 'www.sagacopilot.com' || (url.hostname === 'sagacopilot.com' && url.protocol === 'http:')) {
    url.hostname = 'sagacopilot.com';
    url.protocol = 'https:';
    return Response.redirect(url.toString(), 308);
  }
  const path = url.pathname;
  if (path === '/api/health') return json({ok:true, application:'ece198-motion-counter', deployment:'cloudflare'});
  const isHome = path === '/' || path === '/index.html';
  if (!isHome && !path.startsWith('/api/')) return env.ASSETS.fetch(request);
  let data;
  if (request.method === 'POST') {
    if (request.headers.get('Origin') !== url.origin) throw new HttpError(403, 'Same-origin requests only.');
    if (!/^application\/json(?:;|$)/i.test(request.headers.get('Content-Type') || '')) throw new HttpError(415, 'Send JSON.');
    if (Number(request.headers.get('Content-Length')) > 4096) throw new HttpError(413, 'Request too large.');
    const reader = request.body?.getReader();
    let bytes = 0, chunks = [];
    if (reader) while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      bytes += value.length;
      if (bytes > 4096) { await reader.cancel(); throw new HttpError(413, 'Request too large.'); }
      chunks.push(value);
    }
    try { data = JSON.parse(await new Blob(chunks).text()); } catch { throw new HttpError(400, 'Invalid JSON.'); }
    if (!data || Array.isArray(data) || typeof data !== 'object') throw new HttpError(400, 'Send a JSON object.');
    if (path === '/api/login' || path === '/api/register') return login(request, env, data, path === '/api/register');
  }
  const auth = await identity(request, env.DB);
  if (isHome && (request.method === 'GET' || request.method === 'HEAD')) {
    if (!auth) return new Response(null, {status:303, headers:{Location:'/login.html', 'Cache-Control':'no-store'}});
    const page = await env.ASSETS.fetch(new Request(new URL('/index.html', url), request));
    const result = new Response(page.body, page);
    result.headers.set('Cache-Control', 'no-store');
    return result;
  }
  if (!auth) throw new HttpError(401, 'Sign in to continue.');
  const {account, digest} = auth;
  const expected = request.headers.get('X-Account-Id') ?? url.searchParams.get('user');
  if (expected !== null && expected !== String(account.id)) throw new HttpError(403, 'The signed-in account changed.');
  if (request.method === 'POST' && path === '/api/logout') {
    await env.DB.prepare('DELETE FROM logins WHERE digest=?').bind(digest).run();
    return json({ok:true}, 200, {'Set-Cookie':cookie('', 0)});
  }
  if (request.method === 'POST' && path === '/api/progress/analysis') {
    return analyzeTraining(env, account.id, dates(url.searchParams), request.headers.get('CF-Connecting-IP'));
  }
  if (request.method === 'POST' && path === '/api/sessions') {
    const values = capture(data);
    await env.DB.prepare('INSERT INTO sessions(user_id,capture_key,device_id,id,training_mode,target,count,seconds,started_at,saved_at,training_day) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,capture_key) DO NOTHING')
      .bind(account.id,...values).run();
    return json({ok:true});
  }
  if (request.method === 'GET' && path === '/api/state') {
    const revision = await env.DB.prepare('SELECT coalesce(max(row_id),0) AS n FROM sessions WHERE user_id=?').bind(account.id).first('n');
    return json({account, transport:'web-serial', archive_revision:revision, analysis_available:Boolean(env['ai-analysis'])});
  }
  if (request.method === 'GET' && ['/api/records','/api/export.csv'].includes(path)) {
    const rows = await env.DB.prepare("SELECT id,device_id,user_id,count,seconds,started_at,saved_at,training_day,training_mode,target,'PASSAGE' AS mode,'board' AS source FROM sessions WHERE user_id=? AND training_day BETWEEN ? AND ? ORDER BY started_at DESC,row_id DESC")
      .bind(account.id, ...dates(url.searchParams)).all();
    return path === '/api/export.csv' ? csv(rows.results, account) : json({history:rows.results, reports:reports(rows.results)});
  }
  throw new HttpError(404, 'Unknown endpoint.');
}

export default {
  async fetch(request, env) {
    try { return await dispatch(request, env); }
    catch (error) {
      return json({ok:false, message:error instanceof HttpError ? error.message : 'Service temporarily unavailable.'}, error.status || 503);
    }
  }
};
