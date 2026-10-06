import { Copy } from 'lucide-react';
import { copyImagePrompt, imageGenerationRecord, imageInputBasis } from './imagePrompt';

/** Read-only view of the prompt that was sent for this image. */
export default function ImagePromptPanel({ asset }: { asset: { metadata?: Record<string, unknown> } }) {
  const record = imageGenerationRecord(asset);
  return <section className="image-prompt-panel" aria-label="提示词">
    <h4>提示词</h4>
    <p className="image-prompt-meta">模型：{record.model ?? '无记录'}</p>
    <p className="image-prompt-meta">{imageInputBasis(record.inputs)}</p>
    <pre className="image-prompt-text">{record.prompt ?? '无记录'}</pre>
    <button type="button" className="button" disabled={!record.prompt} onClick={() => { void copyImagePrompt(record.prompt); }}>
      <Copy size={14}/>复制提示词
    </button>
  </section>;
}
