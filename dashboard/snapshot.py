#!/usr/bin/env python3
"""Turn the dashboard's status.json into a phone page plus the data that makes it live.

python snapshot.py --out page.html --data-out data.json [--status status.json | --project <repo>] [--title "Project status"]

--out        one self-contained HTML page (no external files). It renders the snapshot embedded in it, and when its
             `db` capability is granted it subscribes to the doc status/current and redraws as that doc changes.
--data-out   the same snapshot as the body of the doc status/current (write it with ArtifactData update, file_path).
Local paths, URL credentials and key-like strings are removed from every string. Cards open to show phases,
agents, what each is doing and its last words.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status as _status  # noqa: E402

PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:Users|home|tmp|var|private)/)[^\s\"')]*")
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9_-]{32,})")
URL_CRED_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


def clean(value, n=160):
    s = "" if value is None else str(value)
    s = URL_CRED_RE.sub(r"\1", s)
    s = PATH_RE.sub("(path)", s)
    s = SECRET_RE.sub("(removed)", s)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build(snap, title):
    runs_in = snap.get("runs") or []
    live = [r for r in runs_in if r.get("state") == "running"]
    rest = sorted((r for r in runs_in if r.get("state") != "running"), key=lambda r: r.get("end") or 0, reverse=True)[:12]
    runs = []
    for r in live + rest:
        eta = r.get("eta") if isinstance(r.get("eta"), dict) else {}
        agents = []
        for a in (r.get("agents") or [])[:8]:
            agents.append({
                "label": clean(a.get("label"), 60), "phase": clean(a.get("phase"), 30), "model": clean(a.get("model"), 16),
                "state": clean(a.get("state"), 12), "now": clean(a.get("now"), 60), "said": clean(a.get("said"), 300),
                "tokens": int(num(a.get("outTokens"))), "started": int(num(a.get("started"))), "activity": int(num(a.get("activity"))),
            })
        runs.append({
            "id": clean(r.get("id"), 24), "lane": clean(r.get("lane") or r.get("branch") or r.get("name"), 40),
            "title": clean(r.get("title"), 160), "state": clean(r.get("state"), 12), "start": int(num(r.get("start"))),
            "end": int(num(r.get("end"))), "activity": int(num(r.get("activity"))),
            "etaMs": int(num(eta.get("remainingMs"))) if eta.get("known") else 0, "overdue": bool(eta.get("overdue")),
            "phases": [{"title": clean(p.get("title"), 30), "state": clean(p.get("state"), 12), "count": int(num(p.get("count")))} for p in (r.get("phases") or [])],
            "agents": agents, "error": clean(r.get("error"), 200), "headline": clean(r.get("headline"), 300),
            "logs": [clean(x, 160) for x in (r.get("logs") or [])[-5:]],
        })
    usage = [{"label": clean(w.get("label"), 40), "percent": num(w.get("percent")), "resets": clean(w.get("resets"), 40)}
             for w in ((snap.get("usage") or {}).get("windows") or [])]
    burn = snap.get("burn") or {}
    return {
        "title": clean(title, 60), "generated": int(time.time() * 1000), "usage": usage,
        "plan": [{"content": clean(i.get("content"), 140), "status": clean(i.get("status"), 14)} for i in ((snap.get("plan") or {}).get("items") or [])],
        "attention": [{"sev": clean(a.get("sev"), 6), "lane": clean(a.get("lane"), 40), "text": clean(a.get("text"), 200), "kind": clean(a.get("kind"), 20)} for a in (snap.get("attention") or [])[:15]],
        "runs": runs,
        "ready": [{"lane": clean(r.get("lane"), 40), "title": clean(r.get("title"), 140), "branch": clean(r.get("branch"), 40), "ahead": r.get("ahead"),
                   "behind": r.get("behind"), "shipOk": r.get("shipOk"), "pushed": r.get("pushed")} for r in (snap.get("ready") or [])[:15]],
        "studio": snap.get("studio") or {},
        "burn": {"liveAgents": int(num(burn.get("liveAgents"))), "families": burn.get("byFamily5h") or {}},
    }


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#5f6672;--line:#e1e4ea;--ok:#1f8f4d;--run:#2563eb;--warn:#b45309;--bad:#c0262d}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--card:#171a20;--ink:#eceef2;--mute:#9aa2af;--line:#272b33;--ok:#4cc380;--run:#6ea0ff;--warn:#f0a850;--bad:#ff7b80}}
:root[data-theme="dark"]{--bg:#0f1115;--card:#171a20;--ink:#eceef2;--mute:#9aa2af;--line:#272b33;--ok:#4cc380;--run:#6ea0ff;--warn:#f0a850;--bad:#ff7b80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px;max-width:760px;margin-inline:auto}
h1{font-size:20px;margin:0 0 2px}h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:22px 0 8px}
.sub{color:var(--mute);font-size:12.5px;overflow-wrap:anywhere}.row{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mute);margin-right:6px}.live.on{background:var(--ok)}.live.stale{background:var(--warn)}
.card,.box,.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}
details.card{padding:0}details.card>summary{list-style:none;cursor:pointer;padding:10px 12px;min-height:44px}details.card>summary::-webkit-details-marker{display:none}
details.card[open]>summary{border-bottom:1px solid var(--line)}.body{padding:10px 12px}
.title{margin:2px 0 6px;overflow-wrap:anywhere}.state{font-size:12px;color:var(--mute);white-space:nowrap}
.card.running{border-left:4px solid var(--run)}.card.completed{border-left:4px solid var(--ok)}.card.killed,.card.failed{border-left:4px solid var(--bad)}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mute)}
.chip.running{color:var(--run);border-color:var(--run)}.chip.completed,.chip.done{color:var(--ok);border-color:var(--ok)}
.track{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin:6px 0 4px}.fill{height:100%;background:var(--ok)}.fill.mid{background:var(--warn)}.fill.hi{background:var(--bad)}
.item.high{border-left:4px solid var(--bad)}.item.med{border-left:4px solid var(--warn)}
.step{display:flex;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)}.step:last-child{border:0}
.dot{flex:none;width:10px;height:10px;border-radius:50%;margin-top:5px;border:2px solid var(--mute)}.dot.in_progress{border-color:var(--run);background:var(--run)}.dot.completed{border-color:var(--ok);background:var(--ok)}
.agent{padding:8px 0;border-top:1px solid var(--line)}.agent:first-child{border:0;padding-top:0}.said{margin-top:4px;font-size:13.5px;overflow-wrap:anywhere}
.k{color:var(--mute);font-size:12px;margin:10px 0 4px;text-transform:uppercase;letter-spacing:.05em}
"""

JS = r"""
(function(){
var seedEl=document.getElementById('seed'),snap=JSON.parse(seedEl.textContent),root=document.getElementById('app'),liveEl=null;
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function ago(ms){if(!ms)return'';var s=Math.max(0,Math.round((Date.now()-ms)/1000));if(s<90)return s+'s ago';if(s<5400)return Math.round(s/60)+'m ago';return(s/3600).toFixed(1)+'h ago'}
function dur(ms){if(!ms)return'';var m=Math.round(ms/60000);return m<90?m+' min':(m/60).toFixed(1)+' h'}
function key(prefix,v){return prefix+':'+v}
function bar(w){var p=Math.max(0,Math.min(100,w.percent||0)),b=el('div','box'),r=el('div','row');r.appendChild(el('span',null,w.label));r.appendChild(el('b',null,Math.round(p)+'%'));b.appendChild(r);
 var t=el('div','track'),f=el('div','fill '+(p>=85?'hi':p>=70?'mid':''));f.style.width=p+'%';t.appendChild(f);b.appendChild(t);b.appendChild(el('div','sub','resets '+w.resets));return b}
function runCard(r){var d=el('details','card '+r.state);d.dataset.key=key('run',r.id);var s=el('summary'),row=el('div','row');row.appendChild(el('b',null,r.lane));row.appendChild(el('span','state',r.state));s.appendChild(row);
 s.appendChild(el('div','title',r.title));var ch=el('div','chips');r.phases.forEach(function(p){ch.appendChild(el('span','chip '+p.state,p.title))});s.appendChild(ch);
 var meta=[];var live=r.agents.filter(function(a){return a.state==='running'}).length;if(live)meta.push(live+' agent'+(live>1?'s':'')+' running');if(r.etaMs)meta.push((r.overdue?'past usual: ':'about ')+dur(r.etaMs)+' left');meta.push(ago(r.activity||r.end));
 s.appendChild(el('div','sub',meta.filter(Boolean).join(' · ')));d.appendChild(s);
 var b=el('div','body');
 if(r.headline){b.appendChild(el('div','k','Result'));b.appendChild(el('div',null,r.headline))}
 if(r.error){b.appendChild(el('div','k','Error'));b.appendChild(el('div',null,r.error))}
 if(r.agents.length){b.appendChild(el('div','k','Agents'));r.agents.forEach(function(a){var g=el('div','agent'),h=el('div','row');h.appendChild(el('b',null,a.label));h.appendChild(el('span','state',a.model+' · '+a.state));g.appendChild(h);
  var sub=[a.phase];if(a.now&&a.state==='running')sub.push('using '+a.now);if(a.tokens)sub.push(Math.round(a.tokens/1000)+'k tokens');sub.push(ago(a.activity));g.appendChild(el('div','sub',sub.filter(Boolean).join(' · ')));
  if(a.said)g.appendChild(el('div','said',a.said));b.appendChild(g)})}
 if(r.logs.length){b.appendChild(el('div','k','Notes'));r.logs.forEach(function(l){b.appendChild(el('div','sub',l))})}
 b.appendChild(el('div','sub','run '+r.id));d.appendChild(b);return d}
function render(){
 var open={};root.querySelectorAll('details[open]').forEach(function(d){open[d.dataset.key]=1});
 var y=window.scrollY;root.textContent='';
 root.appendChild(el('h1',null,snap.title));
 var u=el('div','sub');liveEl=el('span','live');u.appendChild(liveEl);u.appendChild(document.createTextNode(''));var t=el('span');t.id='age';u.appendChild(t);root.appendChild(u);
 if(snap.usage.length){root.appendChild(el('h2',null,'Usage'));snap.usage.forEach(function(w){root.appendChild(bar(w))})}
 if(snap.plan.length){root.appendChild(el('h2',null,'Task list'));var bx=el('div','box');snap.plan.forEach(function(i){var s=el('div','step');s.appendChild(el('span','dot '+i.status));s.appendChild(el('span',null,i.content));bx.appendChild(s)});root.appendChild(bx)}
 if(snap.attention.length){root.appendChild(el('h2',null,'Needs you'));snap.attention.forEach(function(a){var i=el('div','item '+a.sev),b=el('b',null,a.lane+' ');i.appendChild(b);i.appendChild(document.createTextNode(a.text));root.appendChild(i)})}
 var live=snap.runs.filter(function(r){return r.state==='running'}),rest=snap.runs.filter(function(r){return r.state!=='running'});
 if(live.length){root.appendChild(el('h2',null,'Running now ('+live.length+')'));live.forEach(function(r){root.appendChild(runCard(r))})}
 if(rest.length){root.appendChild(el('h2',null,'Recently finished'));rest.forEach(function(r){root.appendChild(runCard(r))})}
 if(snap.ready.length){root.appendChild(el('h2',null,'Ready to merge'));snap.ready.forEach(function(r){var d=el('details','card');d.dataset.key=key('ready',r.lane);var s=el('summary'),row=el('div','row');row.appendChild(el('b',null,r.lane));
  row.appendChild(el('span','state',r.shipOk===true?'ship OK':r.shipOk===false?'not OK':'unchecked'));s.appendChild(row);s.appendChild(el('div','sub',r.title));d.appendChild(s);
  var b=el('div','body');b.appendChild(el('div',null,'Branch '+r.branch+' · '+(r.ahead==null?'?':r.ahead)+' ahead, '+(r.behind==null?'?':r.behind)+' behind · '+(r.pushed?'pushed':'not pushed')));d.appendChild(b);root.appendChild(d)})}
 if(snap.studio&&snap.studio.limit!=null){root.appendChild(el('h2',null,'Studio'));root.appendChild(el('div','box',snap.studio.count+' of '+snap.studio.limit+' windows open'))}
 if(snap.burn&&snap.burn.liveAgents){root.appendChild(el('h2',null,'Agents'));root.appendChild(el('div','box',snap.burn.liveAgents+' running now'))}
 root.querySelectorAll('details').forEach(function(d){if(open[d.dataset.key])d.open=true});window.scrollTo(0,y);tick()}
function tick(){var t=document.getElementById('age');if(!t||!liveEl)return;var age=Date.now()-snap.generated;t.textContent='Updated '+ago(snap.generated)+(dbOn?' · live':' · snapshot, reload for newer');
 liveEl.className='live '+(age>15*60000?'stale':dbOn?'on':'')}
var dbOn=false;render();setInterval(tick,15000);
if(window.claude&&window.claude.use){window.claude.use('db').then(function(db){if(!db)return;var timer=setTimeout(function(){},10000);
 db.doc('status/current').onSnapshot(function(s){clearTimeout(timer);var d=s.exists&&s.data();if(d&&d.generated&&d.generated>=snap.generated){snap=d;dbOn=true;render()}else if(d){dbOn=true;tick()}},function(){})}).catch(function(){})}
})();
"""


def render_page(data):
    seed = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body><div id=\"app\"></div>"
            '<script id="seed" type="application/json">%s</script><script>%s</script></body></html>') % (
        data["title"].replace("<", "&lt;"), CSS, seed, JS)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-out", default="")
    ap.add_argument("--status", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--title", default="Project status")
    opts = ap.parse_args()
    if opts.status:
        path = Path(opts.status)
    else:
        project = Path(opts.project or ".").resolve()
        path = Path(tempfile.gettempdir()) / "multi-agent-dashboard" / _status.project_slug(project) / "status.json"
    data = build(json.loads(path.read_text(encoding="utf-8")), opts.title)
    Path(opts.out).write_text(render_page(data), encoding="utf-8")
    if opts.data_out:
        Path(opts.data_out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print("wrote", opts.out, "and" if opts.data_out else "", opts.data_out)


if __name__ == "__main__":
    main()
