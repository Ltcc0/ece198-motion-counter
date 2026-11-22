// USB is opened only by a user gesture in the browser that owns the device.
const blankDevice = () => ({firmware:null, mode:null, state:'UNKNOWN', count:null, seconds:null,
  session_id:null, distance_cm:null, phase:null, sensor:null});
const numberField = value => {
  if (!/^\d{1,10}$/.test(value ?? '')) return null;
  const n = Number(value);
  return n <= 4294967295 ? n : null;
};

export class UsbTraining {
  constructor(account) {
    this.account = account;
    this.completion = null;
    this.savedRevision = 0;
    this.device = blankDevice();
    this.port = null;
    this.reader = null;
    this.writer = null;
    this.readTask = null;
    this.plan = null;
    this.pending = null;
    this.last = null;
    this.events = [];
    this.error = null;
    this.storageError = null;
    this.nextRetry = 0;
    this.sending = false;
    this.backlog = new Map();
    this.prefix = 'steps.unsynced.' + account.id + '.';
    for (let i=0; i<localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(this.prefix)) {
        try { this.backlog.set(key, JSON.parse(localStorage.getItem(key))); } catch { /* Incomplete entry. */ }
      }
    }
    const latest = [...this.backlog.values()].sort((a, b) => b.saved_at - a.saved_at)[0];
    if (latest) this.completion = {status:'syncing', key:latest.capture_key, id:latest.id};
  }

  event(text, kind='info') {
    this.events.push({text,kind,at:Date.now()/1000});
    if (this.events.length > 100) this.events.shift();
  }

  snapshot() {
    const t = Date.now()/1000;
    if (this.pending && t - this.pending.at > 5) {
      this.event('No confirmation for ' + this.pending.command + '. Check the board.', 'error');
      this.pending = null;
    }
    this.sync();
    return {device:{...this.device}, connection:{connected:!!this.writer, port:this.writer?'USB':null,
      device:this.writer?'USB serial device':null, last_received_at:this.last,
      stale:this.last === null || t-this.last>3, error:this.error},
      active_training:this.plan && {...this.plan}, pending_command:this.pending?.command,
      events:[...this.events], ports:[], storage_error:this.storageError, completion:this.completion, sync_pending:this.backlog.size > 0};
  }

  async connect() {
    if (!navigator.serial) throw new Error('USB training requires desktop Chrome or Edge.');
    const port = await navigator.serial.requestPort();
    await port.open({baudRate:115200});
    this.port = port;
    this.device = blankDevice();
    this.error = null;
    this.last = null;
    try {
      this.writer = port.writable.getWriter();
      this.reader = port.readable.getReader();
      this.readTask = this.read(this.reader);
      await this.write('HELP\nSTATUS');
    } catch (error) {
      await this.disconnect();
      throw error;
    }
  }

  async read(reader) {
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const {value,done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value,{stream:true});
        let nl;
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0,nl).trim(); buffer = buffer.slice(nl+1);
          if (line.length <= 2048) this.line(line);
        }
        if (buffer.length > 4096) buffer = '';
      }
    } catch { this.error = 'USB connection interrupted. Reconnect your board.'; }
    finally {
      reader.releaseLock();
      this.reader = null;
      if (this.writer) this.writer.releaseLock();
      this.writer = null;
      const port = this.port;
      this.port = this.plan = this.pending = null;
      if (port) await port.close().catch(()=>{});
    }
  }

  async disconnect() {
    if (this.reader) await this.reader.cancel().catch(()=>{});
    if (this.readTask) await this.readTask;
    else if (this.port) { await this.port.close().catch(()=>{}); this.port=null; }
  }

  async write(text) {
    if (!this.writer) throw new Error('Connect the Arduino first.');
    await this.writer.write(new TextEncoder().encode(text + '\n'));
  }

  async command(command, training) {
    if (!this.writer || Date.now()/1000-this.last > 3) throw new Error('Wait for a fresh device reading.');
    if (this.pending) throw new Error('Waiting for the board.');
    if (command === 'START' && (this.device.state !== 'IDLE' || this.device.mode !== 'PASSAGE'))
      throw new Error('Wait for the passage firmware to report IDLE.');
    if (command === 'STOP' && this.device.state !== 'RUNNING') throw new Error('No running session.');
    this.pending = {command, training, at:Date.now()/1000};
    try { await this.write(command); }
    catch (error) { this.pending=null; throw error; }
  }

  checkGoal() {
    if (!this.plan || this.pending || this.device.state !== 'RUNNING') return;
    const {training_mode, target} = this.plan;
    if ((training_mode === 'count' && this.device.count >= target) ||
        (training_mode === 'timed' && this.device.seconds >= target)) {
      this.command('STOP').catch(error=>{this.error=error.message;});
    }
  }

  line(line) {
    const f = Object.fromEntries([...line.matchAll(/([A-Za-z_]+)=([^\s]+)/g)].map(m=>[m[1],m[2]]));
    if (!line) return;
    this.last = Date.now()/1000;
    if (line.startsWith('READY ')) {
      this.device.firmware = f.FW; this.device.mode = f.MODE?.replace(/:$/, '');
    } else if (['IDLE','RUNNING'].includes(f.STATE)) {
      const distance = Number(f.DIST_CM);
      Object.assign(this.device,{state:f.STATE, count:numberField(f.COUNT), seconds:numberField(f.SEC),
        distance_cm:Number.isFinite(distance)?distance:null, sensor:f.SENSOR, phase:f.PHASE});
      this.checkGoal();
    } else if (line.startsWith('STARTED ') && numberField(f.id)>0) {
      const id = Number(f.id);
      const plan = this.pending?.command === 'START' ? this.pending.training : null;
      this.plan = plan ? {...plan,id,started_at:this.last,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
        capture_key:crypto.randomUUID()} : null;
      this.pending = null;
      this.completion = null;
      Object.assign(this.device,{session_id:id,state:'RUNNING',count:0,seconds:0});
      this.event(line,'success');
    } else if (f.REP && numberField(f.REP)!==null) {
      this.device.count=Number(f.REP); this.checkGoal(); this.event(line,'count');
    } else if (line.startsWith('SAVED ') && numberField(f.id)>0 && numberField(f.count)!==null && numberField(f.seconds)!==null) {
      const id = Number(f.id), count=Number(f.count), seconds=Number(f.seconds);
      Object.assign(this.device,{session_id:id,state:'IDLE',count,seconds});
      if (this.plan?.id === id) {
        const info=this.port.getInfo();
        const row={...this.plan,count,seconds,saved_at:Math.max(this.last,this.plan.started_at),
          device_id:'usb:'+(info.usbVendorId ?? 'unknown')+':'+(info.usbProductId ?? 'unknown')};
        const key=this.prefix+row.capture_key;
        this.backlog.set(key,row);
        try { localStorage.setItem(key,JSON.stringify(row)); } catch { /* Keep retrying from memory. */ }
        this.completion = {status:'syncing', key:row.capture_key, id};
        this.storageError = null;
        this.nextRetry=0; this.sync();
      } else if (!this.completion || this.completion.id !== id) {
        this.completion = {status:'unassigned', id};
      }
      this.plan=this.pending=null;
      this.event(line,'success');
    } else if (line.startsWith('SAVE_FAILED ')) {
      this.completion = {status:'failed', id:numberField(f.id)};
      this.device.state='IDLE'; this.plan=this.pending=null; this.event(line,'error');
    } else if (/^(Already |Unknown command|Session ID limit|Command too long)/.test(line)) {
      this.pending=null; this.event(line,'error');
    }
  }

  async sync() {
    if (this.sending || !this.backlog.size || Date.now()<this.nextRetry) return;
    this.sending=true;
    this.nextRetry=Date.now()+10000;
    try {
      for (const [key,row] of this.backlog) {
        const response=await fetch('/api/sessions',{method:'POST', headers:{'Content-Type':'application/json',
          'X-Account-Id':String(this.account.id)},body:JSON.stringify(row),signal:AbortSignal.timeout(10000)});
        if (!response.ok) throw new Error('Upload failed');
        const {record} = await response.json();
        if (record?.id !== row.id || !Number.isSafeInteger(record.row_id) || record.row_id < 1 || !record.training_day)
          throw new Error('Missing database confirmation');
        this.savedRevision = Math.max(this.savedRevision, record.row_id);
        if (this.completion?.key === row.capture_key) this.completion = {status:'saved', ...record};
        this.backlog.delete(key);
        try { localStorage.removeItem(key); } catch { /* A later retry returns the same receipt. */ }
      }
      this.storageError=null;
    } catch { this.storageError='Saved on the board. Waiting to sync with your account.'; }
    finally { this.sending=false; }
  }
}
