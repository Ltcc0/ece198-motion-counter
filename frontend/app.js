'use strict';

// This interface only displays the local server's real device data.
const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  'connectionPill','connectionLabel','setupButton','setupDialog','closeSetupButton',
  'presentationButton','presentationLabel','sessionEyebrow','sessionId','countValue',
  'durationValue','distanceValue','distanceUnit','startButton','stopButton','actionFeedback',
  'stateIndicator','stateTitle','recordsButton','recordsDialog','closeRecordsButton',
  'refreshHistoryButton','exportLink',
  'historyBody','historyEmpty','historyNote','portSelect','refreshPortsButton','connectButton',
  'disconnectButton','setupFeedback','deviceName','firmwareValue','modeValue','lastResponse',
  'trainingTitle','accountEmail','logoutButton','dateFrom','dateTo','clearDates','recordRange',
  'trainingMode','targetField','targetLabel','trainingTarget','goalProgress','modeHint',
  'trendProtocol','trendTable','trendBody','trendSummary','trendNote','goalsHeader'
].map((id) => [id, $(id)]));

let leavingDashboard = false;
function showLogin() {
  if (leavingDashboard) return;
  leavingDashboard = true;
  location.replace('/login.html');
}

let state = null;
let usb = null;
let cloudState = null;
let nextCloudCheck = 0;
let apiAvailable = false;
let fetchInFlight = null;
let mutationInFlight = false;
let awaitingCommand = null;
let feedback = null;
let setupFeedback = null;
let priorCount = null;
let historySignature = '';
let portSignature = '';
let currentPresentation = false;
let countFlashTimer = null;

function number(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function word(value) { return String(value ?? '').toUpperCase(); }
function isRunning(device) { return ['RUNNING','ACTIVE'].includes(word(device?.state)); }
function secondsText(value) {
  const raw = number(value);
  if (raw === null) return '—';
  const total = Math.max(0, Math.floor(raw));
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function savedText(value) {
  if (value === null || value === undefined || value === '') return 'Not recorded';
  const n = number(value);
  const date = new Date(n === null ? value : n * 1000);
  if (Number.isNaN(date.getTime())) return 'Not recorded';
  return date.toLocaleString('en-CA', {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}
function connectionInfo() {
  const c = state?.connection || {};
  const d = state?.device || {};
  const received = number(c.last_received_at);
  const connected = Boolean(c.connected) && apiAvailable;
  const stale = connected && (Boolean(c.stale) || (received !== null && Date.now()/1000 - received > 6));
  const fresh = connected && !stale && received !== null && ['IDLE','RUNNING'].includes(word(d.state));
  return {c,d,connected,stale,fresh,received};
}
function busy() { return mutationInFlight || Boolean(state?.pending_command) || Boolean(awaitingCommand); }
function flashMessage(text, kind='info', setup=false, duration=6500) {
  const item = {text,kind,until:Date.now()+duration};
  if (setup) setupFeedback = item; else feedback = item;
}
function applyMessage(el, text, kind='info') {
  el.textContent = text;
  el.dataset.kind = kind;
}
function currentMessage(item) { return item && item.until > Date.now() ? item : null; }

function render() {
  const {c,d,connected,stale,fresh,received} = connectionInfo();
  const running = isRunning(d);
  const pending = busy();
  const knownMode = word(d.mode);
  const supportsPassage = knownMode === 'PASSAGE';
  let connectionText = 'Disconnected', connectionType = 'disconnected';
  if (!apiAvailable) { connectionText = 'Server unavailable'; connectionType = 'error'; }
  else if (connected && stale) { connectionText = 'Connection interrupted'; connectionType = 'stale'; }
  else if (connected && !fresh) { connectionText = 'Connecting…'; }
  else if (connected) { connectionText = 'Device connected'; connectionType = 'connected'; }
  else if (mutationInFlight) { connectionText = 'Connecting…'; }
  ui.connectionLabel.textContent = connectionText;
  ui.connectionPill.dataset.status = connectionType;

  const count = number(d.count);
  ui.countValue.textContent = count === null ? '—' : Math.max(0,Math.floor(count)).toLocaleString('en-US');
  ui.countValue.setAttribute('aria-label', count === null ? 'No movement count available' : `${Math.max(0,Math.floor(count))} movements counted`);
  if (priorCount !== null && count !== null && count > priorCount && running && fresh) {
    ui.countValue.classList.add('count-bump');
    clearTimeout(countFlashTimer);
    countFlashTimer = setTimeout(() => ui.countValue.classList.remove('count-bump'), 350);
  }
  priorCount = count;
  ui.durationValue.textContent = secondsText(d.seconds);
  const distance = number(d.distance_cm);
  const validDistance = distance !== null && distance > 0;
  const sensor = word(d.sensor);
  ui.distanceValue.textContent = fresh && validDistance ? distance.toFixed(1) : fresh && /NO.?ECHO|NO.?TARGET|CLEAR|TOO_FAR/.test(sensor) ? 'Clear' : fresh && sensor === 'TOO_CLOSE' ? 'Too close' : '—';
  ui.distanceUnit.hidden = !fresh || !validDistance;
  ui.sessionEyebrow.textContent = running && fresh ? 'LIVE SESSION' : count !== null && !running ? 'SESSION SUMMARY' : 'LIVE SESSION';
  const id = d.session_id;
  ui.sessionId.textContent = id !== null && id !== undefined ? `Session ${String(id).padStart(3,'0')}` : running ? 'Session in progress' : 'Ready when you are';

  let view = {state:'disconnected',title:'Waiting for connection'};
  if (!apiAvailable) view = {state:'disconnected',title:'Local server unavailable'};
  else if (connected && stale) view = {state:'stale',title:'Waiting for a fresh reading'};
  else if (connected && !fresh) view = {state:'disconnected',title:'Waiting for the Arduino'};
  else if (fresh && knownMode && !supportsPassage) view = {state:'stopped',title:'Different counting mode — passage firmware required'};
  else if (fresh && !running) view = {state:'stopped',title:count !== null && Number(count)>0?'Session stopped':'Ready to start'};
  else if (fresh && running) {
    const phase = word(d.phase);
    if (phase === 'ARMED' || phase === 'READY') view = {state:'ready',title:'Ready for your next pass'};
    else if (phase === 'OCCUPIED') view = {state:'occupied',title:'Pass counted · move fully away'};
    else view = {state:'waitclear',title:'Clear the path to begin'};
  }
  ui.stateIndicator.dataset.state = view.state;
  ui.stateTitle.textContent = view.title;

  renderTraining(running);
  const validTarget = ui.trainingMode.value === 'free' || ui.trainingTarget.validity.valid;
  ui.startButton.disabled = !fresh || pending || running || !supportsPassage || !state?.account || !validTarget;
  ui.stopButton.disabled = !fresh || pending || !running;
  ui.refreshHistoryButton.disabled = !fresh || pending || running;
  ui.refreshPortsButton.disabled = mutationInFlight;
  ui.connectButton.disabled = !apiAvailable || pending || connected || (usb ? !navigator.serial : !ui.portSelect.value);
  if (usb) {
    ui.refreshHistoryButton.disabled = !apiAvailable;
    ui.portSelect.closest('.port-row').hidden = true;
    document.querySelector('label[for="portSelect"]').hidden = true;
    ui.connectButton.textContent = 'Choose USB device';
    document.querySelector('.drawer-intro').textContent = 'Connect the Arduino to this computer, then choose its USB port in the browser.';
  }
  ui.disconnectButton.disabled = !apiAvailable || mutationInFlight || !c.connected;
  ui.portSelect.disabled = Boolean(c.connected) || mutationInFlight;
  ui.startButton.setAttribute('aria-busy', String(awaitingCommand?.command === 'START'));
  ui.stopButton.setAttribute('aria-busy', String(awaitingCommand?.command === 'STOP'));
  const msg = currentMessage(feedback);
  if (state?.device_busy) {
    for (const control of [ui.startButton, ui.stopButton, ui.connectButton, ui.disconnectButton, ui.refreshHistoryButton]) control.disabled = true;
    ui.stateTitle.textContent = 'Device in use';
    applyMessage(ui.actionFeedback, 'Your training history is available while the device is in use.');
  }
  else if (state?.storage_error) applyMessage(ui.actionFeedback, state.storage_error, 'error');
  else if (pending) applyMessage(ui.actionFeedback, awaitingCommand?.command === 'STOP' ? 'Waiting for the Arduino to stop and confirm the save…' : awaitingCommand?.command === 'START' ? 'Waiting for the Arduino to start…' : 'Waiting for the device response…');
  else if (msg) applyMessage(ui.actionFeedback,msg.text,msg.kind);
  else if (!apiAvailable) applyMessage(ui.actionFeedback,'The local server is unavailable. Live controls are paused.','error');
  else if (!connected) applyMessage(ui.actionFeedback,'Connect your Arduino to begin.');
  else if (!fresh) applyMessage(ui.actionFeedback,'Waiting for a fresh device reading.');
  else if (!supportsPassage) applyMessage(ui.actionFeedback,'Waiting for passage-mode firmware identification.');
  else if (!running && !validTarget) applyMessage(ui.actionFeedback,'Enter a positive whole-number target.');
  else if (running) applyMessage(ui.actionFeedback,state.active_training?.target ? 'Choose Stop & save to finish early.' : 'Choose Stop & save when your session is finished.');
  else applyMessage(ui.actionFeedback,'Clear the path, then start a session directly from this page.');

  ui.deviceName.textContent = connected ? String(c.device || 'Arduino UNO R4 Minima') : 'Not connected';
  ui.firmwareValue.textContent = d.firmware === null || d.firmware === undefined ? '—' : `Version ${d.firmware}`;
  ui.modeValue.textContent = knownMode === 'PASSAGE' ? 'Passage counting' : knownMode ? String(d.mode) : 'Not identified';
  ui.lastResponse.textContent = received === null ? '—' : `${Math.max(0,Math.floor(Date.now()/1000-received))} seconds ago`;
  const setupMsg = currentMessage(setupFeedback);
  if (setupMsg) applyMessage(ui.setupFeedback,setupMsg.text,setupMsg.kind);
  else if (!apiAvailable) applyMessage(ui.setupFeedback,'Cannot reach the local server. Check that it is running.','error');
  else if (c.error) applyMessage(ui.setupFeedback,String(c.error),'error');
  else if (connected) applyMessage(ui.setupFeedback,stale?'The connection is open, but readings are not arriving.':fresh?'Your Arduino is connected and sending readings.':'Port opened. Waiting for the Arduino to respond.');
  else if (usb) applyMessage(ui.setupFeedback, navigator.serial ? 'Choose your Arduino and allow USB access in the browser.' : 'Use desktop Chrome or Edge to connect USB. Your training records are available here.');
  else if (!(state?.ports || []).length) applyMessage(ui.setupFeedback,'No ports found. Connect the Arduino, then refresh the list.');
  else applyMessage(ui.setupFeedback,'Select your Arduino’s USB port and choose Connect device.');

  renderHistory();
}

function renderPorts() {
  const ports = Array.isArray(state?.ports) ? state.ports : [];
  const active = state?.connection?.port || '';
  const signature = JSON.stringify([ports,active]);
  if (signature === portSignature) return;
  const previous = ui.portSelect.value;
  portSignature = signature;
  ui.portSelect.replaceChildren();
  if (!ports.length) {
    const option = document.createElement('option'); option.value=''; option.textContent='No Arduino ports found'; ui.portSelect.append(option);
  } else {
    const prompt = document.createElement('option'); prompt.value=''; prompt.textContent='Select the Arduino USB port'; ui.portSelect.append(prompt);
    ports.forEach((port) => {
      const option = document.createElement('option');
      const path = typeof port === 'string' ? port : String(port.path || '');
      option.value = path;
      option.textContent = typeof port === 'string' ? port : port.label && port.label !== path ? `${port.label} · ${path}` : path;
      ui.portSelect.append(option);
    });
    const choices = [...ui.portSelect.options].map((o)=>o.value);
    if (active && choices.includes(active)) ui.portSelect.value=active;
    else if (previous && choices.includes(previous)) ui.portSelect.value=previous;
    else {
      const likely = choices.find((p)=>/usbmodem|usbserial|wchusb|cu\.SLAB/i.test(p));
      ui.portSelect.value=likely || '';
    }
  }
}
function modeText(item) {
  const mode = word(item.mode);
  if (mode === 'PASSAGE') return 'Passage count';
  if (!mode || mode === 'UNKNOWN') return 'Mode not recorded';
  if (/CYCLE|REP/.test(mode)) return 'Cycle count';
  return String(item.mode);
}
function sourceText(item) {
  const source = word(item.source);
  if (/EEPROM|BOARD|DEVICE/.test(source)) return 'Board history';
  if (/LOCAL|LIVE|HOST/.test(source)) return 'Saved on this Mac';
  return item.source ? String(item.source) : 'Source not recorded';
}
const trainingLabels = {free: 'Free practice', count: 'Count goal', timed: 'Timed practice'};
const targets = {count: '10', timed: '60'};

let recordData = {history: [], reports: []};
let recordRequest = 0;
let protocolKey = '';


function protocolText(item) {
  if (!item.training_mode) return 'Not recorded';
  const label = trainingLabels[item.training_mode];
  if (item.target == null) return label;
  return label + ' · ' + item.target + (item.training_mode === 'count' ? ' passages' : ' seconds');
}

function renderTraining(running) {
  ui.accountEmail.textContent = state?.account?.email || '';
  const plan = state?.active_training;
  if (plan) {
    ui.trainingMode.value = plan.training_mode;
    if (plan.target != null) ui.trainingTarget.value = plan.target;
  }
  const locked = running || Boolean(plan) || busy();
  ui.trainingTitle.textContent = plan ? 'Active session' : 'Next session';
  for (const control of [ui.trainingMode, ui.trainingTarget]) {
    control.disabled = locked || !apiAvailable;
  }
  const mode = ui.trainingMode.value;
  const timed = mode === 'timed';
  ui.targetField.hidden = mode === 'free';
  ui.targetLabel.textContent = timed ? 'Time limit (seconds)' : 'Target passages';
  ui.goalProgress.hidden = !plan || mode === 'free';
  if (!ui.goalProgress.hidden) {
    const value = (timed ? state.device.seconds : state.device.count) ?? 0;
    ui.goalProgress.max = plan.target;
    ui.goalProgress.value = value;
    ui.modeHint.textContent = timed
      ? secondsText(Math.max(0, plan.target - value)) + ' remaining'
      : value + ' of ' + plan.target + ' passages';
  } else {
    ui.modeHint.textContent = mode === 'free' ? 'Choose Stop & save whenever you finish.'
      : 'Stop & save is sent when the board reaches your ' + (timed ? 'time limit.' : 'passage target.');
  }
}

function cell(tr, value, className) {
  const td = tr.insertCell();
  if (className) td.className = className;
  td.textContent = value;
  return td;
}

function renderTrend() {
  const reports = recordData.reports;
  ui.trendProtocol.hidden = reports.length === 0;
  const report = reports.find(r => protocolText(r) === ui.trendProtocol.value);
  ui.trendTable.hidden = ui.trendNote.hidden = !report;
  ui.trendBody.replaceChildren();
  if (!report) {
    ui.trendSummary.textContent = ui.dateFrom.value || ui.dateTo.value
      ? 'No training sessions in this date range.'
      : 'Your first saved training session will appear here.';
    return;
  }
  const latest = report.days[report.days.length - 1];
  if (report.delta === null) {
    ui.trendSummary.textContent = latest.pace === null
      ? 'Pace is unavailable for the latest day because its recorded duration is zero.'
      : latest.day + ' averaged ' + latest.pace.toFixed(1) + ' passages/min. A second training day enables comparison.';
  } else {
    const delta = report.delta;
    const change = (delta > 0 ? '+' : '') + delta.toFixed(1);
    const baseline = report.days.find(day => day.day === report.baseline_day);
    ui.trendSummary.textContent = baseline.day + ' · ' + baseline.pace.toFixed(1) + ' passages/min → ' +
      latest.day + ' · ' + latest.pace.toFixed(1) + ' passages/min (' + change + ' passages/min).';
  }
  const hasGoal = report.target !== null;
  ui.goalsHeader.hidden = !hasGoal;
  const peak = report.days.reduce((max, day) => Math.max(max, day.pace || 0), 0);
  for (const day of [...report.days].reverse()) {
    const tr = ui.trendBody.insertRow();
    cell(tr, day.day); cell(tr, day.sessions); cell(tr, day.count); cell(tr, secondsText(day.seconds));
    if (hasGoal) cell(tr, day.goals_met + '/' + day.sessions);
    const pace = cell(tr, day.pace === null ? '—' : day.pace.toFixed(1));
    if (day.pace !== null) {
      const track = document.createElement('span');
      track.className = 'pace-track';
      track.setAttribute('aria-hidden', 'true');
      const bar = document.createElement('span');
      bar.className = 'pace-bar';
      bar.style.width = (peak ? day.pace / peak * 100 : 0) + '%';
      track.append(bar);
      pace.prepend(track);
    }
  }
}

function displayRecords() {
  const rows = recordData.history;
  ui.exportLink.href = '/api/export.csv?' + recordQuery();
  const from = ui.dateFrom.value;
  const to = ui.dateTo.value;
  ui.recordRange.textContent = [from && 'From ' + from, to && 'Through ' + to].filter(Boolean).join(' · ') || 'All training dates';
  ui.exportLink.setAttribute('aria-disabled', String(!rows.length || !apiAvailable));
  ui.exportLink.tabIndex = rows.length && apiAvailable ? 0 : -1;
  ui.historyBody.replaceChildren();
  ui.historyEmpty.hidden = rows.length > 0;
  for (const row of rows) {
    const tr = ui.historyBody.insertRow();
    cell(tr, '#' + String(row.id).padStart(3, '0'), 'session-cell');
    cell(tr, row.training_day || 'Not recorded');
    cell(tr, protocolText(row)); cell(tr, row.count, 'count-cell'); cell(tr, secondsText(row.seconds), 'mono');
    const type = cell(tr, modeText(row));
    const source = document.createElement('span');
    source.className = 'record-source'; source.textContent = sourceText(row); type.append(source);
    cell(tr, savedText(row.saved_at));
  }
  ui.trendProtocol.replaceChildren(...recordData.reports.map(r => new Option(protocolText(r), protocolText(r))));
  if (recordData.reports.some(r => protocolText(r) === protocolKey)) ui.trendProtocol.value = protocolKey;
  renderTrend();
}

function recordQuery() {
  const query = new URLSearchParams({user: state.account.id});
  if (ui.dateFrom.value) query.set('from', ui.dateFrom.value);
  if (ui.dateTo.value) query.set('to', ui.dateTo.value);
  return query;
}

async function renderHistory() {
  if (!state?.account || !apiAvailable) return;
  ui.historyNote.textContent = state.storage_error || 'Daily pace describes observed training performance. Sensor placement and the exercise performed affect comparisons.';
  ui.historyNote.dataset.kind = state.storage_error ? 'error' : 'info';
  const query = recordQuery().toString();
  ui.clearDates.hidden = !ui.dateFrom.value && !ui.dateTo.value;
  const signature = JSON.stringify([state.account.id, query, state.archive_revision]);
  if (signature === historySignature) return;
  historySignature = signature;
  const request = ++recordRequest;
  recordData = {history: [], reports: []};
  displayRecords();
  ui.historyEmpty.hidden = true;
  if (ui.dateFrom.value && ui.dateTo.value && ui.dateFrom.value > ui.dateTo.value) {
    ui.trendSummary.textContent = 'Choose an end date on or after the start date.';
    return;
  }
  ui.trendSummary.textContent = 'Loading training records…';
  try {
    const response = await fetch('/api/records?' + query, {cache: 'no-store'});
    if (response.status === 401) { showLogin(); return; }
    if (!response.ok) throw new Error('Cannot load training records.');
    const data = await response.json();
    if (request !== recordRequest) return;
    recordData = data;
    displayRecords();
  } catch (error) {
    if (request !== recordRequest) return;
    ui.trendSummary.textContent = 'Cannot load records. Reopen Session records to retry.';
  }
}

function settleCommand() {
  if (!awaitingCommand) return;
  const {c,d}=connectionInfo();
  const cmd=awaitingCommand.command;
  const recentEvents=(Array.isArray(state?.events)?state.events:[]).filter((event)=>(number(event.at)??0)>=awaitingCommand.started/1000);
  const matchesSession=(event)=>{
    if(awaitingCommand.sessionId===null||awaitingCommand.sessionId===undefined)return true;
    const match=String(event.text).match(/\bid=(\d+)/);
    return Boolean(match)&&Number(match[1])===Number(awaitingCommand.sessionId);
  };
  const newResponse=(number(c.last_received_at)??0)>awaitingCommand.received;
  const started=recentEvents.find((event)=>/^STARTED\s/.test(String(event.text)));
  const saved=recentEvents.find((event)=>/^SAVED\s/.test(String(event.text))&&matchesSession(event));
  const saveFailed=recentEvents.find((event)=>/^SAVE_FAILED\s/.test(String(event.text))&&matchesSession(event));
  const logRead=recentEvents.find((event)=>/^Read \d+ saved board records\.$/.test(String(event.text)));
  const failure=recentEvents.find((event)=>event.kind==='error'&&/No confirmation|timed? out|Unknown command|Command too long|STOP before|Session ID limit/i.test(String(event.text)));
  if(cmd==='START'&&(started||(newResponse&&isRunning(d)&&!state?.pending_command))){
    awaitingCommand=null;flashMessage('Session started. Clear the path, then make your first pass.','success');
  }else if(cmd==='STOP'&&saved){
    awaitingCommand=null;
    if(state?.storage_error)flashMessage('Saved on the Arduino. The local copy could not be saved; open Session records for details.','error',false,10000);
    else flashMessage('Session saved. Open Session records to view your result.','success');
  }else if(cmd==='STOP'&&saveFailed){
    awaitingCommand=null;flashMessage('Session stopped, but the Arduino reported a save failure. The record is not confirmed.','error',false,10000);
  }else if(cmd==='LOG'&&logRead){
    awaitingCommand=null;flashMessage('Session records refreshed from the Arduino.','success');
  }else if(failure){
    awaitingCommand=null;flashMessage(String(failure.text),'error',false,10000);
  }else if(Date.now()-awaitingCommand.started>12000){
    awaitingCommand=null;flashMessage('No confirmation received from the Arduino. Check the connection before trying again.','error',false,10000);
  }
}
async function requestState() {
  if(fetchInFlight)return fetchInFlight;
  fetchInFlight=(async()=>{
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),4000);
    try{
      let next = cloudState;
      if (!usb || !cloudState || Date.now() >= nextCloudCheck) {
        const response=await fetch('/api/state?live=1',{cache:'no-store',signal:controller.signal});
        if(response.status===401){showLogin();return;}
        if(!response.ok)throw new Error(`Server returned ${response.status}`);
        next=await response.json();
        if (next.transport === 'web-serial') {
          if (!usb) {
            const {UsbTraining} = await import('/usb-training.js');
            usb = new UsbTraining(next.account, () => {nextCloudCheck=0;historySignature='';});
          }
          cloudState = next;
          nextCloudCheck = Date.now() + 30000;
        }
      }
      if (usb) next = {...next,...usb.snapshot()};
      if(!next||typeof next!=='object'||!next.connection)throw new Error('Invalid server response');
      if (state?.account && state.account.id !== next.account.id) { location.reload(); return; }
      const previousPlan = state?.active_training;
      if (previousPlan && !next.active_training && !awaitingCommand) {
        const result = [...next.events].reverse().find(event =>
          event.at >= previousPlan.started_at &&
          new RegExp('^(SAVED|SAVE_FAILED) id=' + previousPlan.id + '(?: |$)').test(event.text));
        if (result) {
          if (result.text.startsWith('SAVE_FAILED')) flashMessage('The Arduino could not save this session.', 'error');
          else flashMessage('Session saved. Open Session records to review it.', 'success');
        }
      }
      state=next;apiAvailable=true;renderPorts();settleCommand();render();
    }catch(error){apiAvailable=false;render();}
    finally{clearTimeout(timeout);}
  })().finally(() => {fetchInFlight=null;});
  return fetchInFlight;
}
async function post(path,body={}) {
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),10000);
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Account-Id':String(state?.account?.id ?? '')},body:JSON.stringify(body),signal:controller.signal});
    const result=await response.json().catch(()=>({ok:false,message:'The server returned an unreadable response.'}));
    if(!response.ok||result.ok===false)throw new Error(result.message||`Request failed (${response.status})`);
    return result;
  }finally{clearTimeout(timeout);}
}
async function sendCommand(command) {
  if(busy())return;
  mutationInFlight=true;awaitingCommand={command,started:Date.now(),received:number(state?.connection?.last_received_at)??0,sessionId:state?.device?.session_id??null};render();
  const body = {command};
  if (command === 'START') {
    body.training = {training_mode: ui.trainingMode.value,
      target: ui.trainingMode.value === 'free' ? null : ui.trainingTarget.valueAsNumber};
  }
  try{if (usb) await usb.command(command, body.training); else await post('/api/command',body);}
  catch(error){awaitingCommand=null;flashMessage(error.name==='AbortError'?'The device request timed out. Check the connection.':error.message,'error',false,10000);}
  finally{mutationInFlight=false;await requestState();render();}
}
ui.trainingMode.addEventListener('change', () => {
  if (ui.trainingMode.value !== 'free') ui.trainingTarget.value = targets[ui.trainingMode.value];
  render();
});
ui.trainingTarget.addEventListener('input', () => {
  targets[ui.trainingMode.value] = ui.trainingTarget.value;
  render();
});
ui.logoutButton.addEventListener('click', async () => {
  ui.logoutButton.disabled = true;
  try {
    await post('/api/logout');
    showLogin();
  } catch (error) {
    flashMessage(error.message, 'error');
    render();
    ui.logoutButton.disabled = false;
  }
});
for (const input of [ui.dateFrom, ui.dateTo]) input.addEventListener('change', renderHistory);
ui.clearDates.addEventListener('click', () => {
  ui.dateFrom.value = ui.dateTo.value = '';
  renderHistory();
});
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
ui.trendProtocol.addEventListener('change', () => { protocolKey = ui.trendProtocol.value; renderTrend(); });

ui.startButton.addEventListener('click',()=>sendCommand('START'));
ui.stopButton.addEventListener('click',()=>sendCommand('STOP'));
ui.refreshHistoryButton.addEventListener('click',()=>{
  if (!usb) return sendCommand('LOG');
  nextCloudCheck=0; historySignature=''; requestState();
});
ui.exportLink.addEventListener('click',(event)=>{if(ui.exportLink.getAttribute('aria-disabled')==='true')event.preventDefault();});
ui.setupButton.addEventListener('click',()=>{ui.setupDialog.showModal();requestState();});
ui.closeSetupButton.addEventListener('click',()=>ui.setupDialog.close());
ui.recordsButton.addEventListener('click',()=>{
  historySignature = '';
  ui.recordsDialog.showModal();
  renderHistory();
  requestState();
});
ui.closeRecordsButton.addEventListener('click',()=>ui.recordsDialog.close());
ui.recordsDialog.addEventListener('click',(event)=>{
  if(event.target!==ui.recordsDialog)return;
  const rect=ui.recordsDialog.getBoundingClientRect();
  if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)ui.recordsDialog.close();
});
ui.setupDialog.addEventListener('click',(event)=>{
  if(event.target!==ui.setupDialog)return;
  const rect=ui.setupDialog.getBoundingClientRect();
  if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)ui.setupDialog.close();
});
ui.portSelect.addEventListener('change',render);
ui.refreshPortsButton.addEventListener('click',async()=>{await requestState();flashMessage('Port list refreshed.','info',true);render();});
ui.connectButton.addEventListener('click',async()=>{
  const port=ui.portSelect.value;if((!usb && !port)||mutationInFlight)return;
  mutationInFlight=true;setupFeedback=null;render();
  try{if (usb) await usb.connect(); else await post('/api/connect',{port});flashMessage('Port opened. Waiting for the Arduino to identify itself.','info',true);}
  catch(error){flashMessage(error.name==='AbortError'?'Connection timed out. Check the cable and port.':error.message,'error',true,12000);}
  finally{mutationInFlight=false;await requestState();render();}
});
ui.disconnectButton.addEventListener('click',async()=>{
  if(mutationInFlight)return;mutationInFlight=true;render();
  try{if (usb) await usb.disconnect(); else await post('/api/disconnect');awaitingCommand=null;flashMessage('Device disconnected.','info',true);}
  catch(error){flashMessage(error.message,'error',true);}
  finally{mutationInFlight=false;await requestState();render();}
});
ui.presentationButton.addEventListener('click',()=>{
  currentPresentation=!currentPresentation;
  document.body.classList.toggle('presentation-mode',currentPresentation);
  ui.presentationButton.setAttribute('aria-pressed',String(currentPresentation));
  ui.presentationLabel.textContent=currentPresentation?'Exit presentation':'Presentation mode';
  // Fullscreen is requested only within this explicit user gesture.
  if(currentPresentation&&document.documentElement.requestFullscreen){document.documentElement.requestFullscreen().catch(()=>{});}
  else if(!currentPresentation&&document.fullscreenElement&&document.exitFullscreen){document.exitFullscreen().catch(()=>{});}
});
async function poll(){await requestState();if (!leavingDashboard) setTimeout(poll,450);}
poll();
