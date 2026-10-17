(function(){
'use strict';
// Project folder: chosen with the native picker on the machine running the backend, so the agents can read it
// and run its tests (in a sandbox copy). Browsing and previews come from the backend, which leaves out secrets,
// dependencies and caches. "Allow file edits" lets a verified fix be written back to this folder.
const el=id=>document.getElementById(id),api=globalThis.TicketZeroAPI;
let folder=null,busy=false,trigger=null,browsing=null,previewToken=0;
function message(text){el('folder-feedback').textContent=text;el('folder-feedback').hidden=!text;}
function render(){
 el('folder-connected').hidden=!folder;el('folder-fallback').hidden=true;
 for(const id of ['choose-folder','browse-folder','disconnect-folder','allow-edits'])el(id).disabled=busy;
 el('choose-folder').textContent=busy?'Waiting for the picker…':folder?'Change':'Choose folder';
 el('project-description').textContent=folder?folder.path:'Connect your project so the agents can read it and run its tests.';
 el('project-description').classList.toggle('connected-folder-name',!!folder);
 el('folder-help').textContent='Needed for your own issue: the agents read this folder and run its tests in a sandbox copy. Examples bring their own folder.';
 if(folder){
  el('browse-folder').textContent=folder.files+' files · View files';
  el('folder-access').textContent=folder.allowEdits?'Read + edit access':'Read only';
  el('allow-edits').hidden=folder.allowEdits;
  el('folder-scope').textContent=(folder.tests?folder.tests+' test file'+(folder.tests===1?'':'s')+' · tests run with '+folder.python:'No pytest tests found yet')
   +(folder.allowEdits?'. A verified fix will be written here; files you change meanwhile are never overwritten.':'. Your folder stays unchanged unless you allow edits.');
 }
}
async function connect(path){
 const info=await api.folderFiles(path);
 folder={path:info.path,name:info.name,files:info.files,tests:info.test_file_count,python:info.python,allowEdits:false,entries:info.entries};
 message(info.test_file_count?'Folder connected. '+(info.skipped_secrets.length?info.skipped_secrets.length+' secret file(s) are excluded from the agents.':'Nothing has been uploaded.')
  :'Folder connected, but no pytest tests were found. Add a test that fails now, so a fix can be verified.');
}
el('choose-folder').addEventListener('click',async()=>{
 message('');busy=true;render();
 try{const res=await api.pickFolder();if(res.path)await connect(res.path);}
 catch(error){message(error.status===501?'The folder picker needs the backend running on macOS.':error.status?error.message:'Can’t reach the backend. Start it, then choose the folder again.');}
 finally{busy=false;render();}
});
el('allow-edits').addEventListener('click',()=>{if(!folder)return;folder.allowEdits=true;message('Edits allowed. Only a fix that passes your tests is written back.');render();});
el('disconnect-folder').addEventListener('click',()=>{folder=null;browsing=null;previewToken++;if(el('folder-dialog').open)el('folder-dialog').close();message('Disconnected.');render();});

function showFiles(){
 const query=el('file-search').value.trim().toLowerCase(),list=el('folder-file-list');list.replaceChildren();
 const entries=(browsing?.entries||[]).filter(e=>e.path.toLowerCase().includes(query));
 if(!entries.length){const p=document.createElement('p');p.className='empty-files';p.textContent=query?'No matching files.':'No readable files found.';list.append(p);return;}
 for(const entry of entries){const b=document.createElement('button');b.type='button';b.className='file-entry';b.textContent=entry.path;b.title=entry.path;b.addEventListener('click',()=>preview(entry.path,b));list.append(b);}
}
async function preview(path,button){
 const token=++previewToken;el('folder-file-list').querySelectorAll('button').forEach(b=>b.removeAttribute('aria-current'));button.setAttribute('aria-current','true');
 el('file-preview-name').textContent=path;el('file-preview-content').textContent='Reading file…';
 try{const file=await api.folderFile(browsing.path,path);if(token===previewToken)el('file-preview-content').textContent=file.text||'(Empty file)';}
 catch(error){if(token===previewToken)el('file-preview-content').textContent=error.message;}
}
async function openFiles(path,button){
 if(!path)return;trigger=button;previewToken++;
 el('folder-dialog-title').textContent=path.split('/').filter(Boolean).pop()||path;el('folder-dialog-note').textContent='Loading files…';
 el('file-search').value='';el('file-preview-name').textContent='Select a file to preview it.';
 el('file-preview-content').textContent='Previewing a file sends it nowhere. During a run, the agents read files and send the relevant parts to Crusoe’s models.';
 el('folder-file-list').replaceChildren();if(!el('folder-dialog').open)el('folder-dialog').showModal();
 try{const info=folder&&folder.path===path?folder:await api.folderFiles(path);browsing={path,entries:info.entries};
  el('folder-dialog-note').textContent=path+' · '+info.entries.length+' files the agents can read. Secrets (.env, keys), dependencies, virtualenvs and caches are left out.';showFiles();}
 catch(error){el('folder-dialog-note').textContent=error.message;}
}
el('browse-folder').addEventListener('click',e=>openFiles(folder?.path,e.currentTarget));
el('file-search').addEventListener('input',showFiles);
el('close-folder').addEventListener('click',()=>el('folder-dialog').close());
el('folder-dialog').addEventListener('close',()=>{previewToken++;trigger?.focus();});
el('folder-dialog').addEventListener('click',e=>{if(e.target===el('folder-dialog')){const r=el('folder-dialog').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
globalThis.TicketZeroFolder={current:()=>folder&&{path:folder.path,allowEdits:folder.allowEdits},openFiles};
render();
})();
