import type { Plan, StrategyDirection } from './api';

export function selectedStrategyDirection(plan: Plan | null | undefined): StrategyDirection | null {
  const strategy = plan?.strategy;
  if (!strategy?.selected_direction_id || !Array.isArray(strategy.directions)) return null;
  return strategy.directions.find(direction => direction.id === strategy.selected_direction_id && direction.label.trim()) ?? null;
}

export function shortDirectionReason(reason: string, limit = 72): string {
  const text = reason.replace(/\s+/g, ' ').trim();
  const firstSentence = text.match(/^.*?[。！？]/u)?.[0] || text;
  const characters = Array.from(firstSentence);
  return characters.length <= limit ? firstSentence : `${characters.slice(0, limit - 1).join('')}…`;
}

// Preparing a draft has no side effects: the normal composer sends the planning turn.
export function directionChangeDraft(draft: string, direction: StrategyDirection): string {
  const request = `请换个思路，避开当前「${direction.label.trim()}」的切入方式，保留已确认的商品事实和时长。请先更新广告方案供我确认。`;
  if (draft.includes(request)) return draft;
  return draft.trim() ? `${draft.trim()}\n\n${request}` : request;
}

// Keep optional strategy and shot evidence when editing narration on any plan version.
export function clonePlanForEditing(plan: Plan): Plan {
  return structuredClone(plan);
}
