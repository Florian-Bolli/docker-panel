#!/usr/bin/env python3
"""Minimal Docker panel: list all containers, start/stop them, view live logs.
Stdlib only.

Talks to the Docker Engine API over /var/run/docker.sock.
Listens on 127.0.0.1 only. API requests must carry X-Panel: 1 and a localhost
Host header, so random websites can't toggle containers (CSRF / DNS rebinding).
"""
import calendar
import http.client
import json
import os
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Inside the container the panel listens on 0.0.0.0; Docker publishes it on 127.0.0.1 only.
HOST, PORT = os.environ.get("PANEL_HOST", "127.0.0.1"), 9001
SOCK = "/var/run/docker.sock"
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "containers.localhost", "docker.local"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
CURSOR = re.compile(r"^\d+\.\d{9}$")
MAX_LINES = 1000
_tty_cache = {}


class UnixConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=30)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(SOCK)


def docker(method, path, raw=False):
    c = UnixConn()
    try:
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        if raw and r.status == 200:
            return r.status, body
        return r.status, (json.loads(body) if body else None)
    finally:
        c.close()


def health_of(status):
    m = re.search(r"\((health: )?(starting|healthy|unhealthy)\)", status)
    return m.group(2) if m else None


def list_containers():
    _, data = docker("GET", "/containers/json?all=1")
    out = []
    for c in data:
        ports = sorted({p["PublicPort"] for p in c.get("Ports", []) if p.get("PublicPort")})
        out.append({
            "id": c["Id"][:12],
            "name": c["Names"][0].lstrip("/"),
            "image": c["Image"] if len(c["Image"]) < 60 else c["Image"][:57] + "…",
            "state": c["State"],
            "status": c["Status"],
            "health": health_of(c["Status"]),
            "ports": ports,
            "project": c.get("Labels", {}).get("com.docker.compose.project", ""),
        })
    out.sort(key=lambda x: (x["state"] != "running", x["project"] or "~", x["name"]))
    return out


def is_tty(cid):
    if cid not in _tty_cache:
        status, info = docker("GET", f"/containers/{cid}/json")
        if status != 200:
            raise LookupError((info or {}).get("message", "container not found"))
        _tty_cache[cid] = bool(info["Config"].get("Tty"))
    return _tty_cache[cid]


def demux(body):
    """Strip Docker's 8-byte stream frame headers (non-TTY containers)."""
    out, i = bytearray(), 0
    while i + 8 <= len(body):
        size = int.from_bytes(body[i + 4:i + 8], "big")
        out += body[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(out)


def parse_ts(s):
    main, _, frac = s.rstrip("Z").partition(".")
    sec = calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S"))
    return sec, int((frac + "000000000")[:9])


def get_details(cid):
    """Ports (exposed + published) and current memory use of one container."""
    status, info = docker("GET", f"/containers/{cid}/json")
    if status != 200:
        raise LookupError((info or {}).get("message", "container not found"))
    cfg, hc, ns = info["Config"], info["HostConfig"], info["NetworkSettings"]
    running = info["State"]["Running"]
    live = ns.get("Ports") or {}          # actual bindings while running
    wanted = hc.get("PortBindings") or {}  # configured bindings (also when stopped)
    keys = set(cfg.get("ExposedPorts") or {}) | set(wanted) | set(live)

    def sort_key(k):
        num = k.split("/")[0].split("-")[0]
        return (int(num) if num.isdigit() else 0, k)

    ports = []
    for k in sorted(keys, key=sort_key):
        port, _, proto = k.partition("/")
        binds = (live.get(k) if running else wanted.get(k)) or []
        pub = {}
        for b in binds:
            hp = b.get("HostPort")
            if hp:
                pub.setdefault(hp, set()).add(b.get("HostIp") or "0.0.0.0")
        ports.append({
            "container": port, "proto": proto or "tcp",
            "published": [{"port": hp, "ips": sorted(ips)} for hp, ips in sorted(pub.items())],
        })

    mem = None
    if running:
        st, s = docker("GET", f"/containers/{cid}/stats?stream=false&one-shot=true")
        m = (s or {}).get("memory_stats") or {}
        if st == 200 and "usage" in m:
            extra = m.get("stats") or {}
            # same formula as `docker stats`: subtract reclaimable page cache
            cache = extra.get("inactive_file", extra.get("total_inactive_file", 0))
            mem = {"used": max(m["usage"] - cache, 0), "limit": m.get("limit"),
                   "limited": bool(hc.get("Memory"))}
    return {"network": hc.get("NetworkMode", ""), "ports": ports, "mem": mem, "running": running}


def get_logs(cid, cursor):
    """Return log lines newer than cursor ("sec.nanos"), or the last 200 if none."""
    tty = is_tty(cid)
    q = "stdout=1&stderr=1&timestamps=1"
    if cursor:
        q += f"&since={cursor}&tail=all"
        last = tuple(int(x) for x in cursor.split("."))
    else:
        q += "&tail=200"
        last = None
    status, body = docker("GET", f"/containers/{cid}/logs?{q}", raw=True)
    if status != 200:
        raise LookupError((body or {}).get("message", f"docker returned {status}"))
    text = (body if tty else demux(body)).decode("utf-8", "replace")
    lines, newest = [], last
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            continue
        ts_str, _, msg = raw_line.partition(" ")
        try:
            ts = parse_ts(ts_str)
        except ValueError:
            ts, msg = None, raw_line
        if ts is not None:
            if last and ts <= last:
                continue  # already sent
            newest = ts
        lines.append(ANSI.sub("", msg).rstrip("\r"))
    lines = lines[-MAX_LINES:]
    new_cursor = f"{newest[0]}.{newest[1]:09d}" if newest else cursor
    return {"lines": lines, "cursor": new_cursor}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Docker Panel</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mute:#6b7385;--line:#e3e6eb;--on:#1f9d55;--off:#a0a6b2;--wait:#d99a1e;--bad:#d64545;--btn:#eef0f4;--log:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--mute:#8d95a5;--line:#2a2f38;--on:#3ccf7a;--off:#5b6270;--wait:#f0b43c;--bad:#ef6464;--btn:#262b34;--log:#12151a}}
[hidden]{display:none!important}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:900px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute);margin-bottom:18px;font-size:13px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:10px 14px;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;background:var(--off);flex:none}
.is-running .dot{background:var(--on)}.is-busy .dot{background:var(--wait);animation:pulse 1s infinite}.is-bad .dot{background:var(--bad)}
@keyframes pulse{50%{opacity:.35}}
.info{flex:1;min-width:180px}.name{font-weight:600}.meta{color:var(--mute);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--btn);color:var(--mute);margin-left:6px;font-weight:500}
.badge{font-size:12px;color:var(--wait);display:none;align-items:center;gap:6px}.is-busy .badge{display:flex}
.spin{width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:rot .7s linear infinite;display:inline-block}
@keyframes rot{to{transform:rotate(360deg)}}
a{color:inherit}
.btns{display:flex;gap:6px}
button{border:0;border-radius:7px;padding:7px 12px;font:inherit;font-size:13px;cursor:pointer;background:var(--btn);color:var(--fg);display:inline-flex;align-items:center;gap:6px;justify-content:center}
.act{min-width:84px}.act.stop{background:var(--bad);color:#fff}.act.start{background:var(--on);color:#fff}
button:disabled{opacity:.6;cursor:wait}
.logbtn .car{transition:transform .15s;display:inline-block}.logbtn[aria-expanded=true] .car{transform:rotate(180deg)}
.drop{border-top:1px solid var(--line)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:12px 14px;font-size:13px}
@media (max-width:600px){.facts{grid-template-columns:1fr}}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin-bottom:6px;font-weight:600}
.pline{margin-bottom:4px}.pline code{font:12px ui-monospace,Menlo,monospace}
.hint{color:var(--mute);font-size:12px}
.bar{height:6px;background:var(--btn);border-radius:3px;overflow:hidden;margin:6px 0 4px}
.bar i{display:block;height:100%;background:var(--on);border-radius:3px;transition:width .3s}
.bar.warn i{background:var(--wait)}.bar.bad i{background:var(--bad)}
.memval{font-size:18px;font-weight:600}
.tools{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 14px;font-size:12px;color:var(--mute);flex-wrap:wrap}
.del{background:transparent;color:var(--bad);border:1px solid var(--bad);padding:5px 10px}
.del:hover{background:var(--bad);color:#fff}
.confirm{display:none;align-items:center;gap:8px;flex-wrap:wrap;background:color-mix(in srgb,var(--bad) 12%,transparent);border-radius:8px;padding:8px 10px;color:var(--fg);width:100%}
.confirm.show{display:flex}.confirm .txt{flex:1;min-width:200px}
.confirm .yes{background:var(--bad);color:#fff}
pre.logs{margin:0;border-top:1px solid var(--line);background:var(--log);font:12px/1.45 ui-monospace,Menlo,monospace;padding:10px 14px;max-height:360px;overflow:auto;white-space:pre-wrap;word-break:break-all}
pre.logs:empty::before{content:"No logs yet…";color:var(--mute)}
.err{color:var(--bad);font-size:13px;margin:8px 0}
</style></head><body><main>
<h1>Docker containers</h1><div class="sub" id="sub">loading…</div>
<div id="err" class="err"></div><div id="list"></div>
</main><script>
const H={'X-Panel':'1'};
const $list=document.getElementById('list'), $err=document.getElementById('err');
const rows=new Map();      // id -> {el, parts, logs:{open,cursor,timer}}
const pending=new Map();   // id -> 'start' | 'stop'
let data=[], timer=null;

function busyLabel(c){
  const p=pending.get(c.id);
  if(c.state==='restarting') return 'Restarting…';
  if(p==='stop') return 'Stopping…';
  if(p==='start' && c.state!=='running') return 'Starting…';
  if(c.state==='running' && c.health==='starting') return 'Starting… (health check)';
  return null;
}

function row(c){
  let r=rows.get(c.id);
  if(r) return r;
  const el=document.createElement('div');
  el.className='item';
  el.innerHTML=`<div class="row"><span class="dot"></span>
    <div class="info"><div class="name"></div><div class="meta"></div></div>
    <span class="badge"><span class="spin"></span><span class="btxt"></span></span>
    <div class="btns"><button class="logbtn" aria-expanded="false">Details <span class="car">▾</span></button>
    <button class="act"></button></div></div>
    <div class="tools stopwarn" hidden><div class="confirm show"><span class="txt"></span>
      <button class="sno">Cancel</button><button class="yes syes">Yes, stop it</button></div></div>
    <div class="drop" hidden>
      <div class="facts"><div><div class="lbl">Ports</div><div class="ports hint">loading…</div></div>
        <div><div class="lbl">Memory</div><div class="mem hint">loading…</div></div></div>
      <div class="tools"><span>Live logs</span><button class="del">Delete container</button>
        <div class="confirm"><span class="txt"></span><button class="no">Cancel</button><button class="yes">Yes, delete</button></div>
      </div><pre class="logs"></pre></div>`;
  const q=s=>el.querySelector(s);
  r={el,parts:{name:q('.name'),meta:q('.meta'),btxt:q('.btxt'),act:q('.act'),logbtn:q('.logbtn'),pre:q('pre'),
     drop:q('.drop'),del:q('.del'),confirm:q('.drop .confirm'),ctxt:q('.drop .confirm .txt'),yes:q('.drop .yes'),no:q('.no'),
     stopwarn:q('.stopwarn'),stxt:q('.stopwarn .txt'),syes:q('.syes'),sno:q('.sno'),
     ports:q('.ports'),mem:q('.mem')},
     logs:{open:false,cursor:'',timer:null,dtimer:null}};
  r.parts.act.onclick=()=>toggle(c.id);
  r.parts.logbtn.onclick=()=>toggleLogs(c.id);
  r.parts.del.onclick=()=>askDelete(c.id);
  r.parts.no.onclick=()=>{r.parts.confirm.classList.remove('show'); r.parts.del.hidden=false;};
  r.parts.yes.onclick=()=>doDelete(c.id);
  r.parts.sno.onclick=()=>{r.parts.stopwarn.hidden=true;};
  r.parts.syes.onclick=()=>{r.parts.stopwarn.hidden=true; toggle(c.id,true);};
  rows.set(c.id,r);
  return r;
}

function render(){
  const seen=new Set();
  let run=0;
  for(const c of data){
    seen.add(c.id); if(c.state==='running') run++;
    const r=row(c), p=r.parts, busy=busyLabel(c);
    const on=c.state==='running';
    r.el.className='item'+(on?' is-running':'')+(busy?' is-busy':'')+(c.health==='unhealthy'?' is-bad':'');
    p.name.innerHTML=esc(c.name)+(c.project?`<span class="tag">${esc(c.project)}</span>`:'')
      +(c.health==='unhealthy'?'<span class="tag" style="color:var(--bad)">unhealthy</span>':'');
    p.meta.innerHTML=`${esc(c.status)} · ${esc(c.image)} `+c.ports.map(x=>`<a href="http://localhost:${x}" target="_blank">:${x}</a>`).join(' ');
    p.btxt.textContent=busy||'';
    const act=pending.get(c.id);
    if(act){ p.act.disabled=true; p.act.className='act '+(act==='start'?'start':'stop'); p.act.innerHTML='<span class="spin"></span>'; }
    else { p.act.disabled=false; p.act.className='act '+(on?'stop':'start'); p.act.textContent=on?'Stop':'Start'; }
    $list.appendChild(r.el);  // keeps order, moves existing nodes
  }
  for(const [id,r] of rows) if(!seen.has(id)){ clearTimeout(r.logs.timer); clearTimeout(r.logs.dtimer); r.el.remove(); rows.delete(id); }
  document.getElementById('sub').textContent=`${run} running · ${data.length} total`;
}

async function load(){
  clearTimeout(timer);
  try{
    const r=await fetch('/api/containers',{headers:H});
    data=await r.json();
    // a pending action is done once the container reached its target state
    for(const [id,act] of pending){
      const c=data.find(x=>x.id===id);
      if(!c || (act==='start' && c.state==='running') || (act==='stop' && c.state!=='running')) pending.delete(id);
    }
    render(); if(!pending.size) $err.textContent='';
  }catch(e){ $err.textContent='Failed to load: '+e; }
  const busy=data.some(busyLabel);
  timer=setTimeout(load, busy?1000:5000);
}

// Containers this panel depends on: stopping/deleting them gets an extra warning.
const UP='run <code>docker compose up -d</code> in <code>~/Programs/docker-panel</code>';
const PROXY_WHAT=' <b>local-proxy</b> serves this panel at <b>docker.local</b> and <b>containers.localhost</b>.'
  +' Those addresses stop working right away, so this page will lose its connection.';
const PANEL_WHAT=' <b>docker-panel</b> is this panel. This page stops working right away,'
  +' and it can\'t be started again from here.';
const SELF={
  'local-proxy':{q:'Stop the panel\'s own proxy?',
    stop:PROXY_WHAT+' To get it back, open <a href="http://localhost:9001">http://localhost:9001</a> and start local-proxy there.',
    del:PROXY_WHAT+' To get it back, '+UP+'.'},
  'docker-panel':{q:'Stop the Docker panel itself?',
    stop:PANEL_WHAT+' To get it back, run <code>docker start docker-panel</code> in a terminal (it also comes back after a reboot).',
    del:PANEL_WHAT+' To get it back, '+UP+'.'},
};

async function toggle(id, confirmed){
  const c=data.find(x=>x.id===id); if(!c) return;
  const act=c.state==='running'?'stop':'start';
  if(act==='stop' && SELF[c.name] && !confirmed){
    const p=rows.get(id).parts;
    p.stxt.innerHTML=SELF[c.name].q+SELF[c.name].stop;
    p.stopwarn.hidden=false; return;
  }
  pending.set(id,act); render(); $err.textContent='';
  try{
    const r=await fetch(`/api/${act}/${id}`,{method:'POST',headers:H});
    if(!r.ok){ pending.delete(id); $err.textContent=`${c.name}: `+((await r.json()).error||'failed'); }
  }catch(e){
    pending.delete(id);
    $err.innerHTML=c.name==='docker-panel'
      ?'The panel is stopping, so this page no longer responds. Run <code>docker start docker-panel</code> to bring it back.'
      :esc(`${c.name}: ${e}`);
    if(c.name==='docker-panel') return;
  }
  await load();
  const now=data.find(x=>x.id===id);
  if(act==='start' && now && now.state!=='running') $err.textContent=`${c.name} exited right after starting: ${now.status}. Open its logs to see why.`;
}

function toggleLogs(id){
  const r=rows.get(id), L=r.logs, p=r.parts;
  L.open=!L.open;
  p.drop.hidden=!L.open; p.logbtn.setAttribute('aria-expanded',L.open);
  p.confirm.classList.remove('show'); p.del.hidden=false;
  if(L.open){ p.pre.textContent=''; L.cursor=''; p.ports.textContent=p.mem.textContent='loading…'; pollLogs(id); pollDetails(id); }
  else { clearTimeout(L.timer); clearTimeout(L.dtimer); }
}

function fmtBytes(b){
  const u=['B','KB','MB','GB','TB']; let i=0;
  while(b>=1024 && i<u.length-1){ b/=1024; i++; }
  return (b>=100||i===0?Math.round(b):b.toFixed(1))+' '+u[i];
}

function ipLabel(ips){
  const local=ips.every(ip=>ip==='127.0.0.1'||ip==='::1');
  return local?'this computer only':'reachable from your network';
}

function renderPorts(d){
  if(d.network==='host') return '<div class="hint">Uses the <b>host network</b>: it opens ports directly on this machine, so Docker has no port list for it.</div>';
  if(!d.ports.length) return '<div class="hint">No ports exposed.</div>';
  return d.ports.map(p=>{
    const cp=`<code>${esc(p.container)}/${esc(p.proto)}</code>`;
    if(!p.published.length) return `<div class="pline">${cp} <span class="hint">· exposed, not published (only other containers can reach it)</span></div>`;
    return p.published.map(b=>{
      const host=p.proto==='tcp'?`<a href="http://localhost:${esc(b.port)}" target="_blank"><code>localhost:${esc(b.port)}</code></a>`:`<code>${esc(b.port)}</code>`;
      return `<div class="pline">${host} → ${cp} <span class="hint">· ${ipLabel(b.ips)}</span></div>`;
    }).join('');
  }).join('')+(d.running?'':'<div class="hint">Container is stopped: showing configured ports.</div>');
}

function renderMem(d){
  if(!d.mem) return '<div class="hint">Not running.</div>';
  const {used,limit,limited}=d.mem, pct=limit?used/limit*100:0;
  const cls=pct>90?' bad':pct>70?' warn':'';
  const of=limited?`of ${fmtBytes(limit)} limit (${pct.toFixed(0)}%)`:`no limit set · ${pct<0.1?'<0.1':pct.toFixed(1)}% of ${fmtBytes(limit)} system RAM`;
  return `<div class="memval">${fmtBytes(used)}</div><div class="bar${cls}"><i style="width:${Math.min(pct,100).toFixed(1)}%"></i></div><div class="hint">${of}</div>`;
}

async function pollDetails(id){
  const r=rows.get(id); if(!r||!r.logs.open) return;
  const p=r.parts;
  try{
    const res=await fetch(`/api/details/${id}`,{headers:H});
    const d=await res.json();
    if(!res.ok) throw new Error(d.error);
    if(!r.logs.open) return;
    p.ports.className='ports'; p.ports.innerHTML=renderPorts(d);
    p.mem.className='mem'; p.mem.innerHTML=renderMem(d);
  }catch(e){ p.mem.textContent='Error: '+e.message; }
  r.logs.dtimer=setTimeout(()=>pollDetails(id),3000);
}

function askDelete(id){
  const c=data.find(x=>x.id===id), p=rows.get(id).parts; if(!c) return;
  p.ctxt.innerHTML=`Permanently delete <b>${esc(c.name)}</b>?`
    +(c.state==='running'?' It is <b>running</b> and will be stopped first.':'')
    +' This cannot be undone. Its volumes and image are kept'
    +(c.project?`; <code>docker compose up</code> in <b>${esc(c.project)}</b> would recreate it.`:'.')
    +(SELF[c.name]?'<br><br><b>Warning:</b>'+SELF[c.name].del:'');
  p.del.hidden=true; p.confirm.classList.add('show');
}

async function doDelete(id){
  const c=data.find(x=>x.id===id), p=rows.get(id).parts;
  p.yes.disabled=p.no.disabled=true; p.yes.innerHTML='<span class="spin"></span> Deleting';
  try{
    const r=await fetch(`/api/remove/${id}`,{method:'POST',headers:H});
    if(!r.ok) throw new Error((await r.json()).error||'failed');
  }catch(e){
    $err.textContent=`${c?c.name:id}: ${e.message}`;
    p.yes.disabled=p.no.disabled=false; p.yes.textContent='Yes, delete';
  }
  load();
}

async function pollLogs(id){
  const r=rows.get(id); if(!r||!r.logs.open) return;
  const L=r.logs, pre=r.parts.pre;
  try{
    const res=await fetch(`/api/logs/${id}`+(L.cursor?`?since=${L.cursor}`:''),{headers:H});
    const j=await res.json();
    if(!res.ok) throw new Error(j.error);
    if(!r.logs.open) return;
    L.cursor=j.cursor||L.cursor;
    if(j.lines.length){
      const atBottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-24;
      pre.appendChild(document.createTextNode(j.lines.join('\n')+'\n'));
      while(pre.childNodes.length>200) pre.removeChild(pre.firstChild);
      if(atBottom) pre.scrollTop=pre.scrollHeight;
    }
  }catch(e){ pre.appendChild(document.createTextNode(`[log error: ${e.message}]\n`)); }
  L.timer=setTimeout(()=>pollLogs(id),1500);
}

function esc(s){return String(s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]))}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def guard(self, api=True):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self.send(403, {"error": "bad host"})
            return False
        if api and self.headers.get("X-Panel") != "1":
            self.send(403, {"error": "missing header"})
            return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        if url.path == "/":
            if self.guard(api=False):
                self.send(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/api/containers":
            if self.guard():
                self.send(200, list_containers())
        elif len(parts) == 3 and parts[:2] == ["api", "details"] and parts[2].isalnum():
            if not self.guard():
                return
            try:
                self.send(200, get_details(parts[2]))
            except LookupError as e:
                self.send(404, {"error": str(e)})
        elif len(parts) == 3 and parts[:2] == ["api", "logs"] and parts[2].isalnum():
            if not self.guard():
                return
            cursor = parse_qs(url.query).get("since", [""])[0]
            if cursor and not CURSOR.match(cursor):
                return self.send(400, {"error": "bad cursor"})
            try:
                self.send(200, get_logs(parts[2], cursor))
            except LookupError as e:
                self.send(404, {"error": str(e)})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "api" or parts[1] not in ("start", "stop", "remove") \
                or not parts[2].isalnum():
            return self.send(404, {"error": "not found"})
        if not self.guard():
            return
        if parts[1] == "remove":
            # force=1 stops it first if running; volumes are kept (no v=1)
            status, body = docker("DELETE", f"/containers/{parts[2]}?force=1")
            _tty_cache.pop(parts[2], None)
        else:
            status, body = docker("POST", f"/containers/{parts[2]}/{parts[1]}")
        if status in (204, 304):
            self.send(200, {"ok": True})
        else:
            self.send(500, {"error": (body or {}).get("message", f"docker returned {status}")})


if __name__ == "__main__":
    print(f"Docker panel on http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
