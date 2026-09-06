import type {ReplayReport, Command, Diagnostic, DiagnosticField} from './types';

export interface CommandDifference {kind:string;frame:number;player:string;left:Command|null;right:Command|null}
export function compareDesync(a:ReplayReport,b:ReplayReport) {
 const conflicts:string[]=[];
 const identities=['game_session_id','map_id','map_crc','seed'] as const;
 for(const key of identities)if(a.match[key]!=null&&b.match[key]!=null&&a.match[key]!==b.match[key])conflicts.push(key);
 const knownSession=a.match.game_session_id!=null&&a.match.game_session_id!==''&&a.match.game_session_id!==0;
 const completeIdentity=identities.every(k=>a.match[k]!=null&&a.match[k]!==''&&b.match[k]!=null&&b.match[k]!=='');
 const sameSession=completeIdentity&&knownSession&&a.match.game_session_id===b.match.game_session_id&&conflicts.length===0;
 // Only unique matching roster names can bridge source numbering across recordings.
 const playerKey=(r:ReplayReport,c:{source_index:number})=>{
  const p=r.players.find(p=>p.source_index===c.source_index);
  const name=p?.name;
  return name&&a.players.filter(p=>p.name===name).length===1&&b.players.filter(p=>p.name===name).length===1?`player:${name}`:`source:${c.source_index}`;
 };
 const crcRows=new Map<string,{frame:number;epoch:number;player:string;left:Set<string>;right:Set<string>}>();
 for(const [r,side] of [[a,'left'],[b,'right']] as const)for(const cp of r.desync?.checkpoints??[])for(const sample of cp.reports){
  const player=playerKey(r,sample),key=`${cp.epoch}/${cp.frame}/${player}`;
  const row=crcRows.get(key)??{frame:cp.frame,epoch:cp.epoch,player,left:new Set<string>(),right:new Set<string>()};
  row[side].add(`${sample.playback?'playback':'live'}:0x${sample.crc.toString(16).padStart(8,'0')}`);crcRows.set(key,row);
 }
 const crcDifferences=[...crcRows.values()].filter(x=>JSON.stringify([...x.left].sort())!==JSON.stringify([...x.right].sort())).map(x=>({...x,left:[...x.left],right:[...x.right]}));
 const ignored=new Set([609,655,652,653,606]);
 const ac=a.commands.items.filter(c=>!ignored.has(c.message_type)),bc=b.commands.items.filter(c=>!ignored.has(c.message_type));
 const signature=(c:Command)=>`${c.frame}:${c.message_type}:${c.argument_sha256??JSON.stringify(c.arguments.map(a=>[a.type,a.value]))}`;
 const lanes=new Map<string,{left:Command[];right:Command[]}>();
 for(const [r,cs,side] of [[a,ac,'left'],[b,bc,'right']] as const)for(const c of cs){const key=playerKey(r,c),lane=lanes.get(key)??{left:[],right:[]};lane[side].push(c);lanes.set(key,lane);}
 const differences:CommandDifference[]=[];let differenceCount=0;
 for(const [player,{left,right}] of lanes){
  let i=0,j=0,retained=0;
  const add=(kind:string,l:Command|null,r:Command|null)=>{differenceCount++;if(retained++<500)differences.push({kind,frame:Math.min(l?.frame??Infinity,r?.frame??Infinity),player,left:l,right:r});};
  while(i<left.length||j<right.length){
   if(!left[i]){add('Only in B',null,right[j++]);continue;}
   if(!right[j]){add('Only in A',left[i++],null);continue;}
   if(signature(left[i])===signature(right[j])){i++;j++;continue;}
   let aheadA=-1,aheadB=-1;
   for(let n=1;n<=32;n++){if(aheadA<0&&left[i+n]&&signature(left[i+n])===signature(right[j]))aheadA=n;if(aheadB<0&&right[j+n]&&signature(left[i])===signature(right[j+n]))aheadB=n;}
   if(aheadA>0&&(aheadB<0||aheadA<=aheadB)){for(let n=0;n<aheadA;n++)add('Only in A',left[i++],null);}
   else if(aheadB>0){for(let n=0;n<aheadB;n++)add('Only in B',null,right[j++]);}
   else if(left[i].frame<right[j].frame)add('Only in A',left[i++],null);
   else if(right[j].frame<left[i].frame)add('Only in B',null,right[j++]);
   else add('Changed',left[i++],right[j++]);
  }
 }
 differences.sort((l,r)=>l.frame-r.frame);
 const orderDiffers=ac.length!==bc.length||ac.some((c,i)=>!bc[i]||`${playerKey(a,c)}:${signature(c)}`!==`${playerKey(b,bc[i])}:${signature(bc[i])}`);
 const unknownSource=[a,b].some(r=>r.commands.items.some(c=>playerKey(r,c).startsWith('source:')));
 return {sameSession,conflicts,unknownSource,crcDifferences,differences:differences.slice(0,500),differenceCount,orderDiffers,
  firstDifference:differences[0]??null,alignment:'Per-player command order with up to 32 commands of resynchronization. Network reports and replay-camera commands are excluded.'};
}

export function compareDiagnostics(a:Diagnostic,b:Diagnostic) {
 const left=new Map(a.fields.map(f=>[f.path,f])),right=new Map(b.fields.map(f=>[f.path,f]));
 const differences:{path:string;left:DiagnosticField|null;right:DiagnosticField|null}[]=[];let total=0;
 for(const path of new Set([...left.keys(),...right.keys()])){const l=left.get(path),r=right.get(path);if(!l||!r||l.type!==r.type||l.value_sha256!==r.value_sha256){total++;if(differences.length<1000)differences.push({path,left:l??null,right:r??null});}}
 const settings=[...new Set([...Object.keys(a.settings),...Object.keys(b.settings)])].filter(k=>JSON.stringify(a.settings[k])!==JSON.stringify(b.settings[k])).map(k=>({setting:k,left:a.settings[k]??'Not recorded',right:b.settings[k]??'Not recorded'}));
 const warnings=[];
 if(a.kind!==b.kind)warnings.push('Different formats. Compare text with text or binary with binary.');
 if(a.frame==null||b.frame==null)warnings.push('One or both capture frames are unknown.');
 else if(a.frame!==b.frame)warnings.push('Different capture frames. State changes between frames are expected.');
 if(!a.complete||!b.complete)warnings.push('At least one capture is partial; missing fields may reflect incomplete decoding.');
 if(a.forced||b.forced)warnings.push('A report marks this as an intentionally forced diagnostic.');
 if(a.format_version!==b.format_version)warnings.push('Capture format versions differ.');
 if(settings.length)warnings.push('Recorded settings differ. Review them before interpreting state differences.');
 return {differences,total,settings,warnings};
}

export function caseMarkdown(r:ReplayReport,diagnosticA=0,diagnosticB=1):string {
 const d=r.desync,first=d?.incidents[0],c=r.desync_case;
 const safe=(v:unknown)=>String(v??'Unknown').replace(/[\r\n`<>]/g,' ').replace(/([\\*_\[\]#|])/g,'\\$1');
 const lines=['# Desync Lab case','',`Replay: ${safe(r.file.name)}`,`SHA-256: ${r.file.sha256}`,`Result: ${safe(d?.title)}`,''];
 if(first)lines.push(`Last matching checkpoint: ${first.last_matching_frame??'None'}`,`First incident: ${first.frame}`,`Inspect frames: ${first.start_frame} to ${first.frame}`,`Action commands in window: ${first.command_count}`,'');
 for(const peer of c?.peers??[]){const x=compareDesync(r,peer);lines.push(`Comparison: ${safe(peer.file.name)} (${peer.file.sha256})`,`Session metadata matches: ${x.sameSession}`,`Command differences: ${x.differenceCount}`,`Player/checkpoint differences: ${x.crcDifferences.length}`,'');}
 for(const doc of c?.diagnostics??[])lines.push(`Capture: ${safe(doc.name)} (${doc.sha256})`,`Frame: ${doc.frame??'Unknown'}; fields: ${doc.fields.length}; complete: ${doc.complete}; forced: ${doc.forced}`,'');
 if((c?.diagnostics.length??0)>=2){const a=c!.diagnostics[diagnosticA]??c!.diagnostics[0],b=c!.diagnostics[diagnosticB]??c!.diagnostics[1],x=compareDiagnostics(a,b);lines.push(`Capture comparison: ${safe(a.name)} versus ${safe(b.name)}`,`First capture difference: ${safe(x.differences[0]?.path??'None')}`,`Capture differences: ${x.total}`,...x.warnings.map(safe),'');}
 lines.push('## Notes','',(c?.notes??'').split(/\r?\n/).map(safe).join('\n'),'','Recorded evidence is not proof of root cause or player fault. Diagnostic association is user supplied.');
 return lines.join('\n');
}
