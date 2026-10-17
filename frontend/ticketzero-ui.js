(function(){
'use strict';
// Live investigation UI: starts runs on the backend and renders their progress into the existing page.
const root=document.getElementById('ticketzero');if(!root)return;
const el=id=>document.getElementById(id),api=globalThis.TicketZeroAPI,M=globalThis.TicketZeroModel,esc=M.esc;
const icon=n=>TICKETZERO_ICONS[n]||TICKETZERO_ICONS['circle'];
const POLL_MS=1500;
// The two example buttons map to backend demo scenarios (each brings its own project folder).
const EXAMPLES={recovery:'multi_bug_chat_service',escalation:'voice_audio_unsupported'};
let scenarios={},sample=null,live=null,proofTrigger=null,health=null;

function announce(text){el('announcement').textContent=text;}
function formError(text){el('form-error').textContent=text;el('form-error').hidden=!text;}
// Last folder name; demo folders are all called "repo", so include their scenario folder too.
const baseName=p=>{const parts=String(p||'').split('/').filter(Boolean);const last=parts.pop()||'';return last==='repo'&&parts.length?parts.pop()+'/'+last:last;};

// ---------------------------------------------------------------- startup
async function loadHealth(){
 try{health=await api.health();}catch{health=null;}
 const label=root.querySelector('.demo-label');
 label.innerHTML=icon('circle-fill')+' '+(!health?'Backend offline':health.ok?'Live':'Models unavailable');
 const services='Crusoe'+(health?.ydc_key_present?' + You.com':'');
 root.querySelectorAll('.intake-footer,.case-footer').forEach(f=>{const s=f.querySelectorAll(':scope>span');const withIcons=f.classList.contains('case-footer');
  if(s[0])s[0].innerHTML=icon('info')+' '+(health?'Live runs · your folder changes only if you allow edits':'Backend offline · start it to run investigations');
  if(s[1])s[1].innerHTML=(withIcons?icon('plugs')+' ':'')+services+(health?.ok?' · Connected':' · Not connected');});
 if(health&&!health.ok&&health.error)formError('The backend can’t reach its models: '+health.error);
}
async function loadScenarios(){
 let list;try{list=await api.scenarios();scenarios=Object.fromEntries(list.map(s=>[s.name,s]));}catch{return;}
 root.querySelectorAll('[data-example]').forEach(b=>{const s=scenarios[EXAMPLES[b.dataset.example]];if(!s)return;b.querySelector('strong').textContent=s.label;b.querySelector('small').textContent=s.hint;});
 // Benchmark tickets (T01..T10) in the collapsible list, styled like the featured examples.
 const tickets=list.filter(s=>/^T\d\d_/.test(s.name));
 el('more-samples').hidden=!tickets.length;el('more-samples-count').textContent=tickets.length+' tickets';
 el('sample-list').innerHTML=tickets.map(s=>'<button type="button" data-scenario="'+esc(s.name)+'"><span class="sample-icon">'+icon(s.expected_outcome==='ESCALATED'?'clock':'code')+'</span><span><strong><span class="ticket-id">'+esc(s.name.slice(0,3))+'</span>'+esc(s.label)+'</strong><small>'+esc(s.hint)+'</small></span>'+icon('arrow-up-right')+'</button>').join('');
}
function fillSample(key){fillScenario(EXAMPLES[key]);}
function fillScenario(name){
 const s=scenarios[name];if(!s){formError('Examples need the backend. Start it, then reload this page.');return;}
 el('issue').value=s.ticket.description;el('context').value=s.ticket.logs||'';el('expected').value=s.ticket.expected_behavior||'';
 sample={name:s.name,text:s.ticket.description.trim()};formError('');el('form-hint').textContent='Example loaded. It runs on its own demo folder.';el('issue').scrollIntoView({block:'center'});el('issue').focus();
}

// ---------------------------------------------------------------- starting and following a run
async function start(){
 const problem=el('issue').value.trim();
 if(problem.replace(/\s+/g,' ').length<20){formError('Describe the issue in at least 20 characters.');return;}
 const ticket={description:problem,logs:el('context').value.trim(),expected_behavior:el('expected').value.trim()};
 const useExample=!!sample&&problem===sample.text,folder=globalThis.TicketZeroFolder?.current();
 if(!useExample&&!folder){formError('Choose your project folder so the agents can read it and run its tests, or try an example below.');return;}
 const submit=el('issue-form').querySelector('button[type=submit]');submit.disabled=true;formError('');
 const replay=useExample?{scenario:sample.name}:{folder:folder.path,ticket};
 try{const res=await launch(replay);openRun(res.run_id,{allowEdits:!useExample&&folder.allowEdits,replay});}
 catch(e){formError(e.message);}
 finally{submit.disabled=false;}
}
const launch=r=>r.scenario?api.startScenario(r.scenario):api.startFolder(r.folder,r.ticket);

function openRun(id,opts={}){
 stop();live={id,run:null,tree:[],events:[],lastEventId:0,timer:null,selected:null,flowKey:'',allowEdits:!!opts.allowEdits,replay:opts.replay||null,apply:null,stopping:false,lastStatus:''};
 history.replaceState(null,'','#run='+encodeURIComponent(id));
 root.dataset.view='run';el('intake').hidden=true;el('investigation').hidden=false;
 el('case-title').textContent='Starting the investigation…';el('case-status').textContent='Starting';el('timeline').innerHTML='';
 el('work-content').innerHTML='';el('outcome').hidden=true;el('active-work').hidden=false;
 el('response-strip').hidden=true;el('review-actions').hidden=true;el('next-step').hidden=true;
 root.querySelector('.your-issue').open=false;setJourney(1);poll();
}
function stop(){if(live&&live.timer){clearTimeout(live.timer);live.timer=null;}}
async function poll(){
 const cur=live;if(!cur)return;
 try{
  const [run,tree,events]=await Promise.all([api.run(cur.id),api.tree(cur.id),api.events(cur.id,cur.lastEventId)]);
  if(cur!==live)return;
  cur.run=run;cur.tree=tree;if(events.length){cur.events=cur.events.concat(events);cur.lastEventId=events[events.length-1].id;}
  render();
  if(run.status==='running')cur.timer=setTimeout(poll,POLL_MS);else await finish(cur);
 }catch(e){
  if(cur!==live)return;
  if(e.status===404){reset();formError('That run no longer exists.');return;}
  el('playback-note').textContent='Reconnecting to the backend…';cur.timer=setTimeout(poll,POLL_MS*2);
 }
}
// Write a verified fix back to the user's folder, only when they allowed edits before starting.
async function finish(cur){
 const r=cur.run;
 if(r.status!=='verified'||!cur.allowEdits||r.read_only||r.applied_at||cur.apply)return;
 cur.apply={state:'applying'};render();
 try{const res=await api.apply(r.id);cur.apply={state:'applied',files:res.files};}
 catch(e){cur.apply={state:'error',message:e.message};}
 if(cur===live)render();
}
function reset(){stop();live=null;history.replaceState(null,'',location.pathname+location.search);root.dataset.view='intake';el('intake').hidden=false;el('investigation').hidden=true;setJourney(0);el('issue').focus();}

// ---------------------------------------------------------------- rendering
function setJourney(stage){root.querySelectorAll('[data-stage]').forEach(n=>{const i=+n.dataset.stage,done=stage>=4||i<stage;n.className=done?'done':i===stage?'active':'';n.querySelector('.stage-icon').innerHTML=icon(done?'check-circle-fill':i===stage?'circle-dot':'circle');if(i===stage&&stage<4)n.setAttribute('aria-current','step');else n.removeAttribute('aria-current');});}
function flowBlock(cur){return '<div class="work-heading">'+icon(cur.run.status==='running'?'circle-notch':'check-circle-fill')+'<div><h3>How the work is divided</h3><p>Each box is one agent working on part of the problem. Parents wait for every child, merge their fixes and test again. Select a box to see its evidence.</p></div></div>'+M.renderFlow(cur.tree,cur.selected);}
// Re-render the flowchart only when it changed, keeping the user's scroll position.
function renderFlowInto(container,cur){
 const key=JSON.stringify(cur.tree)+'|'+cur.selected+'|'+cur.run.status;if(container.dataset.flowKey===key)return;
 const old=container.querySelector('.flow-scroll'),scroll=old?[old.scrollLeft,old.scrollTop]:null;
 container.innerHTML=flowBlock(cur);container.dataset.flowKey=key;
 const fresh=container.querySelector('.flow-scroll');if(fresh&&scroll){fresh.scrollLeft=scroll[0];fresh.scrollTop=scroll[1];}
}
function render(){
 const cur=live;if(!cur||!cur.run)return;const r=cur.run,rootNode=cur.tree[0],names=M.titles(cur.tree);
 const stage=M.stage(r,cur.events,rootNode?.status),status=M.runStatus(r,stage),terminal=r.status!=='running';
 setJourney(stage);
 el('case-code').innerHTML='Investigation <span>/</span> '+esc(r.id);
 el('case-title').textContent=r.ticket.title||'Your issue';
 el('case-status').textContent=status;el('case-status').className='case-status'+(['escalated','failed','cancelled'].includes(r.status)?' attention':'');
 el('issue-excerpt').textContent=r.ticket.description||'';el('submitted-problem').textContent=r.ticket.description||'';
 el('submitted-context').textContent=r.ticket.logs||'';el('submitted-context').hidden=!r.ticket.logs;
 el('submitted-expected').textContent=r.ticket.expected_behavior?'Expected: '+r.ticket.expected_behavior:'';
 el('run-folder').hidden=!r.source_dir;el('run-folder-name').textContent=baseName(r.source_dir);
 const folderNote=el('run-folder').querySelector('small');if(folderNote)folderNote.textContent=(r.read_only?'Demo folder · read-only':'Your folder')+' · agents work on a sandbox copy';
 el('active-work').hidden=terminal;el('outcome').hidden=!terminal;
 const ms=M.milestones(cur.events,names,rootNode?.id),latest=ms[ms.length-1];
 el('current-title').textContent=latest?latest.title:'Reading the issue';
 el('current-description').textContent=latest?latest.text:'Freezing the acceptance tests and planning the investigation.';
 if(!terminal)renderFlowInto(el('work-content'),cur);
 renderVerification(r);renderTimeline(cur,ms);
 el('pause').textContent=cur.stopping?'Stopping…':'Stop run';el('pause').disabled=cur.stopping;el('playback-note').textContent=M.usageLine(r);
 if(terminal)renderOutcome(cur);
 if(status!==cur.lastStatus){cur.lastStatus=status;announce((r.ticket.title||'Investigation')+'. '+status+'.');}
}
function renderVerification(r){
 const box=root.querySelector('.verification-reminder'),n=M.testCount(r);
 const passed=r.status==='verified',failed=['escalated','failed'].includes(r.status);
 box.firstElementChild.outerHTML=icon(passed?'check-circle-fill':failed?'warning-circle':'circle');
 el('verification-label').textContent=passed?'All acceptance tests passed':failed?'Acceptance tests not met':'Acceptance tests: not verified yet';
 el('verification-description').textContent=passed?'Every frozen test passes on the final code, and an independent critic reviewed the change.'
  :n?n+' frozen test'+(n===1?'':'s')+' must pass on the final code before this is resolved.':'The acceptance tests are being frozen and mapped to your goal.';
}
function renderTimeline(cur,ms){
 const r=cur.run,visible=ms.slice(-4),terminal=r.status!=='running';
 el('timeline').innerHTML=visible.map((m,i)=>{const current=i===visible.length-1&&!terminal;
  return '<li class="'+(m.failed?'failed':current?'active':'')+'"><span class="milestone-icon">'+icon(m.failed?'warning-circle-fill':current?'circle-dot':'check-circle-fill')+'</span><button class="step-link" data-event="'+m.id+'"><strong>'+esc(m.title)+'</strong></button><small>'+esc(m.text)+'</small><time>'+M.elapsed(r.created_at,m.ts)+' elapsed</time></li>';}).join('');
 el('all-steps').textContent='View all '+cur.events.length+' events';
}
function checkRows(r){
 const rows=(r.criteria||[]).map(c=>{const s=c.status||'pending',good=s==='pass';
  return '<div class="test-row '+(good?'good':'')+'">'+icon(good?'check-circle-fill':s==='fail'?'warning-circle':'circle')+'<div class="test-detail">'+esc(c.text)+'<small>'+esc((c.test_ids||[]).join(', '))+'</small></div><span>'+esc(good?'Passed':s==='fail'?'Failed':'Not run')+'</span></div>';});
 return rows.length?'<div class="work-list">'+rows.join('')+'</div>':'';
}
function renderOutcome(cur){
 const r=cur.run,box=el('outcome');box.dataset.outcome=r.status==='verified'?'verified':'escalated';
 const titles={verified:'The tests pass. The fix is verified.',escalated:'This one needs a human look.',failed:'The run stopped with an error.',cancelled:'The run was stopped.'};
 let note;
 if(r.status==='verified'){
  const a=cur.apply;
  note=r.read_only?'<strong>Demo folder is read-only.</strong> Nothing was written; the verified patch is in the report.'
   :a?.state==='applying'?'Writing the verified fix to your folder…'
   :a?.state==='applied'?'<strong>Written to '+esc(r.source_dir)+'</strong>: '+esc(a.files.join(', '))
   :a?.state==='error'?'<strong>Not written to your folder.</strong> '+esc(a.message)
   :r.applied_at?'<strong>Written to your folder</strong> at '+esc(r.applied_at)+'.'
   :'<strong>Your folder was not changed.</strong> Allow file edits before starting to have a verified fix written back, or take the patch from the report.';
 }else if(r.status==='escalated'){note='<strong>Handoff prepared.</strong> The report lists every attempt, what was ruled out, and what a human should check next. Nothing in your folder was changed.';}
 else{note='Nothing in your folder was changed.';}
 box.innerHTML='<h2>'+esc(titles[r.status]||'Finished')+'</h2><p>'+esc(r.reason||'')+'</p>'+checkRows(r)
  +'<div class="result-note">'+note+'</div><div class="outcome-flow" id="outcome-flow"></div>'
  +'<div class="outcome-actions"><button class="primary" data-action="report">'+icon('download-simple')+' Export report</button><button class="outline" data-action="replay">Run again</button><button class="text-button" data-action="new">New issue</button></div><p class="download-status" id="download-status" role="status"></p>';
 renderFlowInto(el('outcome-flow'),cur);
}

// ---------------------------------------------------------------- dialogs
function openDialog(title,source,html,trigger){proofTrigger=trigger;el('evidence-title').textContent=title;el('evidence-source').textContent=source;el('evidence-content').innerHTML=html;if(!el('evidence-dialog').open)el('evidence-dialog').showModal();announce(title+' opened.');}
const pre=t=>'<pre>'+esc(t)+'</pre>';
function diffHtml(d){return '<pre>'+String(d).split('\n').map(l=>'<span class="j-patch-line'+(l.startsWith('+')&&!l.startsWith('+++')?' j-patch-add':l.startsWith('-')&&!l.startsWith('---')?' j-patch-remove':'')+'">'+esc(l)+'</span>').join('')+'</pre>';}
async function openNode(id,trigger){
 if(live){live.selected=id;render();}
 openDialog('Loading…','',' ',trigger);
 let n;try{n=await api.node(id);}catch(e){el('evidence-content').innerHTML='<p>'+esc(e.message)+'</p>';return;}
 const h=[];
 if(n.reason)h.push('<p><strong>'+esc(M.statusLabel(n.status))+':</strong> '+esc(n.reason)+'</p>');
 h.push('<h4>Task</h4>'+pre(n.task));
 const plan=n.plan||{};if(plan.kind)h.push('<h4>Plan</h4><p>'+esc(plan.kind==='split'?'Split into '+(plan.subtasks||[]).length+' sub-tasks'+(plan.parallel?', run in parallel':''):'Solve directly')+(plan.rationale?'. '+esc(plan.rationale):'')+'</p>');
 const ev=n.evidence;if(ev&&ev.root_cause){h.push('<h4>Root cause</h4><p>'+esc(ev.root_cause)+'</p>');if((ev.evidence||[]).length)h.push('<ul>'+ev.evidence.map(x=>'<li><strong>'+esc(x.kind)+'</strong> · '+esc(x.ref)+(x.detail?' — '+esc(x.detail):'')+'</li>').join('')+'</ul>');}
 if((n.findings||[]).length)h.push('<h4>Findings</h4>'+n.findings.map(f=>'<p>'+(f.polarity==='negative'?'✗ ':'✓ ')+esc(f.summary)+'</p>').join(''));
 if((n.citations||[]).length)h.push('<h4>Sources</h4><ul>'+n.citations.map(c=>'<li><a href="'+esc(c.url)+'" target="_blank" rel="noreferrer">'+esc(c.title||c.url)+'</a></li>').join('')+'</ul>');
 if(n.patch)h.push('<h4>Change</h4>'+diffHtml(n.patch));
 const p=n.proof;if(p){h.push('<h4>Verification</h4><p>'+esc(p.summary||'')+'</p>');
  const results=Object.entries((p.tests||{}).results||{});if(results.length)h.push('<ul>'+results.map(([t,o])=>'<li>'+esc(o)+' · '+esc(t)+'</li>').join('')+'</ul>');
  if(p.critic)h.push('<p>Independent critic ('+esc(p.critic.model||'')+'): '+esc(p.critic.veto?'vetoed':'no veto')+' — '+esc(p.critic.reason||'')+'</p>');}
 const roles=Object.entries(n.model_roles||{});
 openDialog(n.title,'Depth '+n.depth+' · '+M.statusLabel(n.status)+(roles.length?' · '+roles.map(([r,m])=>r+': '+m.replace(/^nvidia\//,'')).join(', '):''),h.join(''),trigger);
}
function openEvent(id,trigger){
 const e=live?.events.find(x=>x.id===id);if(!e)return;const names=M.titles(live.tree),m=M.milestones([e],names,live.tree[0]?.id)[0];
 let data='';try{data=e.data_json?JSON.stringify(JSON.parse(e.data_json),null,2):'';}catch{data=e.data_json;}
 openDialog(m?m.title:e.type,(names[e.node_id]||'Run')+' · '+e.type+' · '+new Date(e.ts).toLocaleTimeString(),'<p>'+esc(e.message)+'</p>'+(data&&data!=='{}'?pre(data.slice(0,6000)):''),trigger);
}
function openGoal(trigger){
 const r=live?.run;if(!r)return;
 openDialog('Original acceptance criteria','Defined by the planner · checked by frozen tests',(r.criteria||[]).length?'<p>Every criterion maps to frozen tests that no agent can edit. The issue is resolved only when all of them pass on the final code.</p>'+checkRows(r):'<p>The planner is still mapping the frozen tests to your goal.</p>',trigger);
}
function showHistory(trigger){
 const cur=live;if(!cur)return;const names=M.titles(cur.tree);
 openDialog('Every step, in order',cur.events.length+' events from the live run','<ol class="history-list">'+cur.events.slice(-400).map(e=>'<li><strong>'+esc(e.type.replace(/_/g,' '))+'</strong><p>'+esc(e.message)+'</p><small class="muted">'+esc(names[e.node_id]||'Run')+' · '+M.elapsed(cur.run.created_at,e.ts)+'</small></li>').join('')+'</ol>',trigger);
}
async function download(){
 const status=el('download-status');
 try{const text=await api.report(live.id);const url=URL.createObjectURL(new Blob([text],{type:'text/markdown'})),a=document.createElement('a');a.href=url;a.download='ticketzero-'+live.id+'-report.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='Report download started.';}
 catch(e){status.textContent='Could not export the report: '+e.message;}
}
async function replay(){
 const r=live?.run;if(!r)return;
 const again=live.replay||(r.read_only?{scenario:r.scenario}:{folder:r.source_dir,ticket:{description:r.ticket.description||'',logs:r.ticket.logs||'',expected_behavior:r.ticket.expected_behavior||''}});
 try{const res=await launch(again);openRun(res.run_id,{allowEdits:live.allowEdits,replay:again});}
 catch(e){el('download-status').textContent=e.message;}
}
async function stopRun(){if(!live||live.stopping||live.run?.status!=='running')return;live.stopping=true;render();try{await api.cancel(live.id);}catch{live.stopping=false;render();}}

// ---------------------------------------------------------------- events
el('issue-form').addEventListener('submit',e=>{e.preventDefault();start();});
el('issue').addEventListener('input',()=>{el('form-hint').textContent='Leave out API keys and credentials.';formError('');});
root.addEventListener('click',event=>{
 const b=event.target.closest('button');if(!b)return;
 if(b.dataset.example){fillSample(b.dataset.example);return;}
 if(b.dataset.scenario){fillScenario(b.dataset.scenario);return;}
 if(b.dataset.node){openNode(b.dataset.node,b);return;}
 if(b.dataset.event){openEvent(+b.dataset.event,b);return;}
 if(b.dataset.proof==='goal'){openGoal(b);return;}
 if(b.id==='run-folder'&&live?.run){globalThis.TicketZeroFolder?.openFiles(live.run.source_dir,b);return;}
 const action=b.dataset.action;
 if(action==='new'){if(el('evidence-dialog').open)el('evidence-dialog').close();reset();}
 else if(action==='pause')stopRun();
 else if(action==='history')showHistory(b);
 else if(action==='close-evidence')el('evidence-dialog').close();
 else if(action==='report'&&live)download();
 else if(action==='replay')replay();
});
el('evidence-dialog').addEventListener('close',()=>{if(proofTrigger&&proofTrigger.isConnected)proofTrigger.focus();});
el('evidence-dialog').addEventListener('click',e=>{if(e.target===el('evidence-dialog')){const r=el('evidence-dialog').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)el('evidence-dialog').close();}});
root.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&!live){e.preventDefault();start();}});

loadHealth();loadScenarios();
const fromHash=/run=([\w-]+)/.exec(location.hash);if(fromHash)openRun(decodeURIComponent(fromHash[1]));
})();
