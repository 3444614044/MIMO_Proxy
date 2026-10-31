# -*- coding: utf-8 -*-
"""
管理面板 HTML（单页）
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MiMo 代理 · 管理面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;
background:#0f1115;color:#e6e8eb;padding:20px;line-height:1.6}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#8b93a1;font-size:13px;margin-bottom:20px}
.card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin-bottom:12px;color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #262b36;vertical-align:top}
th{color:#8b93a1;font-weight:500;font-size:12px}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:7px 14px;
cursor:pointer;font-size:13px;margin-right:8px}
button:hover{background:#1d4ed8}
button.gray{background:#30363d}button.gray:hover{background:#3d444d}
button.red{background:#b62324}button.red:hover{background:#da3633}
input,textarea{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6e8eb;
border-radius:6px;padding:8px 10px;font-size:13px;font-family:inherit;margin-bottom:10px}
textarea{min-height:70px;resize:vertical;word-break:break-all}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
pre{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;
font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;color:#8b949e;
max-height:260px;overflow-y:auto}
.stat{display:inline-block;margin-right:24px}
.stat b{color:#fff;font-size:18px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.hint{color:#8b93a1;font-size:12px;margin-top:8px}
.topbar{display:flex;justify-content:space-between;align-items:center;
margin-bottom:16px;flex-wrap:wrap;gap:10px}
.badge{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb55;
border-radius:20px;padding:3px 12px;font-size:12px}
.err{background:#b6232422;border:1px solid #b6232455;color:#ff7b72;
border-radius:6px;padding:8px 12px;font-size:13px;margin-bottom:12px;display:none}
#loginCard{max-width:380px;margin:60px auto}
</style>
</head>
<body>

<!-- ========== 登录页 ========== -->
<div class="wrap" id="loginView" style="display:none">
  <div class="card" id="loginCard">
    <h2>🔐 管理员登录</h2>
    <div class="err" id="loginErr"></div>
    <input type="text" id="lgEmail" placeholder="管理员用户名" autocomplete="username">
    <input type="password" id="lgPass" placeholder="密码" autocomplete="current-password"
           onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()" style="width:100%">登 录</button>
    <div class="hint" id="lgHint">
      管理员账号独立于小米账号，不是邮箱。<br>
      默认 <span class="mono">admin</span> / <span class="mono">admin</span>，
      可用 <span class="mono">ADMIN_USER</span> / <span class="mono">ADMIN_PASS</span> 修改。
    </div>
  </div>
</div>

<!-- ========== 主面板 ========== -->
<div class="wrap" id="mainView" style="display:none">
  <div class="topbar">
    <div>
      <h1>🚀 MiMo v2.6 代理 · 管理面板</h1>
      <div class="sub">多账号轮询 · 工具调用 · 上传 · 自动刷新 · Anthropic · Responses · TTS/ASR</div>
    </div>
    <div class="row">
      <span class="badge" id="whoami">—</span>
      <button class="gray" onclick="doLogout()">退出</button>
    </div>
  </div>

  <div class="card">
    <h2>📊 概览</h2>
    <div id="stats"></div>
  </div>

  <div class="card">
    <h2>👥 账号池</h2>
    <table id="acct"><thead><tr>
      <th>#</th><th>userId</th><th>账号</th><th>状态</th><th>失败</th><th>最近错误</th><th>操作</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>📦 配额（mimo-claw）</h2>
    <div style="margin-bottom:8px"><button class="gray" onclick="loadQuota()">刷新配额</button>
      <span class="hint" style="margin-left:8px">读取每个账号的免费额度与存储用量</span></div>
    <table id="quota"><thead><tr>
      <th>userId</th><th>状态</th><th>额度</th><th>已用%</th><th>集群</th><th>存储</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>🐾 沙箱容器（mimo-claw）</h2>
    <div class="row" style="margin-bottom:8px">
      <button class="gray" onclick="loadClaw()">查容器</button>
      <button onclick="createClaw()">申请容器</button>
      <button class="gray" onclick="ensureClaw()">保障可用</button>
      <button class="gray" onclick="releaseClaw()">释放</button>
      <button class="gray" onclick="wsClaw()">WS 票据</button>
      <button class="gray" onclick="clawSessions()">沙箱会话</button>
    </div>
    <div class="row" style="margin-bottom:8px">
      <button class="gray" onclick="clawSchedule()">调度状态</button>
      <button class="gray" onclick="clawScheduleRun()">立即跑一轮</button>
      <span class="hint" style="margin-left:8px">一个号一个建 · 每号每天限次 · 已有容器则跳过</span>
    </div>
    <div class="row" style="margin-bottom:8px">
      <input id="clawCid" placeholder="会话 ID（读取历史消息用）"
             style="flex:1" autocomplete="off">
      <button class="gray" onclick="clawDialog()">读消息</button>
    </div>
    <pre id="clawBox" class="mono"
         style="white-space:pre-wrap;max-height:320px;overflow:auto">（点上方按钮查询）</pre>
    <div class="hint">
      「保障可用」= 无容器时自动申请并等待就绪；容器状态缓存
      <span class="mono">claw.ensure_cache_ttl</span> 秒。
    </div>
  </div>

  <div class="card">
    <h2>🔑 账密登录（添加小米账号）</h2>
    <input id="accUser" placeholder="小米账号：邮箱 / 手机号" autocomplete="off">
    <input id="accPass" type="password" placeholder="小米账号密码" autocomplete="off"
           onkeydown="if(event.key==='Enter')addCred()">
    <input id="accPh" placeholder="xiaomichatbot_ph（可选，留空尝试自动获取）">
    <div class="row">
      <button onclick="addCred()">添加并登录</button>
      <button class="gray" onclick="clearCred()">清空</button>
    </div>
    <div class="hint">
      填小米账号的邮箱/手机号 + 密码，代理会自动登录换取 serviceToken。<br>
      若提示「缺 ph」，从浏览器 Cookie 里复制 <span class="mono">xiaomichatbot_ph</span> 填到上面第三格再添加一次。
    </div>
  </div>

  <div class="card">
    <h2>➕ Cookie 导入（支持多行批量）</h2>
    <textarea id="cookie" placeholder='serviceToken=xxx; userId=123456; xiaomichatbot_ph=yyy'></textarea>
    <div class="row">
      <button onclick="addCookie()">导入</button>
      <button class="gray" onclick="loadAll()">刷新列表</button>
    </div>
    <div class="hint">多行可一次导入多个账号。</div>
  </div>

  <div class="card">
    <h2>📦 JSON 批量导入（accounts.json）</h2>
    <textarea id="acctJson" placeholder='把 accounts.json 的内容整段粘进来，例如：
{"accounts":[{"uid":"6897302049","ph_enc":"enc1:...","token_enc":"enc1:..."}]}
也支持明文: {"accounts":[{"uid":"123","ph":"xxx","token":"yyy"}]}'></textarea>
    <div class="row">
      <button onclick="addJson()">导入</button>
      <button class="gray" onclick="document.getElementById('acctJson').value=''">清空</button>
    </div>
    <div class="hint">
      支持代理自动保存的加密格式（<span class="mono">enc1:</span> 前缀）和明文格式，一次可导多个账号。<br>
      ⚠️ 加密内容必须来自<b>本机</b>（.secret_key 相同）；跨机器请先用 Cookie 串或明文 token。
    </div>
  </div>

  <div class="card">
    <h2>🧪 快速测试</h2>
    <div class="row">
      <button onclick="testChat()">测试聊天</button>
      <button class="gray" onclick="testAnthropic()">测试 Anthropic</button>
      <button class="gray" onclick="testResponses()">测试 Responses</button>
    </div>
    <pre id="testout">等待操作…</pre>
  </div>

  <div class="card">
    <h2>📜 最近日志</h2>
    <pre id="logs">—</pre>
  </div>
</div>

<script>
let TOKEN = localStorage.getItem('mimo_admin_token') || '';
let APIKEY = localStorage.getItem('mimo_api_key') || '';

const adminHdr = () => ({'X-Admin-Token': TOKEN, 'Content-Type':'application/json'});
// OpenAI/Anthropic 端点用 Bearer API Key，不能带管理员头
const apiHdr  = () => ({'Authorization': 'Bearer ' + APIKEY, 'Content-Type':'application/json'});

function show(v){
  document.getElementById('loginView').style.display = v==='login'?'':'none';
  document.getElementById('mainView').style.display  = v==='main' ?'':'none';
}

async function api(path, opt={}, useAdmin=true){
  opt.headers = Object.assign(useAdmin?adminHdr():apiHdr(), opt.headers||{});
  const r = await fetch(path, opt);
  if(r.status===401){
    // 会话失效 -> 回登录页，不弹原生密码框
    TOKEN=''; localStorage.removeItem('mimo_admin_token');
    show('login');
    return null;
  }
  return r.json();
}

async function doLogin(){
  const user = document.getElementById('lgEmail').value.trim();
  const pass = document.getElementById('lgPass').value;
  const errEl = document.getElementById('loginErr');
  errEl.style.display='none';
  if(!user || !pass){ errEl.textContent='请填写用户名和密码'; errEl.style.display=''; return; }
  try{
    const r = await fetch('/api/admin/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user:user,pass:pass})});
    const j = await r.json();
    if(j && j.ok){
      TOKEN = j.token;
      localStorage.setItem('mimo_admin_token', TOKEN);
      document.getElementById('lgPass').value='';
      errEl.style.display='none';
      await boot();
    }else{
      errEl.textContent = (j && j.error) || '登录失败';
      errEl.style.display='';
    }
  }catch(e){
    errEl.textContent='网络错误: '+e; errEl.style.display='';
  }
}

async function doLogout(){
  try{ await fetch('/api/admin/logout',{method:'POST',headers:adminHdr()}); }catch(e){}
  TOKEN=''; localStorage.removeItem('mimo_admin_token');
  show('login');
}

async function boot(){
  // 校验已有会话
  const s = await fetch('/api/admin/session',{headers:{'X-Admin-Token':TOKEN}})
              .then(r=>r.json()).catch(()=>null);
  if(!s || !s.ok){ TOKEN=''; show('login'); return; }
  document.getElementById('whoami').textContent = s.user;
  show('main');
  // 取 API Key 供自测按钮使用
  const cfg = await api('/api/admin/config');
  if(cfg && cfg.apiKey){ APIKEY = cfg.apiKey; localStorage.setItem('mimo_api_key', APIKEY); }
  loadAll();
}

async function loadAll(){
  const s = await api('/api/admin/stats');
  if(s){
    document.getElementById('stats').innerHTML =
      `<div class="stat">账号 <b>${s.accounts}</b></div>
       <div class="stat">可用 <b class="${s.valid>0?'ok':'bad'}">${s.valid}</b></div>
       <div class="stat">请求 <b>${s.requests}</b></div>
       <div class="stat">失败 <b class="${s.failed>0?'warn':''}">${s.failed}</b></div>
       <div class="stat">运行时长 <b>${Math.floor(s.uptime/60)}m</b></div>
       <div class="stat">自动刷新 <b>${s.autoRefresh?'开':'关'}</b></div>`;
  }

  const a = await api('/api/admin/accounts');
  if(a){
    document.getElementById('acct').querySelector('tbody').innerHTML =
      a.accounts.map((x,i)=>`<tr>
        <td>${i}</td>
        <td class="mono">${x.uid||'-'}</td>
        <td class="mono">${x.user||'(Cookie模式)'}${x.has_password?' 🔑':''}</td>
        <td class="${x.valid?'ok':'bad'}">${x.valid?'✅ 有效':'❌ 失效'}</td>
        <td>${x.fail_count}</td>
        <td class="mono" style="max-width:220px">${x.last_error||''}</td>
        <td><button class="gray" onclick="testAcct(${i})">测试</button>
            <button class="red" onclick="delAcct(${i})">删</button></td>
      </tr>`).join('') || '<tr><td colspan="7">（暂无账号，可用上方「账密登录」添加）</td></tr>';
  }

  const l = await api('/api/admin/logs');
  if(l) document.getElementById('logs').textContent = (l.logs||[]).join('\n') || '（暂无）';
}

async function loadClaw(){
  const box=document.getElementById('clawBox');
  box.textContent='查询中…';
  try{
    const r=await api('GET','/api/admin/claw?wait=1');
    box.textContent=JSON.stringify(r.claws,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function createClaw(){
  const box=document.getElementById('clawBox');
  if(!confirm('为所有账号申请 mimo-claw 容器？创建中会自动等待就绪。'))return;
  box.textContent='申请中…';
  try{
    const r=await api('POST','/api/admin/claw/create',{wait:true});
    box.textContent=JSON.stringify(r.results,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function ensureClaw(){
  const box=document.getElementById('clawBox');
  box.textContent='保障中…';
  try{
    const r=await api('POST','/api/admin/claw/ensure',{auto_create:true});
    box.textContent=JSON.stringify(r.results,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function releaseClaw(){
  const box=document.getElementById('clawBox');
  if(!confirm('释放所有账号的 mimo-claw 容器？'))return;
  box.textContent='释放中…';
  try{
    const r=await api('POST','/api/admin/claw/release',{});
    box.textContent=JSON.stringify(r.results,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function wsClaw(){
  const box=document.getElementById('clawBox');
  box.textContent='获取中…';
  try{
    const r=await api('GET','/api/admin/claw/ws');
    box.textContent=JSON.stringify(r.results,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function clawSessions(){
  const box=document.getElementById('clawBox');
  box.textContent='查询中…';
  try{
    const r=await api('GET','/api/admin/claw/conversations');
    box.textContent=JSON.stringify(r.clawConversations,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function clawDialog(){
  const box=document.getElementById('clawBox');
  const cid=(document.getElementById('clawCid').value||'').trim();
  if(!cid){ box.textContent='请先填会话 ID'; return; }
  box.textContent='读取中…';
  try{
    const r=await api('GET','/api/admin/claw/dialog?conversationId='+encodeURIComponent(cid));
    box.textContent=JSON.stringify(r.dialog,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function clawSchedule(){
  const box=document.getElementById('clawBox');
  box.textContent='查询中…';
  try{
    const r=await api('GET','/api/admin/claw/schedule');
    box.textContent=JSON.stringify(r,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function clawScheduleRun(){
  const box=document.getElementById('clawBox');
  box.textContent='执行中…（一个号一个建，请稍候）';
  try{
    const r=await api('POST','/api/admin/claw/schedule/run',{});
    box.textContent=JSON.stringify(r,null,1);
  }catch(e){ box.textContent='失败: '+e.message; }
}

async function loadQuota(){
  const box = document.getElementById('quota').querySelector('tbody');
  box.innerHTML = '<tr><td colspan="6">查询中…</td></tr>';
  const q = await api('/api/admin/quota');
  if(!q){ box.innerHTML = '<tr><td colspan="6">（未登录）</td></tr>'; return; }
  const fmt = (n) => {
    if(n === null || n === undefined) return '-';
    if(n >= 1e8) return (n/1e8).toFixed(2) + '亿';
    if(n >= 1e4) return (n/1e4).toFixed(1) + '万';
    return String(n);
  };
  const bytes = (n) => {
    if(n === null || n === undefined) return '-';
    if(n >= 1<<30) return (n/(1<<30)).toFixed(1) + ' GB';
    if(n >= 1<<20) return (n/(1<<20)).toFixed(1) + ' MB';
    if(n >= 1<<10) return (n/(1<<10)).toFixed(1) + ' KB';
    return n + ' B';
  };
  box.innerHTML = (q.quotas||[]).map(x=>{
    if(x.error){
      return `<tr><td class="mono">${x.uid||'-'}</td>
        <td class="bad" colspan="5">${x.error}</td></tr>`;
    }
    const f = x.freeQuota||{};
    const st = x.storage||{};
    const pct = f.percent === 0 ? '0%' : (f.percent ? (f.percent*100).toFixed(2)+'%' : '-');
    const used = st.used !== undefined ? bytes(st.used)+' / '+bytes(st.max) : '-';
    const stt = x.status || '-';
    const cls = stt === 'AVAILABLE' ? 'ok' : (stt === 'CREATING' ? 'warn' : '');
    return `<tr>
      <td class="mono">${x.uid||'-'}</td>
      <td class="${cls}">${stt}</td>
      <td class="mono">${fmt(f.remaining)} / ${fmt(f.limit)}</td>
      <td>${pct}</td>
      <td class="mono">${x.cluster||'-'}</td>
      <td class="mono">${used}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6">（暂无账号）</td></tr>';
}

async function addCred(){
  const u = document.getElementById('accUser').value.trim();
  const p = document.getElementById('accPass').value;
  const ph = document.getElementById('accPh').value.trim();
  if(!u || !p) return alert('请填写小米账号和密码');
  const btn = event && event.target;
  if(btn){ btn.disabled=true; btn.textContent='登录中…'; }
  try{
    const r = await api('/api/admin/accounts', {method:'POST',
      body:JSON.stringify({user:u, pass:p, ph:ph})});
    if(r && r.ok){
      document.getElementById('accPass').value='';
      document.getElementById('accPh').value='';
      alert('✅ 添加成功，userId='+(r.uid||'未知'));
    }else if(r && r.needPh){
      alert('⚠️ 已登录但缺少 xiaomichatbot_ph，请在第三格填入后再添加一次。');
    }else{
      alert('❌ '+(r ? (r.error||'登录失败') : '请求失败'));
    }
  }catch(e){ alert('网络错误: '+e); }
  finally{ if(btn){ btn.disabled=false; btn.textContent='添加并登录'; } }
  loadAll();
}

function clearCred(){
  document.getElementById('accUser').value='';
  document.getElementById('accPass').value='';
  document.getElementById('accPh').value='';
}

async function addCookie(){
  const raw = document.getElementById('cookie').value.trim();
  if(!raw) return alert('请粘贴 Cookie 串');
  const lines = raw.split('\n').map(x=>x.trim()).filter(Boolean);
  let ok=0, fail=0;
  for(const line of lines){
    const r = await api('/api/admin/accounts',
      {method:'POST', body:JSON.stringify({cookie:line})});
    if(r && r.ok) ok++; else fail++;
  }
  alert(`导入完成：成功 ${ok}，失败 ${fail}`);
  document.getElementById('cookie').value='';
  loadAll();
}

async function addJson(){
  const raw = document.getElementById('acctJson').value.trim();
  if(!raw) return alert('请粘贴 accounts.json 内容');
  let data;
  try{ data = JSON.parse(raw); }catch(e){ return alert('JSON 解析失败: '+e.message); }
  const r = await api('/api/admin/accounts',
    {method:'POST', body:JSON.stringify({accounts:(data.accounts||data)})});
  if(!r){ return; }
  let msg = `导入完成：成功 ${(r.added||[]).length}，失败 ${(r.failed||[]).length}`;
  if(r.hint) msg += '\n\n💡 '+r.hint;
  if((r.failed||[]).length){
    msg += '\n\n失败明细:\n' + r.failed.map(f=>`uid=${f.uid||'?'} ${f.error}`).join('\n');
  }
  alert(msg);
  document.getElementById('acctJson').value='';
  loadAll();
}

async function testAcct(i){
  const r = await api(`/api/admin/accounts/${i}/test`, {method:'POST'});
  alert(r && r.ok ? '✅ 可用' : '❌ '+(r?r.error:'不可用'));
  loadAll();
}

async function delAcct(i){
  if(!confirm('确定删除该账号？')) return;
  await api(`/api/admin/accounts/${i}`, {method:'DELETE'});
  loadAll();
}

// 以下测试调用 OpenAI/Anthropic 端点，必须用 Bearer API Key（useAdmin=false）
async function testChat(){
  const o = document.getElementById('testout'); o.textContent='请求中…';
  const t0 = Date.now();
  const r = await api('/v1/chat/completions', {method:'POST',
    body:JSON.stringify({model:'mimo-v2.6-pro',messages:[{role:'user',content:'只回复ok'}],stream:false})}, false);
  o.textContent = `(${Date.now()-t0}ms)\n`+JSON.stringify(r,null,2).slice(0,1500);
}
async function testAnthropic(){
  const o = document.getElementById('testout'); o.textContent='请求中…';
  const r = await api('/v1/messages', {method:'POST',
    body:JSON.stringify({model:'claude-sonnet-4-5',max_tokens:64,messages:[{role:'user',content:'只回复ok'}]})}, false);
  o.textContent = JSON.stringify(r,null,2).slice(0,1500);
}
async function testResponses(){
  const o = document.getElementById('testout'); o.textContent='请求中…';
  const r = await api('/v1/responses', {method:'POST',
    body:JSON.stringify({model:'mimo-v2.6-pro',input:'只回复ok'})}, false);
  o.textContent = JSON.stringify(r,null,2).slice(0,1500);
}

boot();
setInterval(()=>{ if(TOKEN) loadAll(); }, 15000);
</script>
</body>
</html>
"""