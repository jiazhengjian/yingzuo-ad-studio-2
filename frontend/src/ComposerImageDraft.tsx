import { useEffect, useState } from 'react';
import { X } from 'lucide-react';

/** Preview of images waiting in the composer. Removing one does not send anything. */
export default function ComposerImageDraft({ files, onRemove, disabled = false }: { files: File[]; onRemove: (index: number) => void; disabled?: boolean }) {
  const [urls, setUrls] = useState<string[]>([]);
  useEffect(() => {
    const next = files.map(file => URL.createObjectURL(file));
    setUrls(next);
    return () => next.forEach(url => URL.revokeObjectURL(url));
  }, [files]);
  if (!files.length) return null;
  return <div className="composer-file-strip" aria-label="待发送的商品图片">{files.map((file, index) => (
    <div className="composer-file" key={`${file.name}-${file.size}-${file.lastModified}-${index}`}>
      {urls[index] && <img src={urls[index]} alt="" />}
      <button type="button" disabled={disabled} aria-label={`移除商品图 ${file.name}`} onClick={() => onRemove(index)}><X size={12} /></button>
    </div>
  ))}</div>;
}
