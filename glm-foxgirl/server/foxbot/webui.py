"""本地 WebUI 控制台（--webui，仅绑定 127.0.0.1）。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config
from .httpio import read_body
from .memory import t_query_memory
from .identity import t_manage_profile
from .persona import t_manage_persona, t_manage_rules
from .session import t_bot_status, t_get_stats, t_search_messages, t_session_note
from .stickers import t_manage_stickers

WEBUI_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>GLM狐狸娘 控制台</title>
<style>
body{font-family:system-ui,"Microsoft YaHei";margin:0;background:#111;color:#eee}
h1{font-size:18px;padding:12px 16px;margin:0;background:#1b1b1b;border-bottom:1px solid #333}
nav{display:flex;gap:4px;padding:8px 16px;background:#161616;flex-wrap:wrap}
nav button{background:#222;color:#ccc;border:1px solid #333;border-radius:6px;padding:6px 12px;cursor:pointer}
nav button.on{background:#3b5bdb;color:#fff}
main{padding:16px;max-width:1100px;margin:0 auto}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
th,td{border-bottom:1px solid #2a2a2a;padding:6px 8px;text-align:left;vertical-align:top}
button.act{background:#2f9e44;color:#fff;border:0;border-radius:4px;padding:3px 8px;cursor:pointer}
button.del{background:#c92a2a;color:#fff;border:0;border-radius:4px;padding:3px 8px;cursor:pointer}
textarea{width:100%;min-height:90px;background:#191919;color:#eee;border:1px solid #333;border-radius:6px;font-family:inherit}
input,select{background:#191919;color:#eee;border:1px solid #333;border-radius:4px;padding:4px}
.kpi{display:flex;gap:12px;flex-wrap:wrap}
.kpi div{background:#1b1b1b;border:1px solid #333;border-radius:8px;padding:10px 14px;min-width:110px}
.kpi b{display:block;font-size:20px;color:#7aa2ff}
small{color:#888}
</style></head><body>
<h1>🦊 GLM狐狸娘 控制台 <span id="ver" style="font-size:12px;color:#888"></span></h1>
<nav id="tabs"></nav><main id="view"></main>
<script>
let S=null,tab='状态';
const tabs=['状态','人格','记忆','画像','黑白名单','表情包','记录','统计'];
function esc(s){return (s==null?'':''+s).replace(/</g,'&lt;')}
async function act(path,body){await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});load()}
async function loadMsgs(){const k=document.getElementById('qk').value,c=document.getElementById('qc').value;
 const d=await (await fetch('/api/messages?keyword='+encodeURIComponent(k)+'&contact_id='+encodeURIComponent(c)+'&limit=100')).json();
 document.getElementById('msgbox').innerHTML=(d.messages||[]).map(m=>'<tr><td>'+m.created_at+'</td><td>'+(m.direction==='out'?'→发':'←收')+'</td><td>'+esc(m.channel)+'</td><td>'+esc(m.display_name||m.contact_id||'')+'</td><td>'+esc(m.content)+(m.media_url?' <small>'+esc(m.media_url)+'</small>':'')+'</td></tr>').join('')||'<tr><td colspan="5">无结果</td></tr>'}
async function load(){S=await (await fetch('/api/state')).json();document.getElementById('ver').textContent='v'+S.status.version;render()}
function nav(){document.getElementById('tabs').innerHTML=tabs.map(t=>`<button class="${t===tab?'on':''}" onclick="tab='${t}';nav();render()">${t}</button>`).join('')}
function render(){const v=document.getElementById('view');
 if(tab==='状态'){const c=S.status.counts;v.innerHTML=`<div class="kpi">
  <div>激活人格<b>${esc(S.status.active_persona||'无')}</b></div><div>待处理消息<b>${c.pending_messages}</b></div>
  <div>待补发<b>${c.outbox}</b></div><div>本轮交互<b>${(S.status.session||{}).turns||0}</b></div><div>记忆<b>${c.memories}</b></div><div>表情包<b>${c.stickers}</b></div>
  <div>规则<b>${c.rules}</b></div><div>collector<b>${S.status.collector.running?'运行中':'未运行'}</b></div></div>
  <p><small>心情：${S.stats.mood} ｜ 数据目录：${esc(S.status.data_dir)}（备份它即备份一切）</small></p>`}
 if(tab==='人格'){v.innerHTML='<table><tr><th>名称</th><th>状态</th><th>prompt（可编辑）</th><th>风格</th><th></th></tr>'+
  S.personas.personas.map(p=>'<tr><td>'+esc(p.name)+'</td><td>'+(p.is_active?'▶激活':'')+'</td>'+
  '<td><textarea onchange="act(\'/api/persona/update\',{persona_id:'+p.id+',prompt:this.value,style:document.getElementById(\\'st'+p.id+'\\').value})">'+esc(p.prompt)+'</textarea></td>'+
  '<td><input id="st'+p.id+'" value="'+esc(p.style)+'"></td>'+
  '<td><button class="act" onclick="act(\'/api/persona/activate\',{persona_id:'+p.id+'})">激活</button></td></tr>').join('')+'</table>'}
 if(tab==='记忆'){v.innerHTML='<table><tr><th>score</th><th>内容</th><th>对象</th><th>重要/情绪</th></tr>'+
  S.memories.memories.map(m=>'<tr><td>'+(m.score??'')+'</td><td>'+esc(m.content)+'</td><td>'+esc(m.contact_id)+'</td><td>'+m.importance+'/'+(m.emotion??0)+'</td></tr>').join('')+'</table>'}
 if(tab==='画像'){v.innerHTML='<table><tr><th>对象</th><th>好感</th><th>印象</th><th>风格</th></tr>'+
  S.profiles.profiles.map(p=>'<tr><td>'+esc(p.display_name||p.contact_id)+' <small>'+esc(p.channel)+'</small></td><td>'+p.affinity+'</td><td>'+esc(p.impression)+'</td><td>'+esc(p.style_notes)+'</td></tr>').join('')+'</table>'}
 if(tab==='黑白名单'){v.innerHTML='<div>新增：<select id="lt"><option>blacklist</option><option>whitelist</option></select> '+
  '<select id="sc"><option>contact</option><option>group</option><option>global</option></select> '+
  '<select id="ch"><option>*</option><option>qq</option><option>wechat</option></select> '+
  '<input id="tid" placeholder="ID" size="12"> <button class="act" onclick="act(\'/api/rules/add\',{list_type:lt.value,scope:sc.value,channel:ch.value,target_id:tid.value})">添加</button></div>'+
  '<table><tr><th>类型</th><th>渠道</th><th>粒度</th><th>ID</th><th></th></tr>'+
  S.rules.rules.map(r=>'<tr><td>'+esc(r.list_type)+'</td><td>'+esc(r.channel)+'</td><td>'+esc(r.scope)+'</td><td>'+esc(r.target_id)+'</td>'+
  '<td><button class="del" onclick="act(\'/api/rules/remove\',{list_type:\''+r.list_type+'\',scope:\''+r.scope+'\',channel:\''+r.channel+'\',target_id:\''+esc(r.target_id)+'\'})">删除</button></td></tr>').join('')+'</table>'}
 if(tab==='表情包'){v.innerHTML='<table><tr><th>情绪</th><th>标签</th><th>路径</th><th>使用</th></tr>'+
  S.stickers.stickers.map(s=>'<tr><td>'+esc(s.emotion)+'</td><td>'+esc(s.tags)+'</td><td>'+esc(s.file_path)+'</td><td>'+s.use_count+'</td></tr>').join('')+'</table>'}
 if(tab==='记录'){v.innerHTML='<div>搜索：<input id="qk" placeholder="关键词/昵称" size="14">'+
  '<input id="qc" placeholder="联系人ID" size="10"> <button class="act" onclick="loadMsgs()">查找</button> '+
  '<small>收发记录永久保留，可追溯；导出用 export_transcript</small></div>'+
  '<table><tr><th>时间</th><th>向</th><th>渠道</th><th>对象</th><th>内容</th></tr><tbody id="msgbox"></tbody></table>'+
  '<h3>接力笔记</h3><table><tr><th>时间</th><th>标题 / 预览</th></tr>'+
  S.notes.notes.map(n=>'<tr><td>'+esc(n.created_at)+'</td><td><b>'+esc(n.title)+'</b><br>'+esc(n.preview)+'</td></tr>').join('')+'</table>';
  loadMsgs()}
 if(tab==='统计'){const w=S.stats.week;v.innerHTML='<div class="kpi">'+
  '<div>7天收<b>'+w['in']+'</b></div><div>7天发<b>'+w['out']+'</b></div><div>回复率<b>'+w['reply_rate']+'</b></div>'+
  '<div>主动<b>'+w['proactive']+'</b></div><div>心情<b>'+S.stats.mood+'</b></div></div>'+
  '<h3>好感 TOP</h3><table><tr><th>对象</th><th>好感</th></tr>'+
  S.stats.top_profiles.map(p=>'<tr><td>'+esc(p.display_name||p.contact_id)+'</td><td>'+p.affinity+'</td></tr>').join('')+'</table>'}
}
nav();load();setInterval(load,10000);
</script></body></html>
"""


class WebUIHandler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = WEBUI_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json({"personas": t_manage_persona(action="list"),
                        "profiles": t_manage_profile(action="list"),
                        "rules": t_manage_rules(action="list"),
                        "stickers": t_manage_stickers(action="list"),
                        "memories": t_query_memory(limit=50),
                        "stats": t_get_stats(),
                        "notes": t_session_note(action="list", limit=10),
                        "status": t_bot_status()})
        elif self.path.startswith("/api/messages"):
            qs = parse_qs(urlsplit(self.path).query)

            def q1(key, default=""):
                return (qs.get(key) or [default])[0]
            try:
                lim = int(q1("limit", "100") or 100)
            except ValueError:
                lim = 100
            self._json(t_search_messages(keyword=q1("keyword"), channel=q1("channel"),
                                         contact_id=q1("contact_id"), chat_id=q1("chat_id"),
                                         since=q1("since"), until=q1("until"), limit=lim))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = read_body(self)
            payload = json.loads(body or b"{}")
            if self.path == "/api/persona/activate":
                self._json(t_manage_persona(action="set_active", persona_id=payload.get("persona_id")))
            elif self.path == "/api/persona/update":
                self._json(t_manage_persona(action="update", persona_id=payload.get("persona_id"),
                                            prompt=payload.get("prompt"), style=payload.get("style")))
            elif self.path == "/api/rules/add":
                self._json(t_manage_rules(action="add", list_type=payload.get("list_type"),
                                          scope=payload.get("scope", "contact"),
                                          channel=payload.get("channel", "*"),
                                          target_id=payload.get("target_id"), note=payload.get("note", "")))
            elif self.path == "/api/rules/remove":
                self._json(t_manage_rules(action="remove", list_type=payload.get("list_type"),
                                          scope=payload.get("scope"), channel=payload.get("channel"),
                                          target_id=payload.get("target_id")))
            elif self.path == "/api/mood":
                from .persona import t_manage_mood
                self._json(t_manage_mood(action="set", mood=payload.get("mood", 0),
                                         reason=payload.get("reason", "")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def log_message(self, fmt, *args):
        pass


def main_webui(port):
    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), WebUIHandler)
    print(f"[glm-foxgirl] WebUI: http://127.0.0.1:{port}/ （仅本机可访问，Ctrl+C 退出）", file=__import__("sys").stderr)
    srv.serve_forever()
