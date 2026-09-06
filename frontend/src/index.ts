import {mountReplayLab as mount} from './main';
import type {MountOptions} from './types';
export type {MountOptions, ReplayReport, Command} from './types';
export function mountReplayLab(host: HTMLElement, options: MountOptions = {}) {
 return mount(host, options, new URL('.', import.meta.url).href);
}
