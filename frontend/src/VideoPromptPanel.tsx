import { Copy } from 'lucide-react';
import { VIDEO_RECORD_MISSING, copyVideoPrompt, videoGenerationRecord, videoInputBasis } from './videoPrompt';

/** Read-only view of the prompt that was sent for this clip. */
export default function VideoPromptPanel({ asset }: { asset: { metadata?: Record<string, unknown> } }) {
  const record = videoGenerationRecord(asset);
  return <section className="image-prompt-panel" aria-label="提示词">
    <h4>提示词</h4>
    <p className="image-prompt-meta">模型：{record.model ?? VIDEO_RECORD_MISSING}</p>
    <p className="image-prompt-meta">{videoInputBasis(record.inputs)}</p>
    <pre className="image-prompt-text">{record.prompt ?? VIDEO_RECORD_MISSING}</pre>
    <button type="button" className="button" disabled={!record.prompt} onClick={() => { void copyVideoPrompt(record.prompt); }}>
      <Copy size={14}/>复制提示词
    </button>
  </section>;
}
