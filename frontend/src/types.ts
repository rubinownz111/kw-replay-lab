export type Row = Record<string, unknown>;
export interface Argument { type: string; value: unknown; opaque?: boolean; sha256?: string }
export interface Command {
  index: number; frame: number; time: string; time_seconds: number;
  source_index: number; player: string; message_type: number; name: string;
  category: string; is_action: boolean; arguments: Argument[];
  record_index: number; record_offset: number; wire_offset: number; wire_size: number; wire_sha256: string;
  asset?: {name: string; hash: string};
}
export interface CameraPoint { frame: number; camera_frame: number; stream_id: number; x: number; y: number; z: number; rotation_quaternion: number[]; position_inherited: boolean }
export interface RecordEntry {index: number; offset: number; frame: number; time: string; channel: number; channel_name: string; payload_size: number; payload_sha256: string; payload_prefix_hex: string; reserved: number; decoded_types: string[]}
export interface Player {name: string; source_index: number; team: number; action_count: number; command_count: number; apm: number; source_mapping: unknown; [key: string]: unknown}
export interface ReplayReport {
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
 if (!v || v.schema_version !== 7 || v.target !== 'KW 1.02' || v.match?.game_version !== '1.2.0.0' || !Array.isArray(v.commands?.items) || !Array.isArray(v.forensics?.records) || !Array.isArray(v.camera?.samples) || !Array.isArray(v.players) || !/^[a-f0-9]{64}$/i.test(v.file?.sha256 || '')) throw new Error('This report needs the KW 1.02 schema 7 analyzer. Restart or update the analyzer service.');
}
