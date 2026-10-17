const test=require('node:test');
const assert=require('node:assert/strict');
require('../ticketzero-model.js');
const M=globalThis.TicketZeroModel;

const node=(id,status,children=[],extra={})=>({id,title:'Task '+id,depth:0,status,reason:'',replans:0,children,...extra});
const TREE=[node('root','waiting_children',[node('a','verified'),node('b','investigating',[node('b1','escalated'),node('b2','pending')]),node('c','failed')])];

test('layout centres every parent over its children and never overlaps nodes on a row',()=>{
  const L=M.layout(TREE);const at=id=>L.nodes.find(p=>p.node.id===id);
  assert.equal(L.nodes.length,6);assert.equal(L.edges.length,5);
  assert.equal(at('root').x,(at('a').x+at('c').x)/2);
  assert.equal(at('b').x,(at('b1').x+at('b2').x)/2);
  assert.ok(at('b1').y>at('b').y&&at('b').y>at('root').y);
  const rows={};for(const p of L.nodes)(rows[p.y]??=[]).push(p.x);
  for(const xs of Object.values(rows)){xs.sort((a,b)=>a-b);for(let i=1;i<xs.length;i++)assert.ok(xs[i]-xs[i-1]>=L.W,'nodes overlap');}
  assert.ok(L.width>=Math.max(...L.nodes.map(p=>p.x))+L.W);
});

test('flowchart marks status, selection and running edges, and escapes model-written text',()=>{
  const evil=[node('r','verifying',[node('x','verified',[],{title:'<img src=x onerror=alert(1)>',reason:'"><script>'})])];
  const html=M.renderFlow(evil,'x');
  assert.ok(!html.includes('<img')&&!html.includes('<script'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.match(html,/class="flow-node running" data-node="r"/);
  assert.match(html,/class="flow-node verified selected" data-node="x"/);
  assert.equal((html.match(/class="flow-edge /g)||[]).length,1);
  assert.match(M.renderFlow([],null),/Waiting for the planner/);
});

test('backend events become readable milestones; noise is dropped',()=>{
  const names={root:'Root',a:'Fix API host'};
  const ev=(id,type,message,node_id='a')=>({id,type,message,node_id,ts:'2026-10-17T20:00:0'+id+'Z'});
  const ms=M.milestones([
    ev(1,'plan','split: three bugs','root'),ev(2,'tool_call','read_file(...)'),ev(3,'child_spawned','Fix API host (calls slice 30)','root'),
    ev(4,'verify_result','FAILED: tests failed'),ev(5,'verify_result','PASSED: 1 passed'),ev(6,'merge','merged: Fix API host','root'),
    ev(7,'escalated','fix attempts exhausted'),ev(8,'run_finished','run VERIFIED: re-verified','root')],names,'root');
  assert.deepEqual(ms.map(m=>m.title),['Split into sub-tasks','Sub-task started','Tests failed','Tests passed','Merged into parent','Sub-task escalated','Run verified']);
  assert.equal(ms[1].text,'Fix API host');
  assert.equal(ms[2].failed,true);
  assert.equal(M.milestones([ev(9,'escalated','budget','root')],names,'root')[0].title,'Escalated to a human');
});

test('journey stage and status follow the run',()=>{
  const run=status=>({status});
  assert.equal(M.stage(run('running'),[{type:'plan'}],'planning'),1);
  assert.equal(M.stage(run('running'),[{type:'patch_proposed'}],'waiting_children'),2);
  assert.equal(M.stage(run('running'),[{type:'verify_result'}],'verifying'),3);
  assert.equal(M.stage(run('verified'),[],'verified'),4);
  assert.equal(M.runStatus(run('escalated'),3),'Handed to a human');
  assert.equal(M.runStatus(run('running'),2),'Testing fixes');
  assert.equal(M.testCount({criteria:[{test_ids:['t1','t2']},{test_ids:['t2','t3']}]}),3);
});

test('handoff shows cause, evidence and checks, and escapes model-written text',()=>{
  const html=M.renderHandoff({kind:'no_code_bug',summary:'s',cause:'503s <b>from</b> the platform',
    evidence:[{kind:'log',ref:'req_7f3a91',detail:'503 at 02:11'}],checks:['Contact support with <script>x</script>']});
  assert.match(html,/Likely cause/);assert.match(html,/req_7f3a91/);assert.match(html,/What a human should check next/);
  assert.ok(!html.includes('<script>')&&!html.includes('<b>'));
  assert.equal(M.renderHandoff(null),'');
});
