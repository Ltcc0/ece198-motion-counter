import {HttpError} from './http.js';

const MAX_DAYS = 90;
const MODEL = 'deepseek-flash';
const ENDPOINT = 'https://api.deepseek.com/chat/completions';

// Count all passages toward volume. Only sessions with positive duration contribute to pace.
const dailySql = `
  WITH daily AS (
    SELECT training_day AS day, COUNT(*) AS sessions, SUM(count) AS passages,
           SUM(seconds) AS seconds,
           SUM(CASE WHEN seconds > 0 THEN count ELSE 0 END) AS timed_passages,
           SUM(training_mode = 'free') AS free_sessions,
           SUM(training_mode = 'count') AS count_goal_sessions,
           SUM(training_mode = 'timed') AS timed_sessions
    FROM sessions
    WHERE user_id = ? AND training_day BETWEEN ? AND ?
    GROUP BY training_day
  ), ranked AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY day) AS position, COUNT(*) OVER () AS total_days
    FROM daily
  ), labeled AS (
    SELECT *, CASE WHEN position = 1 OR position > total_days - ? THEN day
                   ELSE substr(day, 1, 7) END AS period
    FROM ranked
  )
  SELECT period, MIN(day) AS day, MAX(day) AS through, COUNT(*) AS training_days,
         SUM(sessions) AS sessions, SUM(passages) AS passages, SUM(seconds) AS seconds,
         ROUND(60.0 * SUM(timed_passages) / NULLIF(SUM(seconds), 0), 1) AS passages_per_minute,
         SUM(free_sessions) AS free_sessions, SUM(count_goal_sessions) AS count_goal_sessions,
         SUM(timed_sessions) AS timed_sessions
  FROM labeled GROUP BY period ORDER BY MIN(day)`;

const systemPrompt = `Write a brief training-log review in English for a movement counter. The supplied numbers come from a sensor that counts each crossing as one passage. Daily observations combine all sessions and modes. Compare the first and last training days for passages, duration and passages per minute using the supplied data. Mention intervening variation only when supported. Changes in the number of sessions, their duration, or their modes can affect these metrics. Do not infer health, recovery, diagnosis, or movement quality. Null pace means no measurable duration. With one training day, describe a baseline and explicitly say that change cannot yet be assessed. For long ranges the middle dates are combined by month. Use these totals as context, accounting for the number of training days; do not invent individual days or infer a continuous trend.
Suggest one concrete next session using a familiar mode at a previously comfortable workload, within the user's existing exercise plan. Do not prescribe new exercises, faster targets or increases in workload from these readings. Provide one practical tip for making the next comparison useful.
Return a JSON object containing three short, plain-text English strings. Example format: {"observations":"...","next_session":"...","tip":"..."}. Do not use Markdown.`;

async function askDeepSeek(key, dataset) {
  const signal = AbortSignal.timeout(30000);
  let response;
  let content;
  try {
    response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: {Authorization: `Bearer ${key}`, 'Content-Type': 'application/json'},
      body: JSON.stringify({
        model: MODEL,
        thinking: {type: 'disabled'},
        response_format: {type: 'json_object'},
        max_tokens: 650,
        messages: [
          {role: 'system', content: systemPrompt},
          {role: 'user', content: JSON.stringify(dataset)}
        ]
      }),
      signal,
      redirect: 'manual'
    });
    if (response.ok) {
      const payload = await response.json();
      const choice = payload.choices?.[0];
      if (choice?.finish_reason === 'stop') content = JSON.parse(choice.message.content);
    }
  } catch {
    if (signal.aborted) throw new HttpError(504, 'DeepSeek took too long. Try again.');
    throw new HttpError(502, 'Unable to read a review from DeepSeek. Try again.');
  }
  if (response.status === 402) throw new HttpError(503, 'DeepSeek reports insufficient API credit. Contact the site operator.');
  if ([401, 403].includes(response.status)) throw new HttpError(503, 'DeepSeek rejected API access. Contact the site operator.');
  if (response.status === 429) throw new HttpError(429, 'DeepSeek is busy. Try again in a minute.');
  if (!response.ok) throw new HttpError(502, 'DeepSeek is temporarily unavailable. Try again.');
  const review = {};
  for (const field of ['observations', 'next_session', 'tip']) {
    const value = content?.[field];
    if (typeof value !== 'string' || !value.trim() || value.length > 2400) {
      throw new HttpError(502, 'DeepSeek returned an incomplete review. Try again.');
    }
    review[field] = value.trim();
  }
  return review;
}

export async function analyzeTraining(env, accountId, bounds, ip) {
  if (!env['ai-analysis']) throw new HttpError(503, 'The site operator has not activated DeepSeek analysis yet.');
  const {results} = await env.DB.prepare(dailySql).bind(accountId, ...bounds, MAX_DAYS - 1).all();
  if (!results.length) throw new HttpError(422, 'No saved sessions in this date range.');
  const keys = ['account:' + accountId];
  if (ip) keys.push('ip:' + ip);
  const limits = await Promise.all(keys.map(key => env.ANALYSIS_LIMIT.limit({key})));
  if (limits.some(result => !result.success)) throw new HttpError(429, 'Too many reviews. Try again in a minute.');

  const totalDays = results.reduce((sum, row) => sum + row.training_days, 0);
  const days = results.filter(row => row.period.length === 10).map(({period, through, training_days, ...day}) => day);
  const months = results.filter(row => row.period.length === 7);
  const review = await askDeepSeek(env['ai-analysis'], {
    training_days_in_range: totalDays,
    middle_monthly_totals: months,
    daily_totals: days
  });
  return {first: days[0], last: days.at(-1), total_days: totalDays, included_days: days.length, review};
}
