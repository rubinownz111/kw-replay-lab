import {build} from 'esbuild';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'static/replay-lab');
await mkdir(out,{recursive:true});
const cssPlugin={name:'inline-css',setup(b){b.onLoad({filter:/\.css$/},async args=>{
 return {contents:await readFile(args.path,'utf8'),loader:'text'};
});}};
const shared={bundle:true,target:'es2022',minify:true,legalComments:'eof',plugins:[cssPlugin]};
await build({...shared,entryPoints:[path.join(root,'frontend/src/index.ts')],outfile:path.join(out,'replay-lab.js'),format:'esm'});
await build({...shared,entryPoints:[path.join(root,'frontend/src/portable.ts')],outfile:path.join(out,'portable.js'),format:'iife'});
await build({...shared,entryPoints:[path.join(root,'frontend/src/analytics.ts')],outfile:path.join(root,'.artifacts/analytics.mjs'),format:'esm'});
console.log('Built Replay Lab and offline reports.');
