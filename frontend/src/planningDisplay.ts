import type { Detail, Job, Message, Plan } from './api';
import { savedPlanSource } from './conversation';
import { selectedStrategyDirection } from './strategy';

export interface PlanningNote { title: string; content: string }

/** Public, stored planning explanations. Never provider reasoning tokens. */
export function planningNotes(detail: Detail | null, job: Job): PlanningNote[] {
  if (!detail || job.project_id !== detail.project.id || job.kind !== 'plan'
    || job.status !== 'ready' || job.phase !== 'plan_ready') return [];
  const prose = detail.messages.filter(message => message.role === 'assistant' && message.job_id === job.id
    && message.content.trim().length > 120);
  // Keep each explanation with its original run. The current plan can have been
  // edited since then and must not be presented as that run's historical notes.
  return prose.map(message => ({ title: '本轮策划说明', content: message.content }));
}

export function planningMessageText(detail: Detail | null, message: Message): string {
  if (!detail || message.role !== 'assistant' || message.content.trim().length <= 120) return message.content;
  const job = detail.jobs.find(item => item.id === message.job_id && item.project_id === detail.project.id);
  if (!job || !planningNotes(detail, job).length) return message.content;
  return detail.project.plan && savedPlanSource(detail)?.id === job.id
    ? '广告方案已整理好，创意摘要和分镜见下方。'
    : '本轮广告方案已整理好，详细说明可展开策划过程查看。';
}

const sentence = (text: string) => {
  const value = text.trim();
  return value && !/[。！？.!?…]$/.test(value) ? `${value}。` : value;
};

const summarySentence = (text: string, limit: number) => {
  const first = text.trim().match(/^.*?[。！？!?]/u)?.[0]?.trim() || text.trim();
  const chars = Array.from(first);
  return chars.length <= limit ? first : chars.slice(0, limit - 1).join('') + '…';
};

/** Show the chosen concept, not just its label or unselected alternatives. */
export function creativeSummary(plan: Plan): string {
  const direction = selectedStrategyDirection(plan);
  if (!direction) return '';
  const brief = plan.strategy!.brief;
  const audience = brief.audience.trim() || direction.audience.trim();
  const scenario = brief.scenario.trim() || direction.scenario.trim();
  const context = [audience && `面向${audience}`, scenario && `场景设定为${scenario}`].filter(Boolean).join('，');
  if (plan.ad_script) {
    // The reviewed content is newer than the preliminary creative direction.
    // Show its intent and visible progression, never a second full dialogue block.
    const beats = plan.ad_script.beats;
    const progression = [beats[0], beats.length > 1 ? beats[beats.length - 1] : undefined]
      .filter(beat => !!beat).map(beat => summarySentence(beat!.visual_role, 100));
    return [summarySentence(context, 100), summarySentence(plan.ad_script.core_message, 140), ...progression]
      .map(sentence).filter(Boolean).join('');
  }
  return [context, direction.hook, direction.development, direction.payoff].map(sentence).filter(Boolean).join('');
}
