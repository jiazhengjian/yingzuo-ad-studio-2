import type { Asset, Detail, Job, Message } from './api';

/** Attach each design result to its saved message, including after later revisions. */
export function messageDesignAssets(detail: Detail | null, message: Message): Asset[] {
  if (!detail || message.role !== 'assistant' || !message.job_id) return [];
  const gate = detail.gates.find(item => item.project_id === detail.project.id && item.kind === 'creative'
    && item.job_id === message.job_id && item.message === message.content);
  if (!gate || gate.kind !== 'creative') return [];
  const owner = detail.messages.find(item => item.role === 'assistant' && item.job_id === gate.job_id && item.content === gate.message);
  if (owner?.id !== message.id) return [];
  const ids = new Set(gate.options.find(option => option.id === 'current')?.segment.reference_asset_ids ?? []);
  const job = detail.jobs.find(item => item.id === gate.job_id && item.project_id === detail.project.id && item.kind === 'generate');
  if (Array.isArray(job?.progress.design_asset_ids)) for (const id of job.progress.design_asset_ids) {
    if (typeof id === 'string') ids.add(id);
  }
  const assets = detail.assets.filter(asset => asset.project_id === detail.project.id);
  for (const asset of assets) {
    if (asset.kind === 'scene' && ids.has(asset.id) && Array.isArray(asset.metadata.source_asset_ids)) {
      for (const id of asset.metadata.source_asset_ids) if (typeof id === 'string') ids.add(id);
    }
  }
  return oneImagePerCharacter(assets.filter(asset => ids.has(asset.id) && designAssetVisible(asset))
    .sort((a, b) => designOrder(a) - designOrder(b)));
}

function designAssetVisible(asset: Asset): boolean {
  if (asset.kind === 'product' && !asset.metadata.generated) return true;
  if (asset.kind === 'scene' && asset.metadata.generated) return true;
  if (asset.kind === 'character' && asset.metadata.generated) return true;
  return asset.kind === 'start_frame' && !!asset.metadata.generated && asset.metadata.state_reference === true;
}

/** Resolve only references belonging to this plan or its current finished render. */
function currentGeneratedDesignAssets(detail: Detail | null, kind: 'scene' | 'character' | 'start_frame'): Asset[] {
  if (!detail) return [];
  const { project, assets, jobs, renders, gates } = detail;
  // New requirements cannot silently inherit a previous plan's design state.
  if (project.context_version !== project.plan_context_version) return [];
  const sourceIds = new Set<string>();
  const sourceJobIds = new Set<string>();
  const generationJobs = jobs.filter(job => job.project_id === project.id && job.kind === 'generate')
    .sort((a, b) => b.created_at - a.created_at);
  const addJob = (job: Job | undefined) => {
    if (!job || sourceJobIds.has(job.id)) return;
    sourceJobIds.add(job.id);
    if (Array.isArray(job.progress.design_asset_ids)) for (const id of job.progress.design_asset_ids) {
      if (typeof id === 'string') sourceIds.add(id);
    }
    for (const gate of gates) {
      if (gate.project_id !== project.id || gate.job_id !== job.id || gate.kind !== 'creative') continue;
      for (const id of gate.options.find(option => option.id === 'current')?.segment.reference_asset_ids ?? []) sourceIds.add(id);
    }
  };
  addJob(generationJobs.find(job => job.revision === project.revision));
  for (const segment of project.plan?.segments ?? []) for (const id of segment.reference_asset_ids) sourceIds.add(id);

  const currentRender = renders.find(render => render.id === project.current_render_id || render.asset_id === project.current_render_id);
  if (currentRender?.revision === project.revision && ['ready', 'completed', 'succeeded'].includes(currentRender.status)) {
    // Recomposition changes the public revision while retaining the actual
    // generation's references. Follow explicit render ancestry, never recency.
    const visited = new Set<string>();
    let render: typeof currentRender | undefined = currentRender;
    while (render && !visited.has(render.id)) {
      visited.add(render.id);
      addJob(generationJobs.find(job => job.revision === render?.revision));
      const baseId: unknown = render.manifest.base_render_id;
      render = typeof baseId === 'string' ? renders.find(candidate => candidate.id === baseId) : undefined;
    }
  }
  return [...new Map(assets.filter(asset => asset.project_id === project.id && asset.kind === kind && asset.metadata.generated
    && (kind !== 'start_frame' || asset.metadata.state_reference === true)
    && (sourceIds.has(asset.id) || asset.revision === project.revision || typeof asset.metadata.job_id === 'string' && sourceJobIds.has(asset.metadata.job_id)))
    .map(asset => [asset.id, asset])).values()];
}

const designOrder = (asset: Asset) => asset.kind === 'product' ? 0 : asset.kind === 'scene' ? 1 : asset.kind === 'character' ? 2 : 3;

/** A character is represented by one saved reference sheet, not separate view tiles. */
function oneImagePerCharacter(assets: Asset[]): Asset[] {
  const chosen = new Map<string, Asset>();
  for (const asset of assets) {
    const characterId = asset.kind === 'character' && typeof asset.metadata.character_id === 'string' ? asset.metadata.character_id : '';
    const key = characterId ? `character:${characterId}` : `asset:${asset.id}`;
    const previous = chosen.get(key);
    if (!previous || asset.revision >= previous.revision) chosen.set(key, asset);
  }
  return [...chosen.values()];
}

export const currentSceneAssets = (detail: Detail | null): Asset[] => currentGeneratedDesignAssets(detail, 'scene');
export function currentCharacterAssets(detail: Detail | null): Asset[] {
  const characters = detail?.project.plan?.production?.characters;
  const current = currentGeneratedDesignAssets(detail, 'character');
  return oneImagePerCharacter(characters ? current.filter(asset => characters.some(character => character.id === asset.metadata.character_id)) : current);
}

export function designImageLabel(asset: Asset): string {
  if (asset.kind === 'product') return '商品原图';
  if (asset.kind === 'character') {
    const name = typeof asset.metadata.character_name === 'string' && asset.metadata.character_name.trim() ? asset.metadata.character_name.trim() : asset.name;
    return `${asset.metadata.view_layout === 'four_view' ? '角色四视图' : '角色参考'} · ${name}`;
  }
  if (asset.kind === 'start_frame' && asset.metadata.state_reference === true) {
    const label = typeof asset.metadata.state_label === 'string' && asset.metadata.state_label.trim() ? asset.metadata.state_label.trim() : '';
    if (asset.metadata.sheet_layout === 'horizontal_16x9') {
      const phase = asset.metadata.phase === 'start' ? '起始' : '结果';
      return label ? `状态四视图 · ${phase} · ${label}` : `状态四视图 · ${phase}`;
    }
    const phase = asset.metadata.phase === 'start' ? '起始状态' : '结果状态';
    return label ? `${phase} · ${label}` : phase;
  }
  return '场景图';
}

type StateShot = { id?: string; description?: string; continuity?: { start_state?: string; end_state?: string } | null };

const UNFINISHED = /仍|还是|还没|未干|没有干|半湿|半干|一半|湿着|是湿/;
const REACHED = /已经|干了|吹干|干透|干燥|顺了|顺开|打开|空了|平整|见底/;
const STATEY = /湿|干|乱|顺|毛|关|开|满|空|皱|平|脏|净|合/;
const SUBJECTS = ['头发', '发丝', '盖子', '瓶盖', '杯', '门', '衣服', '商品'];

function stateReached(text: string): boolean {
  return Boolean(text) && !UNFINISHED.test(text) && REACHED.test(text);
}

function subjectOf(shot: StateShot): string | null {
  const start = shot.continuity?.start_state ?? '';
  const end = shot.continuity?.end_state ?? '';
  const blob = `${start} ${end} ${shot.description ?? ''}`;
  if (!STATEY.test(blob)) return null;
  const word = SUBJECTS.find(item => blob.includes(item));
  if (!word) return '主体';
  return word === '发丝' ? '头发' : word;
}

/** How many start/result stills this plan should show. One pair per subject. Pass enabled:false to plan none. */
export function planStateReferenceCount(plan: { segments?: { shots?: StateShot[] }[] } | null | undefined, options?: { enabled?: boolean }): number {
  if (options?.enabled === false) return 0;
  const shots = plan?.segments?.flatMap(segment => segment.shots ?? []) ?? [];
  const groups = new Map<string, number[]>();
  shots.forEach((shot, index) => {
    const subject = subjectOf(shot);
    if (!subject) return;
    const group = groups.get(subject) ?? [];
    group.push(index);
    groups.set(subject, group);
  });
  let count = 0;
  for (const indexes of groups.values()) {
    if (count >= 6) break;
    const resultIndex = indexes.find(index => {
      const shot = shots[index];
      return stateReached(shot.continuity?.end_state ?? '') || stateReached(shot.continuity?.start_state ?? '');
    });
    if (resultIndex == null) {
      count += 1;
      continue;
    }
    const earlier = indexes.some(index => index < resultIndex && !stateReached(shots[index].continuity?.end_state ?? ''));
    const startOnResult = !stateReached(shots[resultIndex].continuity?.start_state ?? '');
    if (earlier || startOnResult) count += 1;
    count += 1;
  }
  return count;
}

export const currentStateAssets = (detail: Detail | null): Asset[] => currentGeneratedDesignAssets(detail, 'start_frame');
