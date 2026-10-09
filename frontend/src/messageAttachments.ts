import type { Asset, Message } from './api';

const IMAGE_KINDS = new Set(['product', 'person', 'character', 'scene', 'start_frame']);

export function isOriginalUpload(asset: Asset): boolean {
  return IMAGE_KINDS.has(asset.kind) && asset.metadata.generated !== true;
}

function assetTime(asset: Asset): number | null {
  const value = asset.created_at;
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * The user message this upload was actually sent with.
 * A saved message id wins. Otherwise the image follows the next user message
 * when nothing from the assistant sits between the upload and that send.
 * An opening image stays on the first message once the assistant has replied.
 */
export function attachmentMessageId(messages: Message[], asset: Asset): string | null {
  if (!isOriginalUpload(asset)) return null;
  if (asset.metadata.sent_without_message === true || asset.metadata.pending_attachment === true) return null;
  const userMessages = messages.filter(message => message.role === 'user')
    .sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
  const linked = asset.metadata.message_id;
  if (typeof linked === 'string' && userMessages.some(message => message.id === linked)) return linked;
  if (!userMessages.length) return null;
  const time = assetTime(asset);
  if (time == null) return userMessages[0].id;
  const next = userMessages.find(message => message.created_at >= time);
  if (!next) return [...userMessages].reverse().find(message => message.created_at <= time)?.id ?? userMessages[userMessages.length - 1].id;
  const assistantBetween = messages.some(message => message.role === 'assistant' && message.created_at > time && message.created_at < next.created_at);
  if (!assistantBetween) return next.id;
  return [...userMessages].reverse().find(message => message.created_at <= time)?.id ?? userMessages[0].id;
}

export function originalAssetsForMessage(messages: Message[], assets: Asset[], message: Message): Asset[] {
  if (message.role !== 'user') return [];
  return assets.filter(asset => attachmentMessageId(messages, asset) === message.id)
    .sort((a, b) => (assetTime(a) ?? 0) - (assetTime(b) ?? 0) || a.id.localeCompare(b.id));
}

/** Uploads that were sent without a text bubble, so they must not stick to an older message. */
export function unplacedOriginalAssets(messages: Message[], assets: Asset[]): Asset[] {
  return assets.filter(asset => isOriginalUpload(asset) && asset.metadata.pending_attachment !== true && attachmentMessageId(messages, asset) == null)
    .sort((a, b) => (assetTime(a) ?? 0) - (assetTime(b) ?? 0) || a.id.localeCompare(b.id));
}
