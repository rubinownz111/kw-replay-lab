import styles from './styles.css';
import {renderDesync} from './desync_view';
import {caseMarkdown} from './desync';
import {escapeHtml as esc, display, timeLabel, inRange, objectReferences, compareReports, csv, actionBuckets, recordedDate} from './analytics';
import {listSaved, saveReplay, removeReplay, loadWorkspace, writeWorkspace} from './storage';
import {validateReport} from './types';
import type {Command, ReplayReport, Row, MountOptions, SavedReplay, CameraPoint, Diagnostic} from './types';

const css=styles;
const tabs=['Overview','Desync Lab','Players','Build requests','Timeline','Commands','Objects','Camera','Replay streams','Forensics','Network','Telemetry','Findings','Technical','Compare'];
const fmt=(v: unknown)=>typeof v==='number'?v.toLocaleString():display(v);
const button=(label:string,action:string,kind='quiet',extra='')=>`<button class="button ${kind}" data-action="${action}" ${extra}>${esc(label)}</button>`;
const empty=(message:string)=>`<div class="empty">${esc(message)}</div>`;
const tag=(text:unknown)=>`<span class="tag">${esc(text)}</span>`;
const rows=(v:unknown):Row[]=>Array.isArray(v)?v.filter(x=>x&&typeof x==='object') as Row[]:[];
const json=(v:unknown)=>`<pre>${esc(JSON.stringify(v,null,2))}</pre>`;
const card=(title:string,body:string,note='')=>`<section class="card"><div class="card-head"><h2>${esc(title)}</h2>${note?`<small>${esc(note)}</small>`:''}</div>${body}</section>`;
const metric=(label:string,value:unknown,detail='')=>`<div class="metric"><small>${esc(label)}</small><strong>${esc(fmt(value))}</strong><span>${esc(detail)}</span></div>`;
const details=(title:string,v:unknown)=>`<details><summary>${esc(title)}</summary>${json(v)}</details>`;
function table(headers:string[], body:string[], caption=''):string {return `<div class="table-scroll"><table>${caption?`<caption>${esc(caption)}</caption>`:''}<thead><tr>${headers.map(x=>`<th scope="col">${esc(x)}</th>`).join('')}</tr></thead><tbody>${body.length?body.join(''):`<tr><td colspan="${headers.length}">${empty('No matching records in this view.')}</td></tr>`}</tbody></table></div>`;}
function genericTable(data:Row[],keys:string[],limit=100):string {return table(keys.map(x=>x.replaceAll('_',' ')),data.slice(0,limit).map(r=>`<tr>${keys.map(k=>`<td>${esc(fmt(r[k]))}</td>`).join('')}</tr>`))+ (data.length>limit?`<p class="note">Showing ${limit} of ${fmt(data.length)}. Export Report JSON for all rows.</p>`:'');}

class ReplayLab {
 private root: ShadowRoot;
 private report: ReplayReport | null=null;
 private file: Blob | undefined;
 private comparison: ReplayReport | null=null;
 private saved: SavedReplay[]=[];
 private view:'upload'|'report'|'library'='upload';
 private tab='Overview';
 private start=0; private end=0; private cursor=0;
 private search=''; private player=''; private category=''; private actionsOnly=false; private page=0;
 private desyncIncident=0; private desyncCheckpoint=''; private diagnosticA=0; private diagnosticB=1; private diagnosticQuery='';
 private objectId=''; private cameraStream=''; private cameraMode='path';
 private libraryQuery=''; private libraryMap=''; private libraryDate=''; private librarySort='newest';
 private theme:'system'|'light'|'dark'; private busy=false; private timer:ReturnType<typeof setInterval>|null=null;
 private notice=''; private error=false; private rawToken=0;
 private historyDepth=0; private backLabel=''; private restoring=false; private workspaceDirty=false; private workspaceSaved=false; private workspaceError=false;
 private saveTimer:ReturnType<typeof setTimeout>|null=null; private saveQueue:Promise<void>=Promise.resolve();
 private readonly abort=new AbortController();
 constructor(private host:HTMLElement,private options:MountOptions,private assetsBase:string){
  this.root=host.shadowRoot??host.attachShadow({mode:'open'});
  this.theme=options.theme??'system';
  try{if(!options.theme){const saved=localStorage.getItem('kw-replay-theme');if(saved==='dark'||saved==='light')this.theme=saved;}}catch{/* Storage is optional. */}
  this.root.innerHTML=`<style>${css}</style><div class="lab"><div id="shell"></div><dialog aria-labelledby="inspector-title"><div id="inspector"></div></dialog></div>`;
  this.root.addEventListener('click',e=>{const el=(e.target as Element).closest<HTMLElement>('[data-action]');if(el)void this.action(el.dataset.action!,el).catch(x=>this.message(String(x.message??x),true)).finally(()=>this.rememberPosition());},{signal:this.abort.signal});
  this.root.addEventListener('change',e=>void this.change(e.target as HTMLInputElement).catch(x=>this.message(String(x.message??x),true)).finally(()=>this.rememberPosition()),{signal:this.abort.signal});
  this.root.addEventListener('input',e=>{this.input(e.target as HTMLInputElement);this.rememberPosition();},{signal:this.abort.signal});
  this.root.addEventListener('keydown',e=>{if((e as KeyboardEvent).key==='Escape')this.stop();},{signal:this.abort.signal});
  window.addEventListener('popstate',e=>void this.restoreHistory(e.state),{signal:this.abort.signal});
  window.addEventListener('pagehide',()=>void this.flushWorkspace(),{signal:this.abort.signal});
  if(options.report){validateReport(options.report);this.openReport(options.report);}
  else {this.restoring=true;this.busy=true;this.render();void this.restoreWorkspace();}
 }
 destroy(){this.stop();void this.flushWorkspace();this.abort.abort();this.root.innerHTML='';}
 private q<T extends Element=HTMLElement>(selector:string):T{return this.root.querySelector<T>(selector)!;}
 private message(text:string,error=false){this.notice=text;this.error=error;const n=this.q('#notice');if(n){n.textContent=text;n.className=`notice ${error?'error':''}`;n.hidden=!text;}}
 private stop(){if(this.timer){clearInterval(this.timer);this.timer=null;}const b=this.q<HTMLButtonElement>('[data-action="play"]');if(b)b.textContent='Play timeline';}
 private position():Row{return {view:this.view,tab:this.tab,start:this.start,end:this.end,cursor:this.cursor,search:this.search,player:this.player,category:this.category,actionsOnly:this.actionsOnly,page:this.page,desyncIncident:this.desyncIncident,desyncCheckpoint:this.desyncCheckpoint,diagnosticA:this.diagnosticA,diagnosticB:this.diagnosticB,diagnosticQuery:this.diagnosticQuery,objectId:this.objectId,cameraStream:this.cameraStream,cameraMode:this.cameraMode};}
 private applyPosition(position:Row){
  const current=this.position();for(const key of Object.keys(current)){const value=position[key];if(typeof value===typeof current[key]&&(typeof value!=='number'||Number.isFinite(value)))(this as unknown as Row)[key]=value;}
  if(!['upload','report','library'].includes(this.view))this.view='report';if(!tabs.includes(this.tab))this.tab='Overview';
  const last=this.report?.match.last_frame??0;this.start=Math.max(0,Math.min(this.start,last));this.end=Math.max(this.start,Math.min(this.end,last));this.cursor=Math.max(this.start,Math.min(this.cursor,this.end));
 }
 private historyEntry(){return {app:'kw-replay-lab',hash:this.report?.file.sha256??null,position:this.position(),depth:this.historyDepth,backLabel:this.backLabel};}
 private replaceHistory(){history.replaceState(this.historyEntry(),'');}
 private navigate(update:()=>void){
  this.stop();this.replaceHistory();const label=this.view==='report'?this.tab:this.view==='library'?'Library':'Open replay';
  update();this.historyDepth++;this.backLabel=label;history.pushState(this.historyEntry(),'');this.render();this.rememberPosition();
 }
 private async restoreHistory(entry:unknown){
  this.stop();this.q<HTMLDialogElement>('dialog')?.close();const state=entry as ReturnType<ReplayLab['historyEntry']>|null;
  if(state?.app==='kw-replay-lab'&&state.hash===(this.report?.file.sha256??null)){
   this.historyDepth=Number.isInteger(state.depth)?Math.max(0,state.depth):0;this.backLabel=String(state.backLabel??'');this.applyPosition(state.position??{});
  }else{this.historyDepth=0;this.backLabel='';this.view=this.report?'report':'upload';this.replaceHistory();}
  if(!this.report&&this.view==='report')this.view='upload';
  if(this.view==='library'){try{this.saved=await listSaved();}catch(e){this.message((e as Error).message,true);}}
  this.render();this.rememberPosition();
 }
 private async restoreWorkspace(){
  try{
   const active=await loadWorkspace();if(this.abort.signal.aborted)return;
   if(active){validateReport(active.report);if(active.comparison)validateReport(active.comparison);this.report=active.report;this.file=active.file;this.comparison=active.comparison;this.applyPosition(active.position);this.workspaceSaved=true;
    const state=history.state;if(state?.app==='kw-replay-lab'&&state.hash===this.report.file.sha256){this.applyPosition(state.position??{});this.historyDepth=Number.isInteger(state.depth)?Math.max(0,state.depth):0;this.backLabel=String(state.backLabel??'');}
    try{const note=JSON.parse(sessionStorage.getItem('kw-replay-current-notes')??'null');if(note?.hash===this.report.file.sha256&&typeof note.notes==='string'){this.caseData().notes=note.notes.slice(0,10000);}}catch{}
    if(this.view==='library')this.saved=await listSaved();
   }
  }catch(e){this.notice=(e as Error).message;this.error=true;}
  finally{if(!this.abort.signal.aborted){this.restoring=false;this.busy=false;this.replaceHistory();this.render();}}
 }
 private rememberPosition(){
  if(this.restoring||this.abort.signal.aborted)return;this.replaceHistory();
  if(this.options.offline||!this.report)return;
  if(this.saveTimer)clearTimeout(this.saveTimer);this.saveTimer=setTimeout(()=>void this.flushWorkspace(),250);
 }
 private flushWorkspace():Promise<void>{
  if(this.saveTimer){clearTimeout(this.saveTimer);this.saveTimer=null;}
  if(this.options.offline||this.restoring)return this.saveQueue;
  const position=this.position(),active=this.report?(this.workspaceDirty?{report:this.report,file:this.file,comparison:this.comparison}:undefined):null;
  this.workspaceDirty=false;
  this.saveQueue=this.saveQueue.then(()=>writeWorkspace(position,active)).then(()=>{this.workspaceSaved=!!this.report;this.workspaceError=false;this.sessionStatus();}).catch(e=>{this.workspaceDirty=!!this.report;this.workspaceSaved=false;this.workspaceError=true;this.sessionStatus();this.message((e as Error).message,true);});
  return this.saveQueue;
 }
 private sessionStatus(){const el=this.q('#session-status');if(el)el.textContent=this.workspaceError?'Browser recovery is unavailable. Keep this tab open or export your work.':this.workspaceSaved?'Replay kept in this browser until you close it.':'Keeping the current replay in this browser...';}
 private async action(action:string,el:HTMLElement){
  if(this.restoring)return;
  if(action==='back-view'){history.back();return;}
  if(action==='close-replay'){this.stop();this.q<HTMLDialogElement>('dialog').close();this.report=null;this.file=undefined;this.comparison=null;this.workspaceDirty=false;this.workspaceSaved=false;this.historyDepth=0;this.backLabel='';this.view='upload';this.notice='';try{sessionStorage.removeItem('kw-replay-current-notes');}catch{}await this.flushWorkspace();this.replaceHistory();this.render();return;}
  if(action==='desync-checkpoint'){this.desyncCheckpoint=el.dataset.key!;this.renderPanel();return;}
  if(action==='desync-window'||action==='desync-object'){
   const incident=this.report?.desync?.incidents[this.desyncIncident];if(!incident)return;
   this.navigate(()=>{this.start=incident.start_frame;this.end=incident.frame;this.cursor=this.end;this.search='';this.player='';this.category='';this.actionsOnly=false;this.page=0;
   this.tab=action==='desync-object'?'Objects':el.dataset.view??'Commands';if(action==='desync-object')this.objectId=el.dataset.id!;});return;
  }
  if(action==='desync-export'){this.download(caseMarkdown(this.report!,this.diagnosticA,this.diagnosticB),'desync-case.md','text/markdown');return;}
  if(action==='desync-remove-peer'){this.caseData().peers.splice(Number(el.dataset.peer),1);this.renderPanel();return;}
  if(action==='desync-remove-diagnostic'){this.caseData().diagnostics.splice(Number(el.dataset.index),1);this.diagnosticA=0;this.diagnosticB=1;this.renderPanel();return;}
  if(action==='desync-use-comparison'){if(!this.comparison){this.message('Choose a replay on Compare first, or use Add player replays.');return;}this.addPeer(this.comparison);this.renderPanel();return;}
  if(action==='desync-peer-command'){
   const peer=this.caseData().peers[Number(el.dataset.peer)],c=peer?.commands.items[Number(el.dataset.index)];if(!c)return;
   this.q('#inspector').innerHTML=`<div class="dialog-head"><h2 id="inspector-title">${esc(c.display_name??c.name)}</h2>${button('Close','close-inspector')}</div><p>${esc(peer.file.name)} / frame ${c.frame}</p><p>${esc(c.meaning)}</p>${json(c)}`;this.showDialog();return;
  }
  if(action==='desync-demo'){await this.task(async()=>{const response=await fetch(this.api('/desync/demo'));if(!response.ok)throw new Error('Could not load the desync demo.');const report:unknown=await response.json();validateReport(report);this.openReport(report);this.tab='Desync Lab';this.render();});return;}
  if(action==='theme'){this.theme=this.theme==='system'?'light':this.theme==='light'?'dark':'system';try{localStorage.setItem('kw-replay-theme',this.theme);}catch{}this.applyTheme();el.textContent=`Theme: ${this.theme}`;return;}
  if(action==='close-inspector'){this.q<HTMLDialogElement>('dialog').close();return;}
  if(action==='tab'){if(this.tab!==el.dataset.tab)this.navigate(()=>{this.tab=el.dataset.tab!;this.page=0;});return;}
  if(action==='inspect'){await this.inspect(Number(el.dataset.index));return;}
  if(action==='record'){await this.inspectRecord(Number(el.dataset.index));return;}
  if(action==='object'){this.q<HTMLDialogElement>('dialog').close();this.navigate(()=>{this.objectId=el.dataset.id!;this.tab='Objects';this.page=0;});return;}
  if(action==='play'){if(this.timer){this.stop();return;}if(this.cursor>=this.end)this.cursor=this.start;el.textContent='Pause timeline';this.timer=setInterval(()=>{this.cursor=Math.min(this.end,this.cursor+15);this.updateScope();if(this.cursor>=this.end)this.stop();},250);return;}
  if(action==='reset-range'){this.stop();this.start=0;this.end=this.report!.match.last_frame;this.cursor=this.end;this.updateScope();return;}
  if(action==='previous'||action==='next'){this.page=Math.max(0,this.page+(action==='next'?1:-1));this.renderPanel();return;}
  if(action==='json'){this.download(JSON.stringify(this.report,null,2),'analysis.json','application/json');return;}
  if(action==='commands-csv'){this.download(csv(this.report!.commands.items as unknown as Row[],['frame','time','player','source_index','message_type','name','category','arguments','record_index','wire_offset','wire_size','wire_sha256']),'commands.csv','text/csv');return;}
  if(action==='records-csv'){this.download(csv(this.report!.forensics.records as unknown as Row[],['index','offset','frame','channel','payload_size','payload_sha256']),'records.csv','text/csv');return;}
  if(action==='html'){await this.exportHtml();return;}
  if(action==='save'){await this.saveCurrent();return;}
  if(action==='open-saved'){const item=this.saved.find(x=>x.hash===el.dataset.hash)!;this.openReport(item.report,item.file);return;}
  if(action==='compare-saved'){this.comparison=this.saved.find(x=>x.hash===el.dataset.hash)!.report;this.workspaceDirty=true;this.navigate(()=>{this.view='report';this.tab='Compare';});return;}
  if(action==='remove-saved'){await removeReplay(el.dataset.hash!);this.saved=await listSaved();this.renderLibrary();this.message('Saved copy removed. The original file is unchanged.');return;}
  if(action==='library'){this.stop();this.saved=await listSaved();this.navigate(()=>{this.view='library';});return;}
  if(action==='report'&&this.report){this.navigate(()=>{this.view='report';});return;}
  if(action==='upload'){if(this.view!=='upload')this.navigate(()=>{this.view='upload';this.notice='';});return;}
  if(action==='demo'){await this.task(async()=>{const response=await fetch(this.api('/demo'));if(!response.ok)throw new Error('The analyzer service could not load the demo.');const report:unknown=await response.json();validateReport(report);this.openReport(report);});}
 }
 private api(path:string){return (this.options.apiBase??'/api').replace(/\/$/,'')+path;}
 private async task(callback:()=>Promise<void>){if(this.busy)return;this.busy=true;this.setBusy();try{await callback();}finally{await this.flushWorkspace();this.busy=false;this.setBusy();}}
 private setBusy(){this.root.querySelectorAll<HTMLInputElement|HTMLButtonElement>('[data-upload], [data-action="demo"], [data-action="save"], [data-action="desync-demo"], [data-action="close-replay"]').forEach(x=>x.disabled=this.busy);const n=this.q('#busy');if(n){n.hidden=!this.busy;n.textContent='Decoding replay data. Please wait...';}}
 private async analyze(file:File):Promise<ReplayReport>{
  if(!/\.kwreplay$/i.test(file.name))throw new Error('Choose a .KWReplay file. Only 1.02 is supported.');
  if(file.size>64*1024*1024)throw new Error('Replay exceeds the 64 MiB limit.');
  const form=new FormData();form.append('replay',file);form.append('reference_catalog',String(this.q<HTMLInputElement>('#catalog')?.checked??false));
  const telemetry=this.q<HTMLInputElement>('#sidecar')?.files?.[0];if(telemetry)form.append('telemetry',telemetry);
  const response=await fetch(this.api('/analyze'),{method:'POST',body:form,signal:AbortSignal.timeout(120000)});
  const payload:unknown=await response.json().catch(()=>{throw new Error(`Analyzer service returned HTTP ${response.status}. Check the replay service configuration.`);});
  if(!response.ok)throw new Error(String((payload as Row).error??`Analysis failed (${response.status}).`));validateReport(payload);return payload;
 }
 private caseData(){this.workspaceDirty=true;return this.report!.desync_case??(this.report!.desync_case={peers:[],diagnostics:[],notes:''});}
 private addPeer(report:ReplayReport){
  if(report.file.sha256===this.report!.file.sha256)throw new Error('This is the current replay. Choose another recording.');
  const data=this.caseData();if(data.peers.some(p=>p.file.sha256===report.file.sha256))return;
  if(data.peers.length>=3)throw new Error('A case holds up to three comparison recordings. Remove one first.');
  const {desync_case:ignored,...peer}=report;data.peers.push(peer);
 }
 private async change(input:HTMLInputElement){
  if(input.id==='desync-incident'){this.desyncIncident=Number(input.value);this.desyncCheckpoint='';this.renderPanel();return;}
  if(input.id==='diagnostic-a'||input.id==='diagnostic-b'){if(input.id==='diagnostic-a')this.diagnosticA=Number(input.value);else this.diagnosticB=Number(input.value);this.renderPanel();return;}
  if(input.id==='desync-peers'){
   const files=Array.from(input.files??[]);input.value='';await this.task(async()=>{const errors:string[]=[];for(const file of files){try{if(this.caseData().peers.length>=3)throw new Error('Three comparison recordings are already attached.');this.addPeer(await this.analyze(file));}catch(e){errors.push(`${file.name}: ${(e as Error).message}`);}}this.renderPanel();this.message(errors.join('\n')||'Player recordings added.',!!errors.length);});return;
  }
  if(input.id==='desync-diagnostics'){
   const files=Array.from(input.files??[]);input.value='';if(!files.length)return;
   if(this.caseData().diagnostics.length+files.length>8)throw new Error('A case holds up to eight diagnostic files.');
   if(files.some(f=>f.size>8*1024*1024)||files.reduce((n,f)=>n+f.size,0)+this.caseData().diagnostics.reduce((n,f)=>n+f.size,0)>32*1024*1024)throw new Error('Use diagnostic files up to 8 MiB each and 32 MiB per case.');
   await this.task(async()=>{const form=new FormData();files.forEach(f=>form.append('diagnostics',f));const response=await fetch(this.api('/desync/diagnostics'),{method:'POST',body:form,signal:AbortSignal.timeout(120000)});const payload=await response.json();if(!response.ok)throw new Error(payload.error??'Diagnostic upload failed.');for(const doc of payload.diagnostics as Diagnostic[])if(!this.caseData().diagnostics.some(x=>x.sha256===doc.sha256))this.caseData().diagnostics.push(doc);this.renderPanel();this.message(payload.errors.map((x:Row)=>`${x.name}: ${x.error}`).join('\n')||'Diagnostic files added.',!!payload.errors.length);});return;
  }

  if(input.id==='replay-input'||input.id==='compare-input'||input.id==='library-import'){
   const files=Array.from(input.files??[]);const kind=input.id;input.value='';if(!files.length)return;
   await this.task(async()=>{
    if(kind==='library-import'){
     const messages:string[]=[];let completed=0;
     for(const file of files){try{const report=await this.analyze(file);const duplicate=await this.save(report,file);messages.push(`${file.name}: ${duplicate?'duplicate updated':'saved'}`);completed++;}catch(e){messages.push(`${file.name}: ${(e as Error).message}`);}this.message(`${completed}/${files.length} saved. ${messages.at(-1)}`);}
     this.saved=await listSaved();this.renderLibrary();this.message(messages.join('\n'),completed<files.length);
    }else{const report=await this.analyze(files[0]);if(kind==='compare-input'){this.comparison=report;this.workspaceDirty=true;this.renderPanel();}else this.openReport(report,files[0]);this.message('');}
   });return;
  }
  if(input.id==='player-filter'){this.player=input.value;this.page=0;this.renderPanel();}
  if(input.id==='category-filter'){this.category=input.value;this.page=0;this.renderPanel();}
  if(input.id==='actions-filter'){this.actionsOnly=input.checked;this.page=0;this.renderPanel();}
  if(input.id==='camera-stream'){this.cameraStream=input.value;this.renderPanel();}
  if(input.id==='camera-mode'){this.cameraMode=input.value;this.drawCamera();}
  if(input.id==='library-map'){this.libraryMap=input.value;this.renderLibraryRows();}
  if(input.id==='library-sort'){this.librarySort=input.value;this.renderLibraryRows();}
 }
 private input(input:HTMLInputElement){
  if(input.id==='desync-notes'){this.caseData().notes=input.value;try{sessionStorage.setItem('kw-replay-current-notes',JSON.stringify({hash:this.report!.file.sha256,notes:input.value}));}catch{}return;}
  if(input.id==='diagnostic-search'){const pos=input.selectionStart;this.diagnosticQuery=input.value;this.renderPanel();const next=this.q<HTMLInputElement>('#diagnostic-search');next.focus();next.setSelectionRange(pos,pos);return;}
  if(['range-start','range-end','cursor'].includes(input.id)){
   const n=Math.max(0,Math.min(this.report!.match.last_frame,Number(input.value)||0));this.stop();
   if(input.id==='range-start')this.start=Math.min(n,this.end);else if(input.id==='range-end')this.end=Math.max(n,this.start);else this.cursor=n;
   this.cursor=Math.max(this.start,Math.min(this.end,this.cursor));this.page=0;this.updateScope();return;
  }
  if(input.id==='search'){this.search=input.value;this.page=0;this.renderDataTable();}
  if(input.id==='object-id'){this.objectId=input.value;this.page=0;this.renderDataTable();}
  if(input.id==='library-search'){this.libraryQuery=input.value;this.renderLibraryRows();}
  if(input.id==='library-date'){this.libraryDate=input.value;this.renderLibraryRows();}
 }
 private openReport(report:ReplayReport,file?:Blob){this.stop();this.report=report;this.file=file;this.comparison=null;this.workspaceDirty=true;this.workspaceSaved=false;this.workspaceError=false;this.historyDepth=0;this.backLabel='';try{sessionStorage.removeItem('kw-replay-current-notes');}catch{}this.view='report';this.start=0;this.end=report.match.last_frame;this.cursor=this.end;this.search='';this.player='';this.category='';this.page=0;this.objectId='';this.cameraStream='';this.actionsOnly=false;this.tab='Overview';this.desyncIncident=0;this.desyncCheckpoint='';this.diagnosticA=0;this.diagnosticB=1;this.diagnosticQuery='';this.replaceHistory();this.render();void this.flushWorkspace();}
 private applyTheme(){const lab=this.q('.lab');if(this.theme==='system')lab.removeAttribute('data-theme');else lab.setAttribute('data-theme',this.theme);}
 private render(){
  this.q('#shell').innerHTML=`<header class="masthead"><div class="brand"><span class="brand-title">KW Replay Lab</span><span class="mono">1.02 / ${this.options.offline?'OFFLINE REPORT':'REPLAY ANALYSIS'}</span></div><div class="header-actions">${!this.options.offline?button('Analyze replay','upload')+button('Library','library'):''}${this.report&&this.view!=='report'?button('Resume replay','report'):''}${this.report&&!this.options.offline?button('Close replay','close-replay'):''}${button(`Theme: ${this.theme}`,'theme')}</div></header><div id="notice" class="notice ${this.error?'error':''}" role="status" ${this.notice?'':'hidden'}>${esc(this.notice)}</div><div id="busy" class="notice" role="status" hidden></div>${this.report&&this.view!=='report'?`<div class="active-replay">Current replay: <strong>${esc(this.report.file.name)}</strong>. Use Resume replay to return.</div>`:''}<main id="content"></main><footer>KW 1.02 only <span>Recorded commands, not game simulation.</span></footer>`;
  this.applyTheme();if(this.view==='upload')this.renderUpload();else if(this.view==='library')this.renderLibrary();else this.renderReport();this.setBusy();
 }
 private renderUpload(){
  this.q('#content').innerHTML=`<section class="landing"><div><p class="eyebrow">KANE'S WRATH / REPLAY WORKSPACE</p><h1>Inspect your replay.</h1><p class="lede">Browse commands, track camera activity and compare 1.02 replays.</p><div class="facts-strip">${metric('Target','1.02','Other versions rejected')}${metric('Command names','233','Exact-build recovery')}${metric('Workflow','Offline','Standalone local server')}</div></div><section class="card upload-card"><p class="eyebrow">01 / START AN ANALYSIS</p><h2>Open your replay.</h2><p>Choose a finalized .KWReplay file, up to 64 MiB.</p><label class="drop" id="drop-zone"><span class="upload-label">Choose or drop a replay</span><input id="replay-input" data-upload type="file" accept=".KWReplay,.kwreplay" aria-label="Choose replay"></label><details><summary>Optional research inputs</summary><label class="field">Experimental telemetry sidecar<input id="sidecar" type="file" accept=".jsonl,.kwtelemetry"></label><label class="check"><input id="catalog" type="checkbox"> Include unverified R22 asset hints</label><p class="note">Telemetry and asset hints are unverified.</p></details>${button('Explore a synthetic demo','demo','primary')}${button('Try Desync Lab','desync-demo')}<p class="note">Files are analyzed locally. No account or game installation needed.</p></section></section><div class="feature-strip">${card('Timeline','<p>Filter commands, camera samples and checkpoints by time.</p>')}${card('Inspect and compare','<p>Check command bytes, object IDs and replay differences.</p>')}${card('Save and export','<p>Save reports in your browser or export JSON, CSV and HTML.</p>')}</div>`;
  const drop=this.q('#drop-zone');drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('dragging');});drop.addEventListener('dragleave',()=>drop.classList.remove('dragging'));drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('dragging');const file=(e as DragEvent).dataTransfer?.files[0];if(file)void this.task(async()=>{this.openReport(await this.analyze(file),file);this.message('');}).catch(e=>this.message(e.message,true));});
 }
 private renderReport(){const r=this.report!;
  this.q('#content').innerHTML=`<div class="report-title"><div><p class="eyebrow">REPLAY / ${esc(r.match.game_version)} / ${r.evidence.sample_kind?'SYNTHETIC FIXTURE':'RECORDED FILE'}</p><h1>${esc(r.match.title||'Untitled replay')}</h1><p>${esc(r.match.map_name)} <span class="separator">/</span> ${r.players.length} players <span class="separator">/</span> ${esc(r.match.duration)}</p></div><div class="exports">${!this.options.offline?button('Save to library','save'):''}${button('Report JSON','json')}${button('Commands CSV','commands-csv')}${button('Records CSV','records-csv')}${!this.options.offline?button('Portable HTML','html','primary'):''}</div></div><div class="workspace-navigation">${this.historyDepth>0?button(`Back to ${this.backLabel}`,'back-view'):''}<div><strong>${esc(r.file.name)}</strong>${!this.options.offline?'<small id="session-status"></small>':''}</div></div><p class="evidence-note">${r.evidence.sample_kind?'Synthetic parser fixture, not a playable match. ':''}Declared 1.02. ${r.summary.decode_error_count} payload decode errors. Asset hints ${r.evidence.asset_catalog==='disabled'?'off':'enabled (unverified)'}. Commands are requests; CRCs are recorded values.</p><div class="workbench"><aside class="rail"><p class="mono">WORKSPACE</p><nav aria-label="Report sections">${tabs.map((t,i)=>`<button data-action="tab" data-tab="${t}" ${t===this.tab?'aria-current="page"':''}><span class="mono" aria-hidden="true">${String(i+1).padStart(2,'0')}</span>${t}</button>`).join('')}</nav></aside><div class="workspace"><section class="scrubber" aria-label="Synchronized timeline"><div class="scrubber-head"><strong id="scope-label"></strong><div>${button('Play timeline','play')}${button('Full match','reset-range')}</div></div><div class="range-controls"><label>Start frame<input id="range-start" type="number" min="0" max="${r.match.last_frame}" value="${this.start}"></label><label class="range-slider">Cursor <output id="cursor-label"></output><input id="cursor" aria-label="Timeline cursor" type="range" min="${this.start}" max="${this.end}" step="1" value="${this.cursor}"></label><label>End frame<input id="range-end" type="number" min="0" max="${r.match.last_frame}" value="${this.end}"></label></div><small>Shows start through cursor, inclusive. Timeline playback runs at 4x logic time.</small></section><div id="panel"></div></div></div>`;
  this.scopeLabels();this.renderPanel();this.sessionStatus();
 }
 private scopeLabels(){this.q('#scope-label').textContent=`${timeLabel(this.start)} - ${timeLabel(this.cursor)} / ${timeLabel(this.end)}`;this.q('#cursor-label').textContent=`frame ${this.cursor}`;const c=this.q<HTMLInputElement>('#cursor');c.min=String(this.start);c.max=String(this.end);c.value=String(this.cursor);this.q<HTMLInputElement>('#range-start').value=String(this.start);this.q<HTMLInputElement>('#range-end').value=String(this.end);}
 private updateScope(){this.scopeLabels();this.renderPanel();}
 private scoped(frame:number){return inRange(frame,this.start,this.cursor);}
 private scopedRows(data:Row[]){return data.filter(x=>this.scoped(Number(x.frame??x.record_frame??0)));}
 private renderPanel(){if(this.view!=='report')return;const r=this.report!, panel=this.q('#panel');
  this.q<HTMLElement>('.scrubber').hidden=this.tab==='Desync Lab';
  switch(this.tab){
   case 'Desync Lab':panel.innerHTML=renderDesync(r,!!this.options.offline,this.desyncIncident,this.desyncCheckpoint,this.diagnosticA,this.diagnosticB,this.diagnosticQuery);break;
   case 'Overview': {
    const commands=r.commands.items.filter(x=>this.scoped(x.frame));panel.innerHTML=`<div class="metrics">${metric('Commands',commands.length,'In selected interval')}${metric('Actions',commands.filter(x=>x.is_action).length,'Excludes configured control traffic')}${metric('Build requests',this.scopedRows(r.strategy.build_order).length,'Not completed production')}${metric('Recorded checkpoints',this.scopedRows(r.network.crc_checkpoints).length,'No engine-state recomputation')}</div><div class="two-col">${card('Actions over time','<div id="activity-plot"></div>','30-second buckets; selected interval')}${card('Participants',this.roster())}</div><div class="two-col">${card('Recorded streams',genericTable(r.channels,['id','name','record_count']))}${card('About these results',`<p>Player labels come from replay metadata. Valid structure does not prove file origin or successful actions.</p>${details('File identity',{...r.file,result:r.match.result})}`)}</div>`;this.drawActivity();break;
   }
   case 'Players':panel.innerHTML=card('Player activity',this.roster())+r.players.map(p=>card(p.name,details('Decoded player data',p))).join('');break;
   case 'Timeline':panel.innerHTML=card('Actions over time','<div id="activity-plot"></div>','Counts per 30-second bucket')+card('Events in the selected interval',genericTable(this.scopedRows([...r.strategy.build_order,...r.strategy.eliminations]).sort((a,b)=>Number(a.frame)-Number(b.frame)),['frame','time','player','event','asset']));this.drawActivity();break;
   case 'Commands':case 'Build requests':case 'Forensics':case 'Objects':this.renderTablePanel();break;
   case 'Camera':panel.innerHTML=card('Recorded camera positions',`<div class="filters"><label>Stream<select id="camera-stream"><option value="">All streams (separate traces)</option>${r.camera.streams.map(s=>`<option value="${Number(s.stream_id)}" ${String(s.stream_id)===this.cameraStream?'selected':''}>Stream ${Number(s.stream_id)}</option>`).join('')}</select></label><label>View<select id="camera-mode"><option value="path" ${this.cameraMode==='path'?'selected':''}>Recorded path</option><option value="density" ${this.cameraMode==='density'?'selected':''}>Sample density</option></select></label></div><div id="camera-plot"></div><p class="note">World X/Y positions. Density counts samples, not time or visibility. The marker shows the last sample at or before the cursor.</p>`)+card('Camera stream inventory',genericTable(r.camera.streams,['stream_id','update_count','position_update_count','rotation_update_count']));this.drawCamera();break;
   case 'Replay streams':panel.innerHTML=Object.entries(r.auxiliary_streams).map(([name,data])=>card(name[0].toUpperCase()+name.slice(1),`<div class="metrics compact">${Object.entries(data).filter(([,v])=>typeof v==='number').map(([k,v])=>metric(k.replaceAll('_',' '),v)).join('')}</div>${genericTable(this.scopedRows(rows(data.events)),name==='voice'?['record_frame','stream_id','serial','data_size']:name==='telestrator'?['record_frame','stream_id','operations']:['record_frame','channel','metadata_type','type_name'])}${details('Complete decoded stream',data)}`)).join('');break;
   case 'Network':panel.innerHTML=card('Recorded CRC checkpoints',genericTable(this.scopedRows(r.network.crc_checkpoints),['frame','epoch','status','source_count','agreement','crc_values']), 'Agreement compares recorded reports only')+card('Round-trip time',genericTable(this.scopedRows(r.network.rtt_points),['frame','time_seconds','average_ms','maximum_ms']), 'Announcement samples')+details('Complete network data',r.network);break;
   case 'Telemetry':panel.innerHTML=card('Experimental telemetry',`<p>Unverified samples. They cannot prove player visibility, valid spending or successful actions.</p>${r.telemetry.present?details('Decoded sidecar and correlations',r.telemetry):empty('No sidecar supplied. Add one under optional research inputs before analyzing a replay.')}`);break;
   case 'Findings':panel.innerHTML=card('Recorded evidence and diagnostics',`<p>${esc(r.cheat_analysis.disclaimer)}</p>`)+(r.cheat_analysis.findings.length?r.cheat_analysis.findings.map(f=>card(String(f.title??f.code??'Finding'),`<p>${esc(f.detail??f.description??f.summary??'')}</p>${details('Evidence and interpretation',f)}`)).join(''):empty('No findings. These checks cannot prove authenticity or cheating.'))+card('Coverage',r.cheat_analysis.scope.map(x=>`<article class="coverage"><h3>${esc(x.name)} ${tag(x.status)}</h3><p>${esc(x.detail)}</p></article>`).join(''));break;
   case 'Technical':panel.innerHTML=card('Replay structure',details('File and match',{file:r.file,match:r.match,observer:r.observer})+details('Integrity checks',r.integrity)+details('Decoded metadata',r.metadata)+details('Evidence and assumptions',r.evidence));break;
   case 'Compare':this.renderCompare();break;
  }
 }
 private roster(){const r=this.report!;return table(['Player','Source','Commands','Actions','Interval APM'],r.players.map(p=>{const all=r.commands.items.filter(c=>c.source_index===p.source_index&&this.scoped(c.frame));const count=all.filter(c=>c.is_action).length;return `<tr><td><strong>${esc(p.name)}</strong><small>Team ${esc(p.team??'-')}</small></td><td>${p.source_index}</td><td>${fmt(all.length)}</td><td>${fmt(count)}</td><td>${(count*900/Math.max(1,this.cursor-this.start+1)).toFixed(1)}</td></tr>`;}));}
 private renderTablePanel(){let filters='';const r=this.report!;
  if(this.tab==='Objects')filters=`<label class="field">Object ID (decimal or 0x hexadecimal)<input id="object-id" value="${esc(this.objectId)}" placeholder="e.g. 1042 or 0x412"></label><p class="note">Matches typed ObjectID references only. Ownership and lifecycle are not reconstructed.</p>`;
  else filters=`<div class="filters"><label class="search">Search ${this.tab.toLowerCase()}<input id="search" type="search" value="${esc(this.search)}" placeholder="Search names, arguments, hashes or offsets"></label>${this.tab!=='Forensics'?`<label>Player<select id="player-filter"><option value="">All sources</option>${[...new Set(r.commands.items.map(c=>c.source_index))].map(s=>`<option value="${s}" ${String(s)===this.player?'selected':''}>${esc(r.players.find(p=>p.source_index===s)?.name??`Source ${s}`)}</option>`).join('')}</select></label>`:''}${this.tab==='Commands'?`<label>Category<select id="category-filter"><option value="">All categories</option>${[...new Set(r.commands.items.map(c=>c.category))].map(c=>`<option ${c===this.category?'selected':''}>${esc(c)}</option>`).join('')}</select></label><label class="check"><input id="actions-filter" type="checkbox" ${this.actionsOnly?'checked':''}> Actions only</label>`:''}</div>`;
  this.q('#panel').innerHTML=card(this.tab,filters+'<div id="data-table"></div>');this.renderDataTable();
 }
 private commandRows(commands:Command[]):string[]{return commands.map(c=>`<tr><td>${timeLabel(c.frame)}<small>f${c.frame}</small></td><td>${esc(c.player)}<small>source ${c.source_index}</small></td><td><button class="text-button" data-action="inspect" data-index="${c.index}">${esc(c.display_name??c.name)}</button><small>${esc(c.name)}</small>${c.asset?`<small>${esc(c.asset.name)}</small>`:''}</td><td>${tag(c.category)}</td><td class="argument-cell">${esc(c.arguments.map(a=>`${a.label??a.type}: ${display(a.value)}`).join('; '))}</td></tr>`);}
 private renderDataTable(){const r=this.report!,query=this.search.toLowerCase();let data:unknown[]=[],headers:string[]=[],body:string[]=[];const at=this.page*100;
  if(this.tab==='Forensics'){
   const records=r.forensics.records.filter(x=>this.scoped(x.frame)&&`${x.offset} ${x.offset.toString(16)} ${x.frame} ${x.channel_name} ${x.payload_sha256}`.toLowerCase().includes(query));data=records;headers=['Record','Time','Channel','Bytes','Fingerprint'];body=records.slice(at,at+100).map(x=>`<tr><td><button class="text-button" data-action="record" data-index="${x.index}">#${x.index}</button><small>0x${x.offset.toString(16)}</small></td><td>${timeLabel(x.frame)}<small>f${x.frame}</small></td><td>${esc(x.channel_name)}</td><td>${x.payload_size}</td><td class="mono">${esc(x.payload_sha256.slice(0,20))}</td></tr>`);
  }else if(this.tab==='Build requests'){
   const events=r.strategy.build_order.filter(x=>this.scoped(Number(x.frame))&&(!this.player||String(x.source_index)===this.player)&&JSON.stringify(x).toLowerCase().includes(query));data=events;headers=['Frame','Player','Request','Asset','Producer'];body=events.slice(at,at+100).map(x=>`<tr><td>${timeLabel(Number(x.frame))}<small>f${x.frame}</small></td><td>${esc(x.player)}</td><td>${esc(x.event)}</td><td>${esc((x.asset as Row)?.name)}<small>${esc((x.asset as Row)?.hash)}</small></td><td>${esc(x.producer_object_id)}</td></tr>`);
  }else{
   let commands=r.commands.items;
   if(this.tab==='Objects'){if(!/^(?:0x[\da-f]+|\d+)$/i.test(this.objectId.trim())){this.q('#data-table').innerHTML=empty('Enter a valid ObjectID to find its recorded references.');return;}const id=Number(this.objectId);if(!Number.isSafeInteger(id)||id<0||id>0xffffffff){this.q('#data-table').innerHTML=empty('ObjectID must be an unsigned 32-bit integer.');return;}commands=objectReferences(commands,id);}
   commands=commands.filter(c=>this.scoped(c.frame)&&(this.tab==='Objects'||((!this.player||String(c.source_index)===this.player)&&(!this.category||c.category===this.category)&&(!this.actionsOnly||c.is_action)&&`${c.name} ${c.display_name??''} ${c.meaning??''} ${c.player} ${JSON.stringify(c.arguments)} ${c.asset?.name??''}`.toLowerCase().includes(query))));data=commands;headers=['Time','Player','Command','Category','Typed arguments'];body=this.commandRows(commands.slice(at,at+100));
  }
  if(at>=data.length&&this.page>0){this.page=0;this.renderDataTable();return;}
  this.q('#data-table').innerHTML=table(headers,body)+`<div class="pagination"><span>${data.length?at+1:0}-${Math.min(at+100,data.length)} of ${fmt(data.length)}</span><div>${button('Previous','previous','quiet',this.page===0?'disabled':'')}${button('Next','next','quiet',at+100>=data.length?'disabled':'')}</div></div>`;
 }
 private async inspect(index:number){const c=this.report!.commands.items[index];if(!c)return;const player=this.report!.players.find(p=>p.source_index===c.source_index);this.q('#inspector').innerHTML=`<div class="dialog-head"><div><p class="eyebrow">COMMAND / ${c.index}</p><h2 id="inspector-title">${esc(c.display_name??c.name)}</h2><small>${esc(c.name)}</small></div>${button('Close','close-inspector')}</div><div class="metrics compact">${metric('Frame',c.frame)}${metric('Command ID',c.message_type)}${metric('Source',c.source_index)}${metric('Byte offset','0x'+c.wire_offset.toString(16))}</div><p>Player: ${esc(c.player)}. Mapping: ${esc(display(player?.source_mapping??'Unresolved source'))}.</p><p>${esc(c.meaning??'Argument meanings have not been verified.')}</p>${(c.schema_issues??[]).map(x=>`<p class="notice error">${esc(x)}</p>`).join('')}${details('Meaning evidence',c.semantic_evidence??{})}${table(['Argument / type','Recorded value','Explore'],c.arguments.map(a=>`<tr><td>${esc(a.label??'Unlabeled')}<small>${esc(a.type)} / ${esc(a.label_status??'unknown')}</small></td><td>${esc(display(a.value))}</td><td>${a.type==='object_id'?button('Find references','object','quiet',`data-id="${Number(a.value)}"`):''}</td></tr>`))}<p class="mono">Record #${c.record_index}; ${c.wire_size} command bytes. SHA-256 ${esc(c.wire_sha256)}</p><h3>Raw command bytes</h3><pre id="raw-bytes">Loading...</pre>${details('Containing record',this.report!.forensics.records[c.record_index])}`;this.showDialog();await this.rawBytes(c.wire_offset,c.wire_size);}
 private async inspectRecord(index:number){const r=this.report!.forensics.records[index];this.q('#inspector').innerHTML=`<div class="dialog-head"><h2 id="inspector-title">Record #${index}</h2>${button('Close','close-inspector')}</div>${json(r)}<h3>Payload bytes</h3><pre id="raw-bytes">Loading...</pre>`;this.showDialog();await this.rawBytes(r.offset+13,r.payload_size);}
 private showDialog(){const d=this.q<HTMLDialogElement>('dialog');if(!d.open)d.showModal();}
 private async rawBytes(offset:number,size:number){const token=++this.rawToken;const output=this.q('#raw-bytes');if(!this.file){output.textContent='Original bytes are not included in this report. Open the source replay to inspect raw bytes.';return;}const data=new Uint8Array(await this.file.slice(offset,offset+size).arrayBuffer());if(token!==this.rawToken)return;const lines=[];for(let i=0;i<Math.min(data.length,4096);i+=16)lines.push((offset+i).toString(16).padStart(8,'0')+'  '+Array.from(data.slice(i,i+16),x=>x.toString(16).padStart(2,'0')).join(' '));output.textContent=lines.join('\n')+(data.length>4096?'\n[Preview limited to 4,096 bytes]':'');}
 private drawActivity(){const r=this.report!,buckets=actionBuckets(r,this.start,this.cursor);if(!buckets.length){this.q('#activity-plot').innerHTML=empty('No actions in this interval.');return;}const sources=[...new Set(r.commands.items.filter(c=>c.is_action).map(c=>c.source_index))].slice(0,8);const max=Math.max(1,...buckets.flatMap(([,b])=>[...b.values()]));const width=Math.max(280,this.q('#activity-plot').clientWidth),height=220,x=(f:number)=>40+(f-this.start)/Math.max(1,this.cursor-this.start)*(width-68),y=(v:number)=>height-28-v/max*(height-50);
  const lines=sources.map((source,i)=>`<polyline points="${buckets.map(([f,b])=>`${x(Math.max(this.start,f))},${y(b.get(source)??0)}`).join(' ')}" stroke="var(--chart-${i<8?i+1:1})" stroke-dasharray="${i%2?'5 3':'none'}" fill="none" stroke-width="2"/>${buckets.length===1?`<circle cx="${x(Math.max(this.start,buckets[0][0]))}" cy="${y(buckets[0][1].get(source)??0)}" r="4" fill="var(--chart-${i+1})"/>`:''}`).join('');this.q('#activity-plot').innerHTML=`<svg class="plot" viewBox="0 0 ${width} ${height}" role="img" aria-label="Action counts by player, 30 second buckets"><line x1="40" x2="${width-28}" y1="${height-28}" y2="${height-28}" stroke="var(--rule-strong)"/><text x="4" y="26">${max}</text>${lines}<text x="40" y="216">${timeLabel(this.start)}</text><text x="${width-65}" y="216">${timeLabel(this.cursor)}</text></svg><div class="legend">${sources.map((s,i)=>`<span><i style="background:var(--chart-${i<8?i+1:1})"></i>${esc(r.players.find(p=>p.source_index===s)?.name??`Source ${s}`)}</span>`).join('')}</div>`;
 }
 private drawCamera(){const all=this.report!.camera.samples.filter(p=>(!this.cameraStream||String(p.stream_id)===this.cameraStream));const points=all.filter(p=>this.scoped(p.frame));const container=this.q('#camera-plot');if(!points.length){container.innerHTML=empty('No camera samples in this interval.');return;}let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;for(const p of all){minX=Math.min(minX,p.x);maxX=Math.max(maxX,p.x);minY=Math.min(minY,p.y);maxY=Math.max(maxY,p.y);}const scale=Math.min(800/Math.max(1,maxX-minX),320/Math.max(1,maxY-minY));const x=(v:number)=>40+(800-(maxX-minX)*scale)/2+(v-minX)*scale,y=(v:number)=>340-(320-(maxY-minY)*scale)/2-(v-minY)*scale;
  const groups=new Map<number,CameraPoint[]>();for(const p of points){const list=groups.get(p.stream_id)??[];list.push(p);groups.set(p.stream_id,list);}let marks='';
  if(this.cameraMode==='density'){const cells=new Map<string,number>();for(const p of points){const key=`${Math.floor(x(p.x)/20)*20},${Math.floor(y(p.y)/20)*20}`;cells.set(key,(cells.get(key)??0)+1);}const max=Math.max(...cells.values());marks=[...cells].map(([key,n])=>{const [cx,cy]=key.split(',');return `<rect x="${cx}" y="${cy}" width="20" height="20" fill="var(--chart-seq-500)" opacity="${0.15+0.85*n/max}"><title>${n} recorded samples</title></rect>`;}).join('');
  }else{marks=[...groups].map(([id,ps])=>{const step=Math.max(1,Math.ceil(ps.length/3000));const visible=ps.filter((_,i)=>i%step===0||i===ps.length-1);const last=ps.at(-1)!;return `<polyline points="${visible.map(p=>`${x(p.x)},${y(p.y)}`).join(' ')}" fill="none" stroke="var(--chart-1)" stroke-width="1.5" opacity=".65"/><circle cx="${x(last.x)}" cy="${y(last.y)}" r="5" fill="var(--brand)"/><text x="${Math.min(790,x(last.x)+8)}" y="${Math.max(15,y(last.y)-8)}">Stream ${id}</text>`;}).join('');}
  container.innerHTML=`<svg class="camera-plot" viewBox="0 0 880 380" role="img" aria-label="Recorded camera ${this.cameraMode}"><rect x="20" y="10" width="840" height="345" fill="var(--panel)"/>${marks}<text x="20" y="375">X ${minX.toFixed(1)} ... ${maxX.toFixed(1)} / Y ${minY.toFixed(1)} ... ${maxY.toFixed(1)}</text></svg><p class="note">${fmt(points.length)} samples in ${groups.size} streams. ${this.cameraMode==='path'?'Paths are reduced only for drawing; full samples remain in JSON.':'Darker cells contain more samples.'}</p>`;
 }
 private async save(report:ReplayReport,file?:Blob){return saveReplay({hash:report.file.sha256,report,file,savedAt:new Date().toISOString(),bytes:new Blob([JSON.stringify(report)]).size+(file?.size??0)});}
 private async saveCurrent(){await this.task(async()=>{const duplicate=await this.save(this.report!,this.file);this.message(duplicate?'Existing library entry updated (same file hash).':'Saved in this browser. Export reports to keep a portable backup.');});}
 private renderLibrary(){this.q('#content').innerHTML=`<div class="report-title"><div><p class="eyebrow">LOCAL COLLECTION / 1.02</p><h1>Replay library.</h1><p>Saved reports and source files in this browser. Originals stay unchanged.</p></div>${!this.options.offline?`<label class="button primary">Import replays<input id="library-import" data-upload type="file" multiple accept=".KWReplay,.kwreplay" class="file-overlay" aria-label="Import replays"></label>`:''}</div><div class="metrics">${metric('Saved replays',this.saved.length)}${metric('Storage',(this.saved.reduce((n,x)=>n+x.bytes,0)/1024/1024).toFixed(1)+' MiB')}${metric('Limits','50 / 256 MiB','Files / total stored bytes')}</div>${card('Your collection',`<div class="filters"><label class="search">Search<input id="library-search" type="search" value="${esc(this.libraryQuery)}" placeholder="Player, map, title or filename"></label><label>Map<select id="library-map"><option value="">All maps</option>${[...new Set(this.saved.map(x=>x.report.match.map_name))].map(m=>`<option ${m===this.libraryMap?'selected':''}>${esc(m)}</option>`).join('')}</select></label><label>Recorded date<input id="library-date" type="date" value="${esc(this.libraryDate)}"></label><label>Sort<select id="library-sort"><option value="newest" ${this.librarySort==='newest'?'selected':''}>Recently saved</option><option value="title" ${this.librarySort==='title'?'selected':''}>Title</option><option value="duration" ${this.librarySort==='duration'?'selected':''}>Longest match</option></select></label></div><div id="library-rows"></div>`)}<p class="note">1.02 only. Duplicate files update existing entries. Browser storage can be cleared; export reports you want to keep.</p>`;this.renderLibraryRows();this.setBusy();}
 private renderLibraryRows(){const data=this.saved.filter(x=>{const r=x.report;return (!this.libraryMap||r.match.map_name===this.libraryMap)&&(!this.libraryDate||recordedDate(r.match.recorded_at)===this.libraryDate)&&`${r.file.name} ${r.match.title} ${r.match.map_name} ${r.players.map(p=>p.name).join(' ')}`.toLowerCase().includes(this.libraryQuery.toLowerCase());}).sort((a,b)=>this.librarySort==='title'?a.report.match.title.localeCompare(b.report.match.title):this.librarySort==='duration'?b.report.match.duration_seconds-a.report.match.duration_seconds:b.savedAt.localeCompare(a.savedAt));this.q('#library-rows').innerHTML=table(['Replay','Players','Duration','Saved','Actions'],data.map(x=>`<tr><td><strong>${esc(x.report.match.title)}</strong><small>${esc(x.report.match.map_name)} / ${esc(x.report.file.name)}</small></td><td>${esc(x.report.players.map(p=>p.name).join(', '))}</td><td>${esc(x.report.match.duration)}</td><td>${esc(x.savedAt.slice(0,10))}</td><td class="row-actions">${button('Open','open-saved','quiet',`data-hash="${x.hash}"`)}${this.report?button('Compare','compare-saved','quiet',`data-hash="${x.hash}"`):''}${button('Remove','remove-saved','quiet',`data-hash="${x.hash}"`)}</td></tr>`));}
 private renderCompare(){const a=this.report!,b=this.comparison;let body=`<p>Compares full recordings in command order. Time filters do not apply; source IDs and order affect matching.</p><div class="filters">${!this.options.offline?`<label class="button primary">Choose comparison replay<input id="compare-input" data-upload type="file" accept=".KWReplay,.kwreplay" class="file-overlay" aria-label="Choose comparison replay"></label>${button('Choose from library','library')}`:''}</div>`;
  if(!b){this.q('#panel').innerHTML=card('Compare recordings',body+empty(this.options.offline?'Comparison data is not included in this exported single-replay report.':'Choose another 1.02 replay to compare.'));return;}
  const comparison=compareReports(a,b);body+=`<div class="comparison-names"><strong>A / ${esc(a.file.name)}</strong><strong>B / ${esc(b.file.name)}</strong></div><p class="notice">${comparison.byteIdentical?'Identical source bytes.':comparison.sameSession?'Matching declared session, map and seed. This is a metadata match, not authentication.':'Different or unconfirmed session. Use for opening/activity comparison; differences are not evidence of desynchronization.'}</p>${table(['Metric','A','B','B minus A'],comparison.metrics.map(([label,l,r])=>`<tr><td>${esc(label)}</td><td>${esc(fmt(l))}</td><td>${esc(fmt(r))}</td><td>${(Number(r)-Number(l)).toFixed(2)}</td></tr>`))}`;
  const first=comparison.firstCommandDifference;body+=card('First command-sequence difference',first?`<p>Sequence index ${first.index}. Matching is frame plus SHA-256 of encoded command bytes.</p><div class="two-col">${details('A command',first.left)}${details('B command',first.right)}</div>`:empty('Decoded command sequences match exactly by frame and encoded bytes.'));
  body+=card('Recorded CRC comparison',genericTable(comparison.crcDifferences,['frame','epoch','player','left','right'])+`<p class="note">${comparison.missingLeft.length} checkpoint frames missing from A; ${comparison.missingRight.length} missing from B. No engine CRC is recomputed.</p>`+details('Missing checkpoint frames',{missingFromA:comparison.missingLeft,missingFromB:comparison.missingRight}));
  body+=card('Opening requests: first five logic minutes',`<div class="two-col"><div><h3>A</h3>${genericTable(a.strategy.build_order.filter(x=>Number(x.frame)<=4500),['frame','player','event','asset'])}</div><div><h3>B</h3>${genericTable(b.strategy.build_order.filter(x=>Number(x.frame)<=4500),['frame','player','event','asset'])}</div></div>`);this.q('#panel').innerHTML=card('Compare recordings',body);this.setBusy();
 }
 private download(data:string,name:string,type:string){const blob=new Blob([data],{type});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=(this.report?.file.name.replace(/\.[^.]+$/,'')??'replay')+'-'+name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
 private async exportHtml(){const response=await fetch(new URL('portable.js',this.assetsBase));if(!response.ok)throw new Error('Portable report runtime is missing. Rebuild the frontend.');const runtime=await response.text();const payload=JSON.stringify(this.report).replaceAll('<','\\u003c').replaceAll('\u2028','\\u2028').replaceAll('\u2029','\\u2029');const html=`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KW Replay Lab - offline report</title><style>body{margin:0}</style></head><body><div id="replay-lab"></div><script type="application/json" id="replay-data">${payload}</script><script>${runtime.replace(/<\/script/gi,'<\\/script')}</script></body></html>`;this.download(html,'report.html','text/html');this.message('HTML exported with this report and its Desync Lab case. Raw file bytes are excluded.');}
}
export function mountReplayLab(host:HTMLElement,options:MountOptions={},assetsBase=''){const instance=new ReplayLab(host,options,assetsBase);return {destroy:()=>instance.destroy()};}
