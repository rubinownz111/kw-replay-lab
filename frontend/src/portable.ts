import {mountReplayLab} from './main';
import {validateReport} from './types';
const report=JSON.parse(document.getElementById('replay-data')!.textContent!);
validateReport(report);
mountReplayLab(document.getElementById('replay-lab')!,{report,offline:true},'');
