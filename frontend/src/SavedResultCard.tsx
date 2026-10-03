import { Download, Film, Play } from 'lucide-react';
import { downloadUrl } from './api';
import type { Detail } from './api';
import { playableRender, resultWorkLabel } from './projectResults';

/** The same saved video stays reachable while another attempt is in progress. */
export default function SavedResultCard({ detail, onPlay }: { detail: Detail | null; onPlay: () => void }) {
  const render = playableRender(detail);
  if (!render) return null;
  return <section className="saved-result-card" aria-label="已完成的成片">
    <div className="saved-result-heading"><Film size={17}/><strong>已有成片 V{render.revision}</strong></div>
    <p>{resultWorkLabel(detail)}</p>
    <div className="saved-result-actions">
      <button className="button subtle small" onClick={onPlay}><Play size={13}/>播放成片</button>
      <a className="button subtle small" href={downloadUrl(render.asset_id)} download><Download size={13}/>下载视频</a>
    </div>
  </section>;
}
