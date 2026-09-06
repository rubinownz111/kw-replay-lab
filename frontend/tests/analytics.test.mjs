import test from 'node:test';
import assert from 'node:assert/strict';
import {compareReports,objectReferences,inRange,csv,escapeHtml,actionBuckets,recordedDate} from '../../.artifacts/analytics.mjs';
const command={index:0,frame:30,source_index:3,wire_sha256:'a',is_action:true,arguments:[{type:'object_id',value:204},{type:'integer',value:999}]};
function report(){return {file:{sha256:'source-a'},match:{game_session_id:'match',map_id:'map',seed:1,duration_seconds:2},commands:{items:[structuredClone(command)]},summary:{action_count:1,decode_error_count:0},strategy:{build_order:[]},network:{crc_checkpoints:[{frame:30,crc_values:['a','b']}]}};}
test('object search uses exact typed IDs, never generic integers',()=>{assert.equal(objectReferences([command],204).length,1);assert.equal(objectReferences([command],999).length,0);assert.equal(objectReferences([command],20).length,0);});
test('inclusive time boundaries and action aggregation',()=>{assert.ok(inRange(30,30,30));assert.ok(!inRange(29,30,50));const r=report();assert.equal(actionBuckets(r,0,29).length,0);assert.equal(actionBuckets(r,30,30)[0][1].get(3),1);r.commands.items.push({...command,frame:930});assert.equal(actionBuckets(r,0,1000).length,3);assert.equal(actionBuckets(r,0,1000)[1][1].size,0);});
test('identical reports and reordered CRC sets match',()=>{const a=report(),b=report();b.network.crc_checkpoints[0].crc_values.reverse();const c=compareReports(a,b);assert.ok(c.sameSession);assert.ok(c.byteIdentical);assert.ok(c.commandSequencesEqual);assert.equal(c.crcDifferences.length,0);});
test('first differing command includes frame and encoded hash',()=>{const a=report(),b=report();b.commands.items[0].wire_sha256='b';b.file.sha256='source-b';const c=compareReports(a,b);assert.equal(c.firstCommandDifference.index,0);assert.equal(c.byteIdentical,false);b.commands.items[0].wire_sha256='a';b.commands.items[0].frame=31;assert.ok(compareReports(a,b).firstCommandDifference);});
test('truncated sequences and missing CRCs stay distinct',()=>{const a=report(),b=report();b.commands.items=[];b.network.crc_checkpoints=[];const c=compareReports(a,b);assert.equal(c.firstCommandDifference.right,null);assert.deepEqual(c.missingRight,[30]);assert.equal(c.crcDifferences.length,0);});
test('different sessions never imply a desync',()=>{const a=report(),b=report();b.match.seed=2;assert.equal(compareReports(a,b).sameSession,false);});
test('CSV and HTML escape hostile strings',()=>{assert.match(csv([{name:'=SUM(1,2)'}],['name']),/"'=SUM/);assert.equal(escapeHtml('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');});

test('structured replay dates produce sortable ISO days',()=>{assert.equal(recordedDate({year:2026,month:7,day:2}),'2026-07-02');assert.equal(recordedDate(null),'');});
