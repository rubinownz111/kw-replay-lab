import type {Command, ReplayReport, Row} from './types';
export const escapeHtml = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
export const display = (v: unknown): string => v == null ? '-' : typeof v === 'object' ? JSON.stringify(v) : String(v);
export const inRange = (frame: number, start: number, end: number) => frame >= start && frame <= end;
export const timeLabel = (frame: number): string => {const s = Math.floor(frame / 15); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
export function recordedDate(value: unknown): string {
 if(typeof value==='string')return value.slice(0,10);
 if(!value||typeof value!=='object')return '';
 const d=value as Row;
 return Number(d.year)>0&&Number(d.month)>0&&Number(d.day)>0?`${d.year}-${String(d.month).padStart(2,'0')}-${String(d.day).padStart(2,'0')}`:'';
}
export function objectReferences(commands: Command[], id: number): Command[] {
 return commands.filter(c => c.arguments.some(a => a.type === 'object_id' && a.value === id));
}
export function compareReports(a: ReplayReport, b: ReplayReport) {
 const session = a.match.game_session_id;
 const sameSession = session != null && session !== '' && session !== 0 && session === b.match.game_session_id && a.match.map_id === b.match.map_id && a.match.seed === b.match.seed;
 const ac = a.commands.items, bc = b.commands.items;
 let firstCommandDifference: {index: number; left: Command | null; right: Command | null} | null = null;
 for(let i=0;i<Math.max(ac.length,bc.length);i++) {
  if (!ac[i] || !bc[i] || ac[i].frame !== bc[i].frame || ac[i].wire_sha256 !== bc[i].wire_sha256) {firstCommandDifference={index:i,left:ac[i]??null,right:bc[i]??null};break;}
 }
 const checkpoints = new Map(b.network.crc_checkpoints.map(r=>[Number(r.frame),r]));
 const crcDifferences: Row[] = [], missingLeft: number[] = [], missingRight: number[] = [];
 const framesA = new Set(a.network.crc_checkpoints.map(r=>Number(r.frame)));
 for(const l of a.network.crc_checkpoints) {
  const r=checkpoints.get(Number(l.frame));
  if (!r) {missingRight.push(Number(l.frame));continue;}
  const normalize=(x: Row)=>JSON.stringify([...(x.crc_values as string[] ?? [])].sort());
  if(normalize(l)!==normalize(r)) crcDifferences.push({frame:l.frame,left:l.crc_values,right:r.crc_values});
 }
 for(const r of b.network.crc_checkpoints) if(!framesA.has(Number(r.frame))) missingLeft.push(Number(r.frame));
 return {sameSession, byteIdentical:a.file.sha256===b.file.sha256, commandSequencesEqual:!firstCommandDifference, firstCommandDifference, crcDifferences, missingLeft, missingRight,
  metrics:[['Duration (logic seconds)',a.match.duration_seconds,b.match.duration_seconds],['Commands',ac.length,bc.length],['Actions',a.summary.action_count,b.summary.action_count],['Build requests',a.strategy.build_order.length,b.strategy.build_order.length],['Decode errors',a.summary.decode_error_count,b.summary.decode_error_count]]};
}
export function csv(rows: Record<string,unknown>[], columns: string[]): string {
 const cell=(v: unknown)=>{let s=display(v);if(/^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
 return '\ufeff'+[columns.map(cell).join(','),...rows.map(r=>columns.map(k=>cell(r[k])).join(','))].join('\r\n');
}
export function actionBuckets(report: ReplayReport, start: number, end: number, bucketFrames=450) {
 const buckets=new Map<number,Map<number,number>>();
 for(const c of report.commands.items) if(c.is_action && inRange(c.frame,start,end)) {const f=Math.floor(c.frame/bucketFrames)*bucketFrames;const b=buckets.get(f)??new Map<number,number>();b.set(c.source_index,(b.get(c.source_index)??0)+1);buckets.set(f,b);}
 if(buckets.size)for(let frame=Math.floor(start/bucketFrames)*bucketFrames;frame<=end;frame+=bucketFrames)if(!buckets.has(frame))buckets.set(frame,new Map());
 return [...buckets.entries()].sort((a,b)=>a[0]-b[0]);
}
