// The Worker re-reads training data from the authenticated account for each review.
export function createTrainingReview(formatDuration, onUnauthorized) {
  const byId = id => document.getElementById(id);
  const panel = byId('analysisPanel');
  const button = byId('analyzeButton');
  const status = byId('analysisStatus');
  const result = byId('analysisResult');
  let context;
  let scope;
  let pending;
  let message = '';

  function render() {
    panel.hidden = !context.hosted;
    if (panel.hidden) return;
    let hint = message;
    if (!context.online) hint = 'Reconnect to the server to analyze your training.';
    else if (!context.configured) hint = 'DeepSeek analysis is awaiting site activation.';
    else if (context.from && context.to && context.from > context.to) hint = 'Choose an end date on or after the start date.';
    else if (!context.hasRecords) hint = 'Save a session or select dates with training to get a review.';
    if (status.textContent !== hint) status.textContent = hint;
    button.disabled = Boolean(pending) || !context.online || !context.configured || !context.hasRecords ||
      Boolean(context.from && context.to && context.from > context.to);
    button.setAttribute('aria-busy', String(Boolean(pending)));
  }

  function drawReview({first, last, total_days, included_days, review}) {
    const body = byId('analysisMetrics');
    body.replaceChildren();
    function row(label, passages, duration, pace) {
      const tr = body.insertRow();
      const heading = document.createElement('th');
      heading.scope = 'row';
      heading.textContent = label;
      tr.append(heading);
      for (const value of [passages, duration, pace]) tr.insertCell().textContent = value;
    }
    const pace = n => n === null ? '—' : n.toFixed(1);
    const signed = (n, format) => (n < 0 ? '−' : n > 0 ? '+' : '') + format(Math.abs(n));
    row(first.day, first.passages, formatDuration(first.seconds), pace(first.passages_per_minute));
    if (total_days > 1) {
      row(last.day, last.passages, formatDuration(last.seconds), pace(last.passages_per_minute));
      const paceChange = first.passages_per_minute === null || last.passages_per_minute === null
        ? '—' : signed(last.passages_per_minute - first.passages_per_minute, pace);
      row('Change', signed(last.passages - first.passages, String),
        signed(last.seconds - first.seconds, formatDuration), paceChange);
    }
    byId('analysisObservations').textContent = review.observations;
    byId('analysisPlan').textContent = review.next_session;
    byId('analysisTip').textContent = review.tip;
    message = total_days === 1 ? 'One training day available. This session history establishes a baseline.'
      : included_days < total_days ? `Review covers all ${total_days} training days. The first and latest ${included_days - 1} are shown to DeepSeek by day, and the rest are grouped by month.`
      : `Review covers ${total_days} training days.`;
    result.hidden = false;
  }

  button.addEventListener('click', async () => {
    if (button.disabled) return;
    const controller = new AbortController();
    pending = controller;
    message = 'DeepSeek is reviewing your training days…';
    result.hidden = true;
    render();
    const timeout = setTimeout(() => controller.abort(), 40000);
    try {
      const query = new URLSearchParams();
      if (context.from) query.set('from', context.from);
      if (context.to) query.set('to', context.to);
      const response = await fetch('/api/progress/analysis?' + query, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Account-Id': String(context.accountId)},
        body: '{}', signal: controller.signal, cache: 'no-store'
      });
      if (pending !== controller) return;
      if (response.status === 401) { onUnauthorized(); return; }
      const data = await response.json();
      if (pending !== controller) return;
      if (!response.ok) { message = data.message || 'Unable to generate a review. Try again.'; return; }
      drawReview(data);
    } catch {
      if (pending === controller) message = 'The review could not be completed. Check your connection and try again.';
    } finally {
      clearTimeout(timeout);
      if (pending === controller) { pending = null; render(); }
    }
  });

  return {
    update(next) {
      const nextScope = JSON.stringify([next.accountId, next.from, next.to, next.revision, next.configured]);
      if (scope !== nextScope) {
        pending?.abort();
        pending = null;
        scope = nextScope;
        message = '';
        result.hidden = true;
      }
      context = next;
      render();
    }
  };
}
