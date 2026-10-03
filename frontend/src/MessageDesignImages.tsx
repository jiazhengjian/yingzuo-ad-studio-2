import { useState } from 'react';
import { Maximize2, TriangleAlert } from 'lucide-react';
import { mediaUrl } from './api';
import type { Asset } from './api';
import { designImageLabel } from './designAssets';

function DesignImage({ asset, onPreview }: { asset: Asset; onPreview: (asset: Asset) => void }) {
  const [failed, setFailed] = useState(false);
  const label = designImageLabel(asset);
  return <figure className="message-design-image">
    <button type="button" className="message-design-preview" aria-label={`放大${label}：${asset.name}`} onClick={() => onPreview(asset)}>
      {failed ? <span className="message-design-failed"><TriangleAlert size={16}/>图片加载失败</span> : <>
        <img src={mediaUrl(asset)} alt={`${label}：${asset.name}`} onError={() => setFailed(true)}/>
        <span className="message-design-expand" aria-hidden="true"><Maximize2 size={13}/></span>
      </>}
    </button>
    <figcaption>{label}</figcaption>
  </figure>;
}

export default function MessageDesignImages({ assets, onPreview }: { assets: Asset[]; onPreview: (asset: Asset) => void }) {
  return <div className="message-design-images" role="group" aria-label={assets.some(asset => asset.kind === 'character') ? '商品、场景与角色图片' : '商品与场景图片'}>
    {assets.map(asset => <DesignImage key={`${asset.id}:${mediaUrl(asset)}`} asset={asset} onPreview={onPreview}/>)}
  </div>;
}
