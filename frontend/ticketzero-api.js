(function(scope){
'use strict';
// Client for the TicketZero backend (ticketzero/api.py). Served by Vite, requests go through the /api proxy;
// opened as a standalone file, they go straight to the local backend.
const BASE=scope.location&&scope.location.protocol==='file:'?'http://127.0.0.1:8000':'/api';
async function request(path,options={},asText=false){
 const res=await fetch(BASE+path,options);
 if(!res.ok){let detail=await res.text();try{detail=JSON.parse(detail).detail??detail;}catch{}const error=Error(typeof detail==='string'&&detail?detail:'HTTP '+res.status);error.status=res.status;throw error;}
 return asText?res.text():res.json();
}
const post=(path,body)=>request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
const q=encodeURIComponent;
scope.TicketZeroAPI={
 health:()=>request('/health'),
 scenarios:()=>request('/scenarios'),
 pickFolder:()=>post('/fs/pick'),
 folderFiles:path=>request('/fs/files?path='+q(path)),
 folderFile:(path,file)=>request('/fs/file?path='+q(path)+'&file='+q(file)),
 startScenario:scenario=>post('/runs',{scenario}),
 startFolder:(folder,ticket)=>post('/runs',{folder,ticket}),
 run:id=>request('/runs/'+q(id)),
 tree:id=>request('/runs/'+q(id)+'/tree'),
 events:(id,afterId)=>request('/runs/'+q(id)+'/events?after_id='+afterId+'&limit=2000'),
 node:id=>request('/nodes/'+q(id)),
 report:id=>request('/runs/'+q(id)+'/report',{},true),
 cancel:id=>post('/runs/'+q(id)+'/cancel'),
 apply:id=>post('/runs/'+q(id)+'/apply')
};
})(globalThis);
