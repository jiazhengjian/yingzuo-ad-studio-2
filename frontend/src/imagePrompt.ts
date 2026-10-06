export type ImageInputRecord = { role: string; label: string; asset_id?: string };

export type ImageGenerationRecord = {
  prompt: string | null;
  model: string | null;
  inputs: ImageInputRecord[] | null;
};

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

/** The prompt and references saved with an image. Missing fields stay missing. */
export function imageGenerationRecord(asset: { metadata?: Record<string, unknown> } | null | undefined): ImageGenerationRecord {
  const meta = asset?.metadata ?? {};
  const request = meta.design_request;
  const requestPrompt = request && typeof request === 'object' && !Array.isArray(request)
    ? text((request as Record<string, unknown>).prompt)
    : null;
  const inputs = Array.isArray(meta.image_inputs)
    ? meta.image_inputs.flatMap((item): ImageInputRecord[] => {
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
    prompt: text(meta.image_prompt) ?? requestPrompt,
    model: text(meta.model),
    inputs,
  };
}

export function imageInputBasis(inputs: ImageInputRecord[] | null): string {
  if (!inputs) return '依据：无记录';
  if (!inputs.length) return '依据：没有参考图';
  const labels = [...new Set(inputs.map(item => item.label))];
  return `依据：${labels.join('、')}`;
}

export async function copyImagePrompt(prompt: string | null): Promise<boolean> {
  if (!prompt || typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return false;
  await navigator.clipboard.writeText(prompt);
  return true;
}
