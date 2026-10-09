/** Reference images and material prep stay on the design card. Video appears after that confirmation. */
const MATERIAL_PHASES = new Set([
  'queued', 'creating_designs', 'synthesizing_voice', 'voice', 'tts', 'synthesizing', 'awaiting_creative',
]);

export interface CanvasVideoState {
  hasRender: boolean;
  hasSegments: boolean;
  phase?: string | null;
  kind?: string | null;
  pendingGateKind?: string | null;
}

export function canvasShowsVideoSection(state: CanvasVideoState): boolean {
  if (state.hasRender || state.hasSegments) return true;
  if (state.pendingGateKind === 'creative') return false;
  const kind = state.kind ?? '';
  if (kind === 'generate' || kind === 'recompose' || kind === 'cta') {
    if (MATERIAL_PHASES.has(state.phase ?? '')) return false;
  }
  return true;
}
