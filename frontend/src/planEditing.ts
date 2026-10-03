import type { Plan } from './api';

type Segment = Plan['segments'][number];
type Shot = Segment['shots'][number];
const normalized = (text: string) => text.replace(/\s/g, '');
export const narrationLimit = (seconds: number) => Math.floor(seconds * 29 / 10);
export const shotNarrationLimit = (shot: Pick<Shot, 'start' | 'end'>) => Math.max(0, Math.floor((shot.end - shot.start) * 2.9 + 1e-7));
export const planScriptLimit = (plan: Plan) => Math.min(360, narrationLimit(plan.segments.reduce((total, segment) => total + segment.duration_seconds, 0)));
export function soundLabel(strategy: unknown, production?: unknown): string {
  const mode = production && typeof production === 'object' && 'speech_mode' in production ? production.speech_mode : undefined;
  if (mode === 'none') return '无台词';
  if (mode === 'character_dialogue') return '角色对白';
  if (mode === 'voiceover') return '画外旁白';
  return strategy === 'native' ? '中文原声' : '中文画外音';
}
export const segmentOffset = (plan: Plan, index: number) => plan.segments.slice(0, index).reduce((total, segment) => total + segment.duration_seconds, 0);
export const shotTimeLabel = (shot: Pick<Shot, 'start' | 'end'>, offset = 0) => `${Number((offset + shot.start).toFixed(3))}–${Number((offset + shot.end).toFixed(3))} 秒`;
const hasAnyShotNarration = (plan: Plan) => plan.segments.some(segment => segment.shots?.some(shot => !!shot.narration));
// An empty list is a deliberate silent span schedule, not a legacy plan.
export const hasSpeechSpans = (plan: Plan) => plan.segments.some(segment => segment.speech_spans != null);
const speechEditMessage = '请在对话中修改旁白，让台词与画面一起重新安排。';
export function hasBoundShotNarration(plan: Plan): boolean {
  return plan.audio_strategy === 'native' && !hasSpeechSpans(plan) && plan.segments.every(segment => !!segment.shots?.length
    && segment.shots.every(shot => typeof shot.narration === 'string')
    && normalized(segment.shots.map(shot => shot.narration || '').join('')) === normalized(segment.narration || ''));
}
// Legacy native plans have no shot speech. Preserve their segment-level editing
// until the planner supplies an actual mapping; never invent one in the client.
export function updateNarration(plan: Plan, index: number, text: string): Plan {
  if (hasSpeechSpans(plan)) throw new Error(speechEditMessage);
  if (hasAnyShotNarration(plan)) throw new Error('请按镜头修改台词。');
  const segments = plan.segments.map((segment, i) => i === index ? { ...segment, narration: text } : segment);
  return { ...plan, segments, script: segments.map(segment => segment.narration || '').join('') };
}
export function updateShotNarration(plan: Plan, segmentIndex: number, shotIndex: number, text: string): Plan {
  if (hasSpeechSpans(plan)) throw new Error(speechEditMessage);
  if (!hasBoundShotNarration(plan) || !plan.segments[segmentIndex]?.shots[shotIndex]) throw new Error('这版台词尚未对应到镜头，请先更新方案。');
  const segments = plan.segments.map((segment, i) => {
    if (i !== segmentIndex) return segment;
    const shots = segment.shots.map((shot, j) => j === shotIndex ? { ...shot, narration: text } : shot);
    return { ...segment, shots, narration: shots.map(shot => shot.narration || '').join('') };
  });
  // A direct merchant edit is authoritative, but is no longer the model-reviewed
  // content script. Keep the editable shot plan and its facts; never attach the
  // prior review verdict to different words. A chat revision rebuilds the script.
  const contentBinding = plan.ad_script || plan.script_review ? { ad_script: null, script_review: null } : {};
  return { ...plan, ...contentBinding, segments, script: segments.map(segment => segment.narration || '').join('') };
}
export function validEditedPlan(plan: Plan): boolean {
  // Span text has reviewed, timed visual anchors. The legacy edit endpoint must
  // not save new words with stale anchors; chat revision rebuilds their mapping.
  if (hasSpeechSpans(plan)) return false;
  if (!plan.script.trim() || plan.script.length > planScriptLimit(plan)) return false;
  if (plan.audio_strategy !== 'native') return true;
  if (plan.segments.some(segment => (segment.narration || '').length > narrationLimit(segment.duration_seconds))) return false;
  const joined = plan.segments.map(segment => segment.narration || '').join('');
  if (normalized(joined) !== normalized(plan.script)) return false;
  if (hasAnyShotNarration(plan)) {
    if (!hasBoundShotNarration(plan)) return false;
    if (plan.segments.some(segment => segment.shots.some(shot => (shot.narration || '').length > shotNarrationLimit(shot)))) return false;
  }
  return true;
}
