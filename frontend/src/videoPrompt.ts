export type VideoGenerationRecord = {
  prompt: string | null;
  model: string | null;
};

export const VIDEO_RECORD_MISSING = '暂无记录';

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

/** The prompt saved with a generated clip. Missing fields stay missing. */
export function videoGenerationRecord(asset: { metadata?: Record<string, unknown> } | null | undefined): VideoGenerationRecord {
  const meta = asset?.metadata ?? {};
  return {
    prompt: text(meta.video_prompt),
    model: text(meta.model),
  };
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
