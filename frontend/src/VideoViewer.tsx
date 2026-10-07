import { Download } from 'lucide-react';
import { downloadUrl, mediaUrl } from './api';
import VideoPromptPanel from './VideoPromptPanel';

type VideoAsset = { id: string; url?: string; name: string; metadata?: Record<string, unknown> };

/** Canvas thumbnail. Playback stays in the viewer, so this control does not take the click. */
export function CanvasVideoButton({ label, src, onOpen }: { label: string; src: string; onOpen: () => void }) {
  return <button type="button" className="video-open" aria-label={label} onClick={onOpen}>
    <video muted playsInline preload="metadata" src={src} />
  </button>;
}

/** Same two-pane viewer as a generated image: the clip on the left, the sent prompt on the right. */
export default function VideoViewer({ asset, note }: { asset: VideoAsset; note: string }) {
  return <div className="asset-viewer-content">
    <div className="asset-stage">
      <video key={asset.id} controls autoPlay muted playsInline src={mediaUrl({ id: asset.id, url: asset.url || '' })} aria-label={asset.name} />
      <div className="asset-tools">
        <a href={downloadUrl(asset.id)} download className="round-button" aria-label="下载视频"><Download size={17}/></a>
      </div>
    </div>
    <aside className="asset-inspector">
      <h3>素材与修改意见</h3>
      <div className="asset-description">
        <strong>{asset.name}</strong>
        <p>{note}</p>
        <small>提示词只读，这里不能修改或重新生成。</small>
      </div>
      <VideoPromptPanel asset={asset} />
    </aside>
  </div>;
}
