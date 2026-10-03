import type { ProductionDecision } from './api';

export function speechRoleLabel(production?: ProductionDecision | null, speakerId?: string): string {
  if (production?.speech_mode === 'none') return '无台词';
  if (production?.speech_mode !== 'character_dialogue') return '旁白';
  const speaker = production.characters.find(character => character.id === (speakerId || production.speaker_id));
  const label = speaker ? `${speaker.name}说话` : '角色对白';
  return production.speech_delivery === 'on_and_off_screen' ? `${label}（含画外延续）` : label;
}

/** Display the saved decision; absence on an older plan does not mean nobody appears. */
export default function ProductionSummary({ production }: { production?: ProductionDecision | null }) {
  if (!production) return null;
  const count = production.characters.length;
  const partial = production.characters.filter(character => character.appearance === 'partial_only').length;
  const people = count ? partial === count ? `${count} 人局部出镜`
    : partial ? `${count} 人出镜（${partial} 人仅局部）` : `${count} 人出镜` : '无人出镜';
  const speech = production.speech_mode === 'voiceover' ? '画外旁白' : speechRoleLabel(production);
  const images = count ? `1 张场景图＋${count} 张角色四视图` : '1 张场景图';
  return <p className="production-summary" aria-label="人物、声音与参考图安排">{people} · {speech} · {images}</p>;
}
