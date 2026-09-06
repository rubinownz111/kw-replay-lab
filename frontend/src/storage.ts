import type {SavedReplay, ReplayReport, Row} from './types';
const MAX_BYTES=256*1024*1024, MAX_ITEMS=50;
async function open(): Promise<IDBDatabase> {
 return new Promise((resolve,reject)=>{const request=indexedDB.open('kw-replay-lab-v1',2);request.onupgradeneeded=()=>{const db=request.result;if(!db.objectStoreNames.contains('replays'))db.createObjectStore('replays',{keyPath:'hash'});if(!db.objectStoreNames.contains('workspace'))db.createObjectStore('workspace');};request.onsuccess=()=>{request.result.onversionchange=()=>request.result.close();resolve(request.result);};request.onerror=()=>reject(new Error('Browser storage is unavailable. You can still analyze and export.'));});
}
export interface ActiveWorkspace {report:ReplayReport; file?:Blob; comparison:ReplayReport|null; position:Row}
export async function loadWorkspace():Promise<ActiveWorkspace|null> {
 const db=await open();try{return await new Promise((resolve,reject)=>{
  const tx=db.transaction('workspace'),store=tx.objectStore('workspace'),active=store.get('active'),position=store.get('position');
  tx.oncomplete=()=>resolve(active.result?{...active.result,position:position.result??{}}:null);tx.onerror=()=>reject(tx.error);
 });}finally{db.close();}
}
export async function writeWorkspace(position:Row,active?:Omit<ActiveWorkspace,'position'>|null):Promise<void> {
 const db=await open();try{await new Promise<void>((resolve,reject)=>{
  const tx=db.transaction('workspace','readwrite'),store=tx.objectStore('workspace');
  if(active===null)store.clear();
  else {store.put(position,'position');if(active!==undefined)store.put(active,'active');}
  tx.oncomplete=()=>resolve();tx.onabort=tx.onerror=()=>reject(new Error('Could not keep the current replay in browser storage. Export or save it before leaving this page.'));
 });}finally{db.close();}
}
export async function listSaved(): Promise<SavedReplay[]> {
 const db=await open();try{return await new Promise((resolve,reject)=>{const r=db.transaction('replays').objectStore('replays').getAll();r.onsuccess=()=>resolve(r.result as SavedReplay[]);r.onerror=()=>reject(r.error);});}finally{db.close();}
}
export async function saveReplay(item: SavedReplay): Promise<boolean> {
 const db=await open();
 try{return await new Promise((resolve,reject)=>{
  const tx=db.transaction('replays','readwrite'), store=tx.objectStore('replays');let duplicate=false;
  tx.oncomplete=()=>resolve(duplicate);tx.onerror=()=>reject(new Error('Could not save: browser storage quota or transaction failure. Export a report instead.'));
  const request=store.getAll();request.onsuccess=()=>{const existing=request.result as SavedReplay[];duplicate=existing.some(x=>x.hash===item.hash);const kept=existing.filter(x=>x.hash!==item.hash);
   if(kept.length>=MAX_ITEMS||kept.reduce((n,x)=>n+x.bytes,0)+item.bytes>MAX_BYTES){tx.abort();reject(new Error('Library limit reached (50 replays / 256 MiB). Remove saved copies before importing more.'));return;}
   store.put(item);
  };
 });}finally{db.close();}
}
export async function removeReplay(hash: string): Promise<void> {
 const db=await open();try{await new Promise<void>((resolve,reject)=>{const tx=db.transaction('replays','readwrite');tx.objectStore('replays').delete(hash);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);});}finally{db.close();}
}
