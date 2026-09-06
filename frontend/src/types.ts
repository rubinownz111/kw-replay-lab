export type Row = Record<string, unknown>;
export interface Argument { type: string; value: unknown; opaque?: boolean; sha256?: string; label?: string; label_status?: string }
export interface Command {
  index: number; frame: number; time: string; time_seconds: number;
  source_index: number; player: string; message_type: number; name: string;
  display_name?: string; meaning?: string; semantic_status?: string; semantic_evidence?: Row; schema_issues?: string[];
  category: string; is_action: boolean; arguments: Argument[];
  argument_sha256?: string;
  record_index: number; record_offset: number; wire_offset: number; wire_size: number; wire_sha256: string;
  asset?: {name: string; hash: string|null; production_index?:number};
}
export interface CameraPoint { frame: number; camera_frame: number; stream_id: number; x: number; y: number; z: number; rotation_quaternion: number[]; position_inherited: boolean }
export interface RecordEntry {index: number; offset: number; frame: number; time: string; channel: number; channel_name: string; payload_size: number; payload_sha256: string; payload_prefix_hex: string; reserved: number; decoded_types: string[]}
export interface Player {name: string; source_index: number; team: number; action_count: number; command_count: number; apm: number; source_mapping: unknown; [key: string]: unknown}
export interface ReplayReport {
 desync?: DesyncReport; desync_case?: DesyncCase;
 schema_version: number; target: string; evidence: Row; file: {name: string; size: number; sha256: string};
 match: {title: string; map_name: string; map_id: string; duration: string; duration_seconds: number; last_frame: number; game_version: string; recorded_at: unknown; seed: unknown; game_session_id: unknown; map_crc: unknown; result: Row; [key: string]: unknown};
 players: Player[]; summary: {command_count: number; action_count: number; record_count: number; decode_error_count: number; [key: string]: unknown};
 commands: {items: Command[]; category_counts: {name: string; count: number}[]};
 strategy: {build_order: Row[]; eliminations: Row[]; sell_events: Row[]; [key: string]: unknown};
 camera: {path: CameraPoint[]; samples: CameraPoint[]; streams: Row[]; [key: string]: unknown};
 network: {crc_checkpoints: Row[]; rtt_points: Row[]; [key: string]: unknown};
 auxiliary_streams: {voice: Row; telestrator: Row; metadata: Row};
 integrity: Row; forensics: {records: RecordEntry[]}; cheat_analysis: {findings: Row[]; scope: Row[]; disclaimer: string};
 telemetry: Row; metadata: Row; channels: Row[]; observer: Row;
}
export interface SavedReplay {hash: string; report: ReplayReport; file?: Blob; savedAt: string; bytes: number}
export interface MountOptions {apiBase?: string; report?: ReplayReport; offline?: boolean; theme?: 'light' | 'dark' | 'system'}
export function validateReport(value: unknown): asserts value is ReplayReport {
 const v = value as Partial<ReplayReport> | null;
 if (!v || ![7,8].includes(v.schema_version??0) || v.target !== 'KW 1.02' || v.match?.game_version !== '1.2.0.0' || !Array.isArray(v.commands?.items) || !Array.isArray(v.forensics?.records) || !Array.isArray(v.camera?.samples) || !Array.isArray(v.players) || !/^[a-f0-9]{64}$/i.test(v.file?.sha256 || '')) throw new Error('This report needs the KW 1.02 schema 7 or 8 analyzer. Restart or update the analyzer service.');
}

export interface CRCReport {source_index:number; player:string; crc:number; record_frame:number; delay_frames:number; playback:boolean; mismatch_reporting:boolean; command_index:number}
export interface Checkpoint {epoch:number; frame:number; time:string; status:string; agreement:boolean|null; report_count:number; source_count:number; missing_sources:number[]; conflicting_sources:number[]; unexpected_sources:number[]; duplicate_reports:number; mismatch_reporting:boolean; reports:CRCReport[]}
export interface Incident {epoch:number; frame:number; start_frame:number; last_matching_frame:number|null; kind:string; command_count:number; command_indices:number[]; commands_truncated:boolean; categories:Record<string,number>; objects:{id:number;references:number}[]; sources:number[]}
export interface DesyncReport {version:number; status:string; title:string; coverage:{expected_sources:number[]; source_labels:Record<string,string>; checkpoint_count:number; matching_count:number; disagreement_count:number; incomplete_count:number; conflicting_count:number; invalid_crc_count:number; decode_errors:number; [key:string]:unknown};checkpoints:Checkpoint[];incidents:Incident[];next_steps:Row[];limitations:string}
export interface DiagnosticField {path:string;label:string;type:string;value:unknown;offset?:number;size?:number;line?:number;raw_hex?:string;value_sha256:string}
export interface Diagnostic {name:string;sha256:string;size:number;kind:string;fields:DiagnosticField[];issues:string[];complete:boolean;forced:boolean;frame:number|null;settings:Row;association:string;format_version?:number}
export interface DesyncCase {peers:ReplayReport[];diagnostics:Diagnostic[];notes:string}
