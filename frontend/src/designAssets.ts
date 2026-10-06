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

const STATEY = /湿|干|乱|顺|毛|关|开|满|空|皱|平|脏|净|合/;
const FROM_TO = /(?:由|从)([^，。；\s]{1,6}?)(?:到|变成|变为)([^，。；\s]{1,6})/;
const STATE_PAIRS: [string, string[]][] = [
  ['湿', ['干了', '变干', '吹干', '干燥', '干发', '已经干', '是干']],
  ['乱', ['顺了', '变顺', '梳顺', '顺滑', '已经顺']],
  ['毛躁', ['顺了', '顺滑', '不毛躁', '梳顺']],
  ['关上', ['打开', '开了', '敞开']],
  ['合上', ['打开', '敞开', '开了']],
  ['满', ['空了', '倒空', '见底']],
  ['皱', ['平整', '展平', '抚平']],
];

type StateShot = { id?: string; description?: string; purpose?: string; continuity?: { start_state?: string; end_state?: string; allowed_changes?: string } | null };

const INTERMEDIATE = /一半|半干|半湿|被吹开|吹开一半|开始变|还没干|未干|将干/;
const END_SMOOTH = /顺了|顺滑|不毛躁|梳顺|已经顺|顺开|变顺|拨顺/;
const END_DRY = /干了|已经干|干燥|干发|是干|干透|吹干/;
const SHOT_DONE = /已经干|干了|干透|顺了|顺滑|不毛躁|梳顺|拨顺|顺开/;

function shotBlob(shot: StateShot): string {
  const continuity = shot.continuity ?? {};
  return [continuity.start_state, continuity.end_state, continuity.allowed_changes, shot.description, shot.purpose].filter(Boolean).join(' ');
}

function shotChange(shot: StateShot): [string, string] | null {
  const continuity = shot.continuity ?? {};
  const start = continuity.start_state ?? '';
  const end = continuity.end_state ?? '';
  const allowed = continuity.allowed_changes ?? '';
  const description = shot.description ?? '';
  const blob = [start, end, allowed, description].filter(Boolean).join(' ');
  const match = FROM_TO.exec(blob);
  if (match && STATEY.test(match[1]) && STATEY.test(match[2])) return [match[1], match[2]];
  for (const [left, rights] of STATE_PAIRS) {
    if (left && start.includes(left) && !end.includes(left) && rights.some(right => end.includes(right) || allowed.includes(right) || description.includes(right))) {
      const after = rights.find(right => end.includes(right) || allowed.includes(right) || description.includes(right)) ?? '';
      return [left, after];
    }
  }
  return null;
}

function canonicalStart(text: string): string | null {
  if (!text || INTERMEDIATE.test(text)) return null;
  return ['湿', '毛躁', '乱', '关上', '合上', '满', '皱'].find(word => text.includes(word)) ?? null;
}

function endFromLabel(text: string): string | null {
  if (!text || INTERMEDIATE.test(text)) return null;
  if (END_SMOOTH.test(text)) return '顺了';
  if (!text.includes('半干') && (END_DRY.test(text) || /(?<![半未不])干$/.test(text))) return '干了';
  if (/打开|敞开|开了/.test(text) && !text.includes('吹开') && !text.includes('一半')) return '打开';
  if (/空了|倒空|见底/.test(text)) return '空了';
  if (/平整|展平|抚平/.test(text)) return '平整';
  return null;
}

function endFromShot(shot: StateShot): string | null {
  const end = shot.continuity?.end_state ?? '';
  const labeled = endFromLabel(end);
  if (labeled) return labeled;
  const description = shot.description ?? '';
  if (SHOT_DONE.test(description) && !INTERMEDIATE.test(description)) return endFromLabel(description);
  return null;
}

function subjectKeyFromText(text: string): string {
  if (text.includes('头发') || text.includes('发丝')) return '头发';
  if (text.includes('盖')) return '盖子';
  return '主体';
}

/** How many start/result stills this plan should show. One pair per subject, result on a later shot. */
export function planStateReferenceCount(plan: { segments?: { shots?: StateShot[] }[] } | null | undefined): number {
  const shots = plan?.segments?.flatMap(segment => segment.shots ?? []) ?? [];
  const groups = new Map<string, { index: number; before: string; after: string }[]>();
  shots.forEach((shot, index) => {
    const change = shotChange(shot);
    if (!change) return;
    const blob = shotBlob(shot);
    const key = change[0].includes('发') || blob.includes('头发') || blob.includes('发丝') || ['湿', '毛躁', '乱'].some(word => change[0].includes(word)) ? '头发' : change[0];
    const group = groups.get(key) ?? [];
    group.push({ index, before: change[0], after: change[1] });
    groups.set(key, group);
  });
  const open = new Map<string, { index: number; label: string }>();
  shots.forEach((shot, index) => {
    const start = shot.continuity?.start_state ?? '';
    const end = shot.continuity?.end_state ?? '';
    const key = subjectKeyFromText(`${start} ${end} ${shot.description ?? ''}`);
    if (shotChange(shot)) {
      open.delete(key);
      return;
    }
    const result = endFromLabel(start) || endFromLabel(end);
    const halfway = INTERMEDIATE.test(start) || INTERMEDIATE.test(end);
    if ((result || halfway) && open.has(key) && !groups.has(key)) {
      const proc = open.get(key)!;
      groups.set(key, [
        { index: proc.index, before: proc.label, after: proc.label },
        { index, before: start || end, after: result || start || end },
      ]);
      open.delete(key);
      return;
    }
    const startLabel = canonicalStart(end);
    if (startLabel && !endFromLabel(end)) open.set(key, { index, label: startLabel });
  });
  let count = 0;
  for (const group of groups.values()) {
    if (count >= 6) break;
    let startIndex = group[0].index;
    const labeled = group.find(item => canonicalStart(item.before));
    if (labeled) startIndex = labeled.index;
    else {
      for (let index = 0; index <= group[0].index; index += 1) {
        const found = canonicalStart(shotBlob(shots[index]));
        if (found) { startIndex = index; break; }
      }
    }
    if (!canonicalStart(group.find(item => item.index === startIndex)?.before ?? '') && !shots.slice(0, startIndex + 1).some(shot => canonicalStart(shotBlob(shot)))) continue;
    count += 1;
    const laterChange = [...group].reverse().find(item => item.index > startIndex && endFromLabel(item.after));
    const laterShot = laterChange ? laterChange.index : shots.findIndex((shot, index) => index > startIndex && endFromShot(shot));
    if (laterShot > startIndex) count += 1;
  }
  return count;
}

export const currentStateAssets = (detail: Detail | null): Asset[] => currentGeneratedDesignAssets(detail, 'start_frame');
