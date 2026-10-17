(function(scope){
'use strict';
// Pure helpers that turn backend run data into what the page shows: the divide-and-conquer flowchart, the
// activity milestones, the journey stage and status text. No DOM access, so qa/ tests run them in Node.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ACTIVE=new Set(['planning','waiting_children','investigating','fixing','verifying','running']);
const STATUS_LABEL={pending:'Queued',planning:'Planning',waiting_children:'Waiting on sub-tasks',investigating:'Investigating',fixing:'Writing a fix',verifying:'Running tests',verified:'Verified',escalated:'Escalated',failed:'Failed',cancelled:'Stopped'};
function tone(status){if(ACTIVE.has(status))return 'running';return ['verified','escalated','failed','cancelled'].includes(status)?status:'pending';}
function statusLabel(status){return STATUS_LABEL[status]||String(status||'');}
function flatten(tree){const out=[];const walk=n=>{out.push(n);(n.children||[]).forEach(walk);};(tree||[]).forEach(walk);return out;}
function titles(tree){return Object.fromEntries(flatten(tree).map(n=>[n.id,n.title]));}

// Tidy top-down layout: leaves take consecutive slots, every parent is centred over its children.
function layout(tree,opt={}){
 const W=opt.width||214,H=opt.height||70,GX=opt.gapX||22,GY=opt.gapY||56;let slot=0;const nodes=[],edges=[];
 function place(n,depth){const kids=n.children||[];let x;
  if(!kids.length){x=slot*(W+GX);slot++;}
  else{const xs=kids.map(c=>place(c,depth+1));x=(xs[0]+xs[xs.length-1])/2;kids.forEach(c=>edges.push({from:n.id,to:c.id,tone:tone(c.status)}));}
  nodes.push({node:n,x,y:depth*(H+GY)});return x;}
 (tree||[]).forEach(r=>place(r,0));
 const width=nodes.length?Math.max(...nodes.map(p=>p.x))+W:0,height=nodes.length?Math.max(...nodes.map(p=>p.y))+H:0;
 return {nodes,edges,width,height,W,H};
}

// The flowchart as HTML: an SVG layer for the edges and one button per agent (node). Every text is escaped.
function renderFlow(tree,selected){
 const L=layout(tree);if(!L.nodes.length)return '<p class="flow-empty">Waiting for the planner to read the issue…</p>';
 const pad=14,w=L.width+pad*2,h=L.height+pad*2,pos=new Map(L.nodes.map(p=>[p.node.id,p]));
 const paths=L.edges.map(e=>{const a=pos.get(e.from),b=pos.get(e.to);const x1=a.x+L.W/2+pad,y1=a.y+L.H+pad,x2=b.x+L.W/2+pad,y2=b.y+pad,my=(y1+y2)/2;
  return '<path class="flow-edge '+e.tone+'" d="M'+x1+' '+y1+' C'+x1+' '+my+' '+x2+' '+my+' '+x2+' '+y2+'"/>';}).join('');
 const boxes=L.nodes.map(p=>{const n=p.node,t=tone(n.status);
  return '<button type="button" class="flow-node '+t+(n.id===selected?' selected':'')+'" data-node="'+esc(n.id)+'" style="left:'+(p.x+pad)+'px;top:'+(p.y+pad)+'px;width:'+L.W+'px;height:'+L.H+'px"'+(n.reason?' title="'+esc(n.reason)+'"':'')+'>'
  +'<span class="flow-title">'+esc(n.title)+'</span><span class="flow-meta"><i class="flow-dot"></i>'+esc(statusLabel(n.status))+' · depth '+esc(n.depth)+(n.replans?' · replans '+esc(n.replans):'')+'</span></button>';}).join('');
 return '<div class="flow-scroll"><div class="flow" style="width:'+w+'px;height:'+h+'px"><svg class="flow-edges" width="'+w+'" height="'+h+'" aria-hidden="true">'+paths+'</svg>'+boxes+'</div></div>'
  +'<div class="flow-legend"><span><i class="flow-dot verified"></i>Verified</span><span><i class="flow-dot running"></i>Working</span><span><i class="flow-dot escalated"></i>Escalated</span><span><i class="flow-dot failed"></i>Failed</span><span><i class="flow-dot"></i>Queued</span></div>';
}

// One activity milestone per meaningful backend event; null for noise (tool calls, status ticks, findings).
function milestone(e,names,rootId){
 const m=String(e.message||''),node=names[e.node_id]||'';
 switch(e.type){
  case 'plan':
   if(/^triage:/.test(m))return {title:'Issue triaged',text:m.replace(/^triage:\s*/,'')};
   if(/^froze /.test(m))return {title:'Tests frozen',text:'The acceptance tests are locked so no agent can edit them.'};
   if(/acceptance criteria/.test(m))return {title:'Success defined',text:m.replace(/^defined/,'Defined')};
   if(/^split:/.test(m))return {title:'Split into sub-tasks',text:node};
   if(/^replan/.test(m))return {title:'Replanning',text:node,failed:true};
   return null;
  case 'node_opened':return e.node_id===rootId?{title:'Issue received',text:'Freezing the tests and planning the investigation.'}:null;
  case 'child_spawned':return {title:'Sub-task started',text:m.replace(/\s*\(calls slice[^)]*\)$/,'')};
  case 'evidence_gate':return /enough=False/.test(m)?{title:'Needs more evidence',text:node,failed:true}:{title:'Root cause found',text:node};
  case 'patch_proposed':return {title:'Fix proposed',text:node};
  case 'patch_rejected':return {title:'Fix rejected',text:node+' · '+m,failed:true};
  case 'verify_result':return /^PASSED/.test(m)?{title:'Tests passed',text:node}:{title:'Tests failed',text:node,failed:true};
  case 'children_joined':return {title:'All sub-tasks returned',text:node};
  case 'merge':
   if(/^applied/.test(m))return {title:'Written to your folder',text:m.replace(/^applied to /,'')};
   return /^merged/.test(m)?{title:'Merged into parent',text:m.replace(/^merged:\s*/,'')}:{title:'Merge conflict',text:m,failed:true};
  case 'escalated':return e.node_id===rootId?{title:'Escalated to a human',text:m,failed:true}:{title:'Sub-task escalated',text:node,failed:true};
  case 'run_finished':return /VERIFIED/.test(m)?{title:'Run verified',text:'All acceptance tests pass on the final code.'}:{title:/CANCELLED/.test(m)?'Run stopped':'Run escalated',text:m.replace(/^run \w+:\s*/,''),failed:true};
  default:return null;
 }
}
function milestones(events,names,rootId){return (events||[]).map(e=>{const x=milestone(e,names||{},rootId);return x&&Object.assign(x,{id:e.id,ts:e.ts,type:e.type,nodeId:e.node_id});}).filter(Boolean);}

// Journey: 0 Describe, 1 Investigate, 2 Test, 3 Verify, 4 = all done (verified).
function stage(run,events,rootStatus){
 if(!run)return 1;if(run.status==='verified')return 4;
 const types=new Set((events||[]).map(e=>e.type));
 if(rootStatus==='verifying'||(run.status!=='running'&&types.has('verify_result')))return 3;
 if(types.has('patch_proposed')||types.has('verify_result'))return 2;
 return 1;
}
function runStatus(run,st){
 if(!run)return 'Starting';
 return {verified:'Verified',escalated:'Escalated · unresolved',failed:'Run failed',cancelled:'Stopped'}[run.status]
  ||(st>=3?'Verifying the original goal':st===2?'Testing fixes':'Investigating');
}
function elapsed(fromIso,toIso){const s=Math.max(0,Math.floor((new Date(toIso||Date.now())-new Date(fromIso))/1000));return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
function usageLine(run){const u=run.usage||{},b=(run.budget||{}).run||{};const calls=run.calls_used_live??u.llm_calls??0;
 return 'Live · '+calls+(b.max_llm_calls?' / '+b.max_llm_calls:'')+' LLM calls · '+Number(u.total_tokens||0).toLocaleString('en-US')+' tokens · $'+Number(u.cost_usd||0).toFixed(4)+' · cache '+((u.cache_hit_rate||0)*100).toFixed(0)+'%';}
function testCount(run){return new Set((run.criteria||[]).flatMap(c=>c.test_ids||[])).size;}
scope.TicketZeroModel={esc,tone,statusLabel,flatten,titles,layout,renderFlow,milestones,stage,runStatus,elapsed,usageLine,testCount};
})(globalThis);
