import {readFile,writeFile} from 'node:fs/promises';
const read=p=>readFile(new URL(p,import.meta.url),'utf8');
const names=['circle-fill','circle','circle-dot','circle-notch','check-circle-fill','warning-circle-fill','warning-circle','caret-right','caret-down','arrow-right','arrow-up-right','code','clock','file-text','file-code','info','plugs','plus','x','download-simple','folder-open'];
const icons={};for(const n of names){const fill=n.endsWith('-fill');let svg=await read('node_modules/@phosphor-icons/core/assets/'+(fill?'fill':'regular')+'/'+(n==='circle-dot'?'radio-button':n)+'.svg');icons[n]=svg.replace('<svg ','<svg class="icon" aria-hidden="true" focusable="false" ');}
let fonts='';for(const [family,name,weights] of [['DM Sans','dm-sans',[400,500,600]],['Lora','lora',[600]]])for(const weight of weights){const bytes=await readFile(new URL('node_modules/@fontsource/'+name+'/files/'+name+'-latin-'+weight+'-normal.woff2',import.meta.url));fonts+=`@font-face{font-family:'${family}';font-style:normal;font-weight:${weight};font-display:swap;src:url(data:font/woff2;base64,${bytes.toString('base64')}) format('woff2');}\n`;}
const css=await read('ticketzero.css'),apiClient=await read('ticketzero-api.js'),model=await read('ticketzero-model.js'),ui=await read('ticketzero-ui.js');
const readFolderUI=await read('ticketzero-folder-ui.js');
let html=(await read('ticketzero-shell.html')).replace('/* FONT_CSS */',()=>fonts).replace('/* DESIGN_CSS */',()=>css);
html=html.replace(/\{\{icon:([^}]+)\}\}/g,(_,name)=>icons[name]).replace('/* ICON_DATA */',()=>('const TICKETZERO_ICONS='+JSON.stringify(icons)+';')).replace('/* API_SCRIPT */',()=>apiClient).replace('/* MODEL_SCRIPT */',()=>model).replace('/* UI_SCRIPT */',()=>ui);
html=html.replace('/* FOLDER_UI_SCRIPT */',()=>readFolderUI);
await writeFile(new URL('index.html',import.meta.url),html);
console.log('Built TicketZero: '+Math.round(html.length/1024)+' KB');
