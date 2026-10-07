export type VideoInputRecord = { role: string; label: string; asset_id?: string };

export type VideoGenerationRecord = {
  prompt: string | null;
  model: string | null;
  inputs: VideoInputRecord[] | null;
};

export const VIDEO_RECORD_MISSING = '暂无记录';

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

/** The prompt and references saved with a generated clip. Missing fields stay missing. */
export function videoGenerationRecord(asset: { metadata?: Record<string, unknown> } | null | undefined): VideoGenerationRecord {
  const meta = asset?.metadata ?? {};
  const inputs = Array.isArray(meta.video_inputs)
    ? meta.video_inputs.flatMap((item): VideoInputRecord[] => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      const role = text(record.role);
      const label = text(record.label);
      if (!role || !label) return [];
      const assetId = text(record.asset_id);
      return [assetId ? { role, label, asset_id: assetId } : { role, label }];
    })
    : null;
  return {
    prompt: text(meta.video_prompt),
    model: text(meta.model),
    inputs,
  };
}

export function videoInputBasis(inputs: VideoInputRecord[] | null): string {
  if (!inputs) return `依据：${VIDEO_RECORD_MISSING}`;
  if (!inputs.length) return '依据：没有参考图';
  const labels = [...new Set(inputs.map(item => item.label))];
  return `依据：${labels.join('、')}`;
}

export function isCanvasVideo<T extends { kind?: string }>(asset: T | null | undefined): asset is T & { kind: 'segment' | 'render' } {
  return asset?.kind === 'segment' || asset?.kind === 'render';
}

export function videoViewerNote(asset: { kind?: string; metadata?: Record<string, unknown> } | null | undefined): string {
  const saved = typeof asset?.metadata?.video_prompt === 'string' && !!asset.metadata.video_prompt.trim();
  if (asset?.kind === 'segment') return '这一段是发给视频模型的片段。请核对商品、镜头和声音。';
  if (saved) return '这是由这一段合成的成片。请核对商品、镜头衔接与声音。';
  return '这是合成后的成片。请核对商品、镜头衔接与声音。每一段发给模型的提示写在对应片段上。';
}

export async function copyVideoPrompt(prompt: string | null): Promise<boolean> {
  if (!prompt || typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return false;
  await navigator.clipboard.writeText(prompt);
  return true;
}
