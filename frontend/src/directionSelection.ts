import type { DirectionGateData, Gate, Project } from './api';

type ProjectVersion = Pick<Project, 'id' | 'context_version' | 'revision'>;

export function isCurrentDirectionGate(gate: DirectionGateData, project?: ProjectVersion): boolean {
  return !!project && gate.status === 'pending' && gate.project_id === project.id
    && Number.isInteger(gate.context_version) && Number.isInteger(gate.revision)
    && gate.context_version === project.context_version && gate.revision === project.revision;
}

/** Only the latest pending gate can control the shared composer. */
export function pendingConversationGate(gates: Gate[], project?: ProjectVersion): Gate | undefined {
  const newest = [...gates].filter(gate => gate.status === 'pending' && gate.project_id === project?.id)
    .sort((a, b) => b.created_at - a.created_at)[0];
  return newest?.kind === 'direction' && !isCurrentDirectionGate(newest, project) ? undefined : newest;
}

export function validDirectionOptions(gate: DirectionGateData): boolean {
  const text = (value?: string) => typeof value === 'string' && !!value.trim();
  return gate.options.length === 3 && new Set(gate.options.map(option => option.id)).size === 3
    && gate.options.every(option => text(option.id) && text(option.label)
      && (text(option.big_idea) || text(option.film_concept?.key_visual) || text(option.hook) || text(option.payoff) || text(option.angle)));
}

export function directionSelectRequest(gate: DirectionGateData, directionId: string, project: ProjectVersion | undefined, activeGateId: string | undefined, clientTurnId: string) {
  if (!isCurrentDirectionGate(gate, project) || activeGateId !== gate.id) throw new Error('这些方向已更新，请从最新卡片选择。');
  if (!validDirectionOptions(gate) || !gate.options.some(option => option.id === directionId)) throw new Error('请选择卡片中的一个创意方向。');
  return { gate_id: gate.id, direction_id: directionId, expected_context_version: gate.context_version,
    accepted_revision: gate.revision, client_turn_id: clientTurnId };
}

/** Synchronous lock covers clicks before React has rendered a busy state. */
export function directionSubmission() {
  let state: 'idle' | 'submitting' | 'submitted' = 'idle';
  return {
    state: () => state,
    async run(action: () => Promise<void>) {
      if (state !== 'idle') throw new Error('请等待当前操作完成。');
      state = 'submitting';
      try { await action(); state = 'submitted'; }
      catch (error) { state = 'idle'; throw error; }
    },
  };
}
