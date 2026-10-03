export type Route = 'product' | 'presenter';
export type AdForm = 'auto' | 'brand_tvc' | 'scene_story' | 'presenter_pitch';
export type FilmStructure = 'product_visual' | 'world_intercut' | 'lifestyle' | 'scene_action' | 'presenter_explanation';
export interface FilmConcept { structure: FilmStructure; key_visual: string; memory_anchor: string; sound_role: string }
export interface CreativeIntent { use_context: string; setting_requirement: string; reference_intent: string; structure_preference: 'auto' | 'product_visual' | 'world_intercut' | 'lifestyle' }
export interface Health { ok: boolean; configured: { planner: boolean; video: boolean }; active_jobs: number; providers: { planner: string; video: string }; capabilities: { video_model: string; max_video_submissions: number; max_image_submissions: number; duration_min_seconds: number; duration_max_seconds: number; default_duration_seconds: number; duration_seconds?: number }; limitations: string[] }
export interface ProductionDecision { version: '1.0.0'; product_constraints: { text: string; evidence_ids: string[] }[]; characters: { id: string; name: string; description: string; appearance?: 'face_visible' | 'partial_only' | null }[]; speech_mode: 'voiceover' | 'character_dialogue' | 'none'; speech_delivery?: 'visible_only' | 'on_and_off_screen' | null; speaker_id: string; voice_description: string }
export interface StrategyDirection { id: string; rule_id: string; label: string; audience: string; scenario: string; angle: string; audience_value?: string; product_expression?: string; story_summary?: string; attraction_mechanism?: string; consumer_insight?: string; big_idea?: string; hook_type?: string; brand_world?: string; film_concept?: FilmConcept | null; production?: ProductionDecision | null; hook: string; development: string; payoff: string; evidence_ids: string[]; reason: string; production_notes: string[] }
export interface PlanStrategy { version: string; brief: { product_name: string; category: string; audience: string; scenario: string; creative_intent?: CreativeIntent | null; facts: { id: string; text: string; source_kind: 'merchant' | 'image'; source_id: string }[]; unknowns: string[]; assumptions: string[] }; directions: StrategyDirection[]; selected_direction_id: string; rules_used: { id: string; version: string; sources: string[] }[] }
export interface AdScript { skill_version: string; core_message: string; audience_takeaway: string; product_role: string; evidence_ids: string[]; beats: { id: string; purpose: string; message: string; visual_role: string; narration: string; evidence_ids: string[] }[] }
export interface ScriptReview { script: AdScript; ready: boolean; checks: { criterion: string; passed: boolean; note: string }[] }
export interface Shot { id?: string; start: number; end: number; description: string; narration?: string; purpose?: string; evidence_ids?: string[]; beat_id?: string; beat_ids?: string[] }
export interface SpeechAnchor { text: string; start: number; end: number; shot_ids: string[]; visual_evidence: string }
export interface SpeechSpan { id: string; beat_id: string; text: string; start: number; end: number; shot_ids: string[]; anchors: SpeechAnchor[]; speaker_id?: string }
export interface Segment { id: string; duration_seconds: number; purpose: string; prompt: string; narration: string; shots: Shot[]; speech_spans?: SpeechSpan[] | null; reference_asset_ids: string[] }
export interface Plan { title: string; script: string; language: 'zh-CN'; aspect_ratio: '9:16'; segments: Segment[]; facts: { name: string; claims: string[] }; audio_strategy: 'voiceover' | 'native'; production?: ProductionDecision | null; strategy?: PlanStrategy | null; ad_script?: AdScript | null; script_review?: ScriptReview | null }
export interface Project { id: string; title: string; brief: string; route: Route; ad_form?: AdForm | null; duration_seconds: number; status: string; work_status?: string; result_status?: 'ready' | 'none'; result_revision?: number | null; revision: number; context_version: number; plan_context_version: number; plan: Plan | null; current_render_id?: string | null; created_at: number; updated_at: number }
export interface Asset { id: string; project_id: string; kind: 'product' | 'person' | 'character' | 'scene' | 'start_frame' | 'segment' | 'audio' | 'render'; name: string; url: string; mime: string; revision: number; metadata: Record<string, unknown> }
export interface Message { id: string; role: 'user' | 'assistant' | 'system'; content: string; job_id?: string | null; created_at: number }
export interface JobProgress extends Record<string, unknown> { error_code?: string; retry_action?: string | null; retryable?: boolean; failed_segment_ids?: string[]; issue_summary?: string[]; repair_attempts?: number }
export interface MediaSkillUsage { id: string; display_name: string; version: string; mode: 'compiler'; stage: 'asset_prompts' | 'video_prompts'; content_sha256: string }
export interface Job { id: string; project_id: string; revision: number; kind: 'plan' | 'generate' | 'recompose' | 'cta'; status: string; phase: string; error?: string | null; progress: JobProgress; media_skill_usage?: MediaSkillUsage[]; created_at: number; updated_at: number; started_at?: number | null }
export interface Question { id: string; prompt: string; options: { id: string; label: string }[]; allow_custom: boolean; allow_skip: boolean }
export interface Answer { question_id: string; option_id?: string; text?: string; skipped?: boolean }
export interface CreativeOption { id: string; label: string; description: string; segment: Plan['segments'][number] }
export interface DirectionOption extends StrategyDirection { recommended: boolean }
interface GateBase { id: string; project_id: string; job_id: string; status: 'pending' | 'answered' | 'superseded'; message: string; questions: Question[]; answers: Answer[]; selected_option_id?: string; note?: string; created_at: number; answered_at?: number | null; revision?: number; context_version?: number }
export interface ExistingGate extends GateBase { kind: 'clarification' | 'creative'; options: CreativeOption[] }
export interface DirectionGateData extends GateBase { kind: 'direction'; options: DirectionOption[]; revision: number; context_version: number }
export type Gate = ExistingGate | DirectionGateData;
export interface RunEvent { seq: number; project_id: string; job_id: string; kind: 'status' | 'step' | 'message' | 'gate' | 'artifact' | 'terminal'; label: string; state: 'running' | 'done' | 'waiting' | 'failed' | 'canceled'; created_at: number }
export interface Render { id: string; asset_id: string; url: string; revision: number; created_at: number; status: string; manifest: Record<string, unknown> }
export interface Detail { project: Project; assets: Asset[]; messages: Message[]; jobs: Job[]; renders: Render[]; gates: Gate[]; events: RunEvent[] }
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...options, headers: { ...(options?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options?.headers } });
  if (!response.ok) { let detail = `请求没有完成（${response.status}）`; try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; } catch { /* HTTP status stays useful */ } throw new Error(detail); }
  return response.json() as Promise<T>;
}
export const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
export const activeStatus = (status: string) => ['queued', 'running', 'cancel_requested'].includes(status);
export const mediaUrl = (asset: Pick<Asset, 'id' | 'url'>) => asset.url || `/media/${encodeURIComponent(asset.id)}`;

// Use the server attachment response; embedded browsers may ignore <a download>.
export const downloadUrl = (assetId: string) => `/media/${encodeURIComponent(assetId)}?download=true`;
