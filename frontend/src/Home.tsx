import { useEffect, useRef, useState } from 'react';
import type { ClipboardEvent, DragEvent, FormEvent, ReactNode } from 'react';
import { ArrowRight, ChevronDown, Clapperboard, Clock3, FolderOpen, ImagePlus, LoaderCircle, Mic, Plus, Settings2, ShoppingBag, Sparkles, TriangleAlert, X } from 'lucide-react';
import type { AdForm } from './api';
import { homeAdChoice, homeAdChoices, nextHomeAdForm, type HomeAdForm } from './adForm';
import './home.css';

export interface HomeProps {
  brand: ReactNode;
  draft: string;
  onDraft: (value: string) => void;
  adForm: AdForm | null;
  onAdFormChange: (value: HomeAdForm | null) => void;
  durationSeconds: number;
  onDurationChange: (value: number) => void;
  files: File[];
  /** Newly added, validated files; the parent appends these to its pending files. */
  onFiles: (files: File[]) => void;
  onRemoveFile: (index: number) => void;
  onSubmit: (event: FormEvent) => void;
  onOpenProjects: () => void;
  busy: boolean;
  online: boolean;
  plannerConfigured: boolean;
  onSettings: () => void;
  loading: boolean;
  error: string;
  onDismissError: () => void;
}

const typeIcons = { brand_tvc: ShoppingBag, scene_story: Clapperboard, presenter_pitch: Mic };

export default function Home({ brand, draft, onDraft, adForm, onAdFormChange, durationSeconds, onDurationChange, files, onFiles, onRemoveFile, onSubmit, onOpenProjects, busy, online, plannerConfigured, onSettings, loading, error, onDismissError }: HomeProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const settingsRef = useRef<HTMLDivElement>(null);
  const durationButtonRef = useRef<HTMLButtonElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [parametersOpen, setParametersOpen] = useState(false);
  const [durationInput, setDurationInput] = useState(String(durationSeconds));
  const [fileError, setFileError] = useState('');
  const [previews, setPreviews] = useState<{ file: File; url: string }[]>([]);
  const disabled = busy || loading;
  const canSubmit = !disabled && online && draft.trim().length >= 2;
  const chosen = homeAdChoice(adForm);

  useEffect(() => { setDurationInput(String(durationSeconds)); }, [durationSeconds]);

  function commitDuration() {
    const value = durationInput.trim() ? Number(durationInput) : durationSeconds;
    const bounded = Number.isFinite(value) ? Math.max(10, Math.min(60, Math.round(value))) : durationSeconds;
    setDurationInput(String(bounded));
    onDurationChange(bounded);
  }

  useEffect(() => {
    const items = files.map(file => ({ file, url: URL.createObjectURL(file) }));
    setPreviews(items);
    return () => items.forEach(item => URL.revokeObjectURL(item.url));
  }, [files]);

  useEffect(() => {
    if (!parametersOpen) return;
    const outside = (event: MouseEvent) => { if (!settingsRef.current?.contains(event.target as Node)) setParametersOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setParametersOpen(false); durationButtonRef.current?.focus(); } };
    document.addEventListener('mousedown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('mousedown', outside); document.removeEventListener('keydown', escape); };
  }, [parametersOpen]);

  function addFiles(incoming: File[]) {
    if (disabled) return;
    const valid = incoming.filter(file => ['image/jpeg', 'image/png', 'image/webp'].includes(file.type) && file.size <= 15 * 1024 * 1024);
    const remaining = Math.max(0, 3 - files.length);
    const accepted = valid.slice(0, remaining);
    const problems = [];
    if (valid.length !== incoming.length) problems.push('请使用15 MB以内的JPG、PNG或WebP图片');
    if (valid.length > remaining) problems.push('每个项目最多添加3张商品图');
    setFileError(problems.join('；'));
    if (accepted.length) onFiles(accepted);
  }

  function pasteImages(event: ClipboardEvent<HTMLFormElement>) {
    const images = Array.from(event.clipboardData.items).filter(item => item.kind === 'file').map(item => item.getAsFile()).filter((file): file is File => file !== null);
    if (images.length) { event.preventDefault(); addFiles(images); }
  }

  function enterDrag(event: DragEvent<HTMLFormElement>) {
    if (!event.dataTransfer.types.includes('Files') || disabled) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }

  function leaveDrag(event: DragEvent<HTMLFormElement>) {
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (!dragDepth.current) setDragging(false);
  }

  function dropFiles(event: DragEvent<HTMLFormElement>) {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    addFiles(Array.from(event.dataTransfer.files));
  }

  function submit(event: FormEvent) {
    if (!canSubmit) { event.preventDefault(); return; }
    onSubmit(event);
  }

  function chooseType(id: HomeAdForm) {
    onAdFormChange(nextHomeAdForm(adForm, id));
    textareaRef.current?.focus();
  }

  return <div className="home-page">
    <header className="home-header">
      <div className="home-header-left"><div className="home-brand">{brand}</div><nav className="home-nav" aria-label="主导航"><span aria-current="page">首页</span><button type="button" onClick={onOpenProjects}>我的项目</button></nav></div>
      <div className="home-header-right"><span className={`home-connection ${online ? '' : 'is-offline'}`}><i/>{online ? '本地工作台' : '服务未连接'}</span><button type="button" className="home-header-settings" onClick={onSettings} aria-label="打开连接设置"><Settings2 size={18}/><span>连接设置</span></button></div>
    </header>

    <main className="home-main">
      <section className="home-create" aria-labelledby="home-title">
        <div className="home-heading"><p className="home-eyebrow"><span/>让你的商品，拥有自己的镜头</p><h1 id="home-title">从商品，到好广告<span>。</span></h1><p className="home-subtitle">说说你想做什么广告，也可以先上传商品图。</p></div>
        <div className="home-composer-frame">
          <form className={`home-composer ${files.length ? 'has-files' : ''} ${dragging ? 'is-dragging' : ''}`} onSubmit={submit} onPaste={pasteImages} onDragEnter={enterDrag} onDragLeave={leaveDrag} onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); }} onDrop={dropFiles}>
            <div className="home-composer-body">
              {!files.length && <button type="button" className="home-product-upload" onClick={() => inputRef.current?.click()} disabled={disabled}><span className="home-product-icon"><ShoppingBag size={29} strokeWidth={1.4}/><Plus size={13}/></span><strong>添加商品</strong><span>上传一张清晰的商品图</span></button>}
              <div className="home-input-column">
                {files.length > 0 && <div className="home-file-strip" aria-label="待上传的商品图片">{files.map((file, index) => { const preview = previews.find(item => item.file === file); return <div className="home-file" key={`${file.name}-${file.size}-${file.lastModified}-${index}`} title={file.name}>{preview && <img src={preview.url} alt={file.name}/>}<span>商品 {index + 1}</span><button type="button" disabled={disabled} aria-label={`移除商品图 ${file.name}`} onClick={() => { setFileError(''); onRemoveFile(index); }}><X size={12}/></button></div>; })}{files.length < 3 && <button type="button" className="home-file-add" disabled={disabled} onClick={() => inputRef.current?.click()} aria-label="继续添加商品图"><Plus size={19}/><span>商品</span></button>}</div>}
                {chosen && <div className="home-prompt-label"><Sparkles size={14}/><span>{chosen.name}</span></div>}
                <label className="home-sr-only" htmlFor="home-draft">描述你想制作的广告</label>
                <textarea ref={textareaRef} id="home-draft" value={draft} onChange={event => onDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing && canSubmit) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder="比如：这是我的香水，想拍一条干净、有质感的广告，突出瓶身设计…" rows={3} maxLength={6000} disabled={disabled}/>
              </div>
            </div>
            <div className="home-composer-footer">
              <div className="home-create-controls">
                <div className="home-parameters" ref={settingsRef}>
                  <button type="button" ref={durationButtonRef} className={`home-duration ${parametersOpen ? 'is-open' : ''}`} disabled={disabled} aria-label={`选择视频时长，当前${durationSeconds}秒`} aria-expanded={parametersOpen} aria-controls="home-parameters" onClick={() => setParametersOpen(open => !open)}><Clock3 size={16}/><span>{durationSeconds}秒</span><ChevronDown size={12}/></button>
                  {parametersOpen && <div className="home-parameters-popover" id="home-parameters">
                    <div className="home-parameters-title"><strong>视频时长</strong><span>10–60 秒</span></div>
                    <div className="home-duration-input-row"><label htmlFor="home-duration-input">成片时长</label><div><input id="home-duration-input" type="number" min={10} max={60} step={1} inputMode="numeric" value={durationInput} disabled={disabled} onChange={event => { const raw = event.target.value; setDurationInput(raw); const value = Number(raw); if (raw && Number.isInteger(value) && value >= 10 && value <= 60) onDurationChange(value); }} onBlur={commitDuration} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commitDuration(); } }}/><span>秒</span></div></div>
                    <input className="home-duration-slider" type="range" min={10} max={60} step={1} aria-label="视频时长" aria-valuetext={`${durationSeconds}秒`} value={durationSeconds} disabled={disabled} onChange={event => onDurationChange(Number(event.target.value))}/>
                    <div className="home-duration-bounds"><span>10秒</span><span>60秒</span></div>
                    <div className="home-duration-presets" aria-label="常用时长">{[10, 15, 20, 30, 45, 60].map(value => <button type="button" key={value} aria-pressed={durationSeconds === value} disabled={disabled} onClick={() => onDurationChange(value)}>{value}秒</button>)}</div>
                    <dl><div><dt>画面比例</dt><dd>9:16 竖屏</dd></div><div><dt>语言与声音</dt><dd>中文原声</dd></div></dl>
                    <p>按所选时长安排脚本与镜头。时长越长，制作时间和生成用量通常越多。</p>
                  </div>}
                </div>
                <button className="home-create-button" type="submit" disabled={!canSubmit}>{busy ? <LoaderCircle size={17} className="home-spin"/> : <Sparkles size={17}/>}<span>{busy ? '正在准备' : '开始创作'}</span><ArrowRight size={16}/></button>
              </div>
            </div>
            <input ref={inputRef} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={event => { addFiles(Array.from(event.target.files ?? [])); event.target.value = ''; }}/>
            {dragging && <div className="home-drop-overlay" aria-live="polite"><ImagePlus size={28}/><span>松手，添加商品图</span><small>最多3张 · JPG、PNG、WebP</small></div>}
          </form>
        </div>
        {(fileError || error) && <div className="home-error" role="alert"><TriangleAlert size={16}/><span>{fileError || error}</span><button type="button" aria-label="关闭错误提示" onClick={() => { setFileError(''); onDismissError(); }}><X size={15}/></button></div>}
        <div className="home-composer-note"><span>{loading ? '正在连接你的工作台…' : !online ? '本地服务暂未连接，请检查服务后继续。' : '可以先聊需求，也可以拖入或粘贴商品图'}</span>{!plannerConfigured && !loading && <button type="button" onClick={onSettings}>配置策划服务 <ArrowRight size={12}/></button>}</div>
        <div className="home-presets" aria-label="广告类型">{homeAdChoices.map(type => { const Icon = typeIcons[type.id]; return <button type="button" key={type.id} aria-pressed={adForm === type.id} disabled={disabled} onClick={() => chooseType(type.id)}><Icon size={18}/><span><strong>{type.name}</strong><small>{type.note}</small></span><ArrowRight size={14}/></button>; })}</div>
      </section>
      <footer className="home-footer"><span>为认真做商品的人，认真做好每一条广告。</span><button type="button" onClick={onOpenProjects}><FolderOpen size={13}/>我的项目</button></footer>
    </main>
  </div>;
}
