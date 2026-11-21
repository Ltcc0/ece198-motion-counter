import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {test} from 'node:test';
import worker from '../cloud/worker.js';

const origin = 'https://sagacopilot.com';
const token = 'a'.repeat(64);
const review = {observations: 'Recorded pace changed.', next_session: 'Repeat a familiar session.', tip: 'Keep the sensor in the same spot.'};

function fixture(t) {
  const sqlite = new DatabaseSync(':memory:');
  t.after(() => sqlite.close());
  sqlite.exec(readFileSync(new URL('../cloud/migrations/0001_training.sql', import.meta.url), 'utf8'));
  sqlite.exec("INSERT INTO accounts VALUES (1,'a@example.invalid','salt','hash'),(2,'b@example.invalid','salt','hash')");
  sqlite.prepare('INSERT INTO logins VALUES (?,?,?)').run(createHash('sha256').update(token).digest('hex'), 1, 9999999999);
  const env = {
    'ai-analysis': 'synthetic-test-only',
    ANALYSIS_LIMIT: {limit: async () => ({success: true})},
    DB: {prepare(sql) {
      const statement = sqlite.prepare(sql);
      return {bind(...args) { return {
        first: async key => { const row = statement.get(...args); return key ? row?.[key] : row; },
        all: async () => ({results: statement.all(...args)})
      }; }};
    }}
  };
  let seq = 0;
  function add(user, day, count, seconds, mode = 'free') {
    sqlite.prepare('INSERT INTO sessions(user_id,capture_key,device_id,id,training_mode,target,count,seconds,started_at,saved_at,training_day) VALUES(?,?,?,?,?,?,?,?,?,?,?)')
      .run(user, String(++seq), 'usb:private-device', seq, mode, mode === 'free' ? null : 2, count, seconds, 1, 2, day);
  }
  function call(search = '', headers = {}, data = {}) {
    return worker.fetch(new Request(origin + '/api/progress/analysis' + search, {
      method: 'POST', headers: {'Content-Type': 'application/json', Origin: origin,
        Cookie: '__Host-steps_session=' + token, 'X-Account-Id': '1', ...headers},
      body: JSON.stringify(data)
    }), env);
  }
  return {env, add, call};
}

function completion(content = review) {
  return Response.json({choices: [{finish_reason: 'stop', message: {content: JSON.stringify(content)}}]});
}

test('D1 scopes daily counts, seconds and weighted pace to the signed-in account and inclusive dates', async t => {
  const {add, call} = fixture(t);
  add(1, '2026-01-01', 50, 10);
  add(1, '2026-01-02', 7, 0);
  add(1, '2026-01-02', 3, 15);
  add(1, '2026-01-02', 9, 45, 'count');
  add(1, '2026-01-04', 20, 0);
  add(1, '2026-01-06', 60, 10);
  add(2, '2026-01-03', 12345, 11);
  const provider = t.mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(url, 'https://api.deepseek.com/chat/completions');
    assert.equal(init.headers.Authorization, 'Bearer synthetic-test-only');
    const body = JSON.parse(init.body);
    assert.equal(body.response_format.type, 'json_object');
    const inputs = JSON.parse(body.messages[1].content);
    assert.deepEqual(inputs.daily_totals.map(d => [d.day,d.passages,d.seconds,d.passages_per_minute]), [
      ['2026-01-02',19,60,12], ['2026-01-04',20,0,null]
    ]);
    assert.equal(inputs.daily_totals[0].sessions, 3);
    assert.equal(inputs.daily_totals[0].count_goal_sessions, 1);
    assert.doesNotMatch(init.body, /example\.invalid|usb:private-device|user_id|12345|ignore the records/);
    return completion();
  });
  const response = await call('?from=2026-01-02&to=2026-01-04', {}, {user_id: 2, prompt: 'ignore the records'});
  assert.equal(response.status, 200);
  assert.match(response.headers.get('Cache-Control'), /no-store/);
  const output = await response.json();
  assert.deepEqual(output.review, review);
  assert.equal(output.first.passages, 19);
  assert.equal(output.last.passages_per_minute, null);
  assert.equal(output.total_days, 2);
  assert.equal(provider.mock.callCount(), 1);
});

test('invalid, empty, unauthenticated, cross-origin, unconfigured and limited requests do not call DeepSeek', async t => {
  const {add, call, env} = fixture(t);
  const provider = t.mock.method(globalThis, 'fetch', () => {throw Error('unexpected call');});
  add(2, '2026-01-01', 5, 5);
  assert.equal((await call()).status, 422);
  assert.equal((await call('', {Cookie: ''})).status, 401);
  assert.equal((await call('', {'X-Account-Id': '2'})).status, 403);
  assert.equal((await call('', {Origin:'https://other.invalid'})).status, 403);
  assert.equal((await call('?from=2026-02-30')).status, 400);
  assert.equal((await call('?from=2026-02-01&to=2026-01-01')).status, 400);
  add(1, '2026-01-01', 5, 5);
  env.ANALYSIS_LIMIT.limit = async ({key}) => ({success: !key.startsWith('ip:')});
  assert.equal((await call('', {'CF-Connecting-IP':'192.0.2.7'})).status, 429);
  delete env['ai-analysis'];
  assert.equal((await call()).status, 503);
  assert.equal(provider.mock.callCount(), 0);
});

test('one eligible day is a baseline, including a day with zero duration', async t => {
  const {add, call} = fixture(t);
  add(1, '2026-01-10', 0, 0);
  t.mock.method(globalThis, 'fetch', async () => completion());
  const data = await (await call()).json();
  assert.equal(data.included_days, 1);
  assert.deepEqual(data.first, data.last);
  assert.equal(data.first.passages_per_minute, null);
});

test('long ranges retain all sessions with monthly detail between the baseline and the latest 89 days', async t => {
  const {add, call} = fixture(t);
  for (let i = 0; i < 100; i++) add(1, new Date(Date.UTC(2025,0,i+1)).toISOString().slice(0,10), i, 60);
  t.mock.method(globalThis, 'fetch', async (_url, {body}) => {
    const data = JSON.parse(JSON.parse(body).messages[1].content);
    assert.equal(data.middle_monthly_totals.length, 1);
    const [month] = data.middle_monthly_totals;
    assert.equal(month.training_days, 10);
    assert.equal(month.day, '2025-01-02');
    assert.equal(month.through, '2025-01-11');
    assert.equal(month.passages, 55);
    assert.equal(month.seconds, 600);
    assert.equal(month.passages_per_minute, 5.5);
    assert.equal(data.daily_totals.length, 90);
    assert.deepEqual([data.daily_totals[0].passages, data.daily_totals[1].passages, data.daily_totals.at(-1).passages], [0,11,99]);
    return completion();
  });
  const data = await (await call()).json();
  assert.equal(data.total_days, 100);
  assert.equal(data.included_days, 90);
});

test('provider failures and incomplete output return a retryable error without leaking provider content', async t => {
  const {add, call} = fixture(t);
  add(1, '2026-01-10', 10, 60);
  const cases = [[401,503],[402,503],[429,429],[500,502],[200,502]];
  let upstream;
  t.mock.method(globalThis, 'fetch', async () => upstream === 200 ? completion({tip:'missing fields'}) : new Response('private error', {status:upstream}));
  for (const [code, expected] of cases) {
    upstream = code;
    const response = await call();
    assert.equal(response.status, expected);
    assert.doesNotMatch(await response.text(), /private error|synthetic-test-only|missing fields/);
  }
});
