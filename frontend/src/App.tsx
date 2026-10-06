import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, FormEvent, ReactNode } from 'react';
import { ArrowDownToLine, ArrowRight, AtSign, Check, CheckCircle2, ChevronLeft, ChevronRight, Download, FileImage, Film, FolderOpen, ImagePlus, Images, Layers3, LoaderCircle, Maximize2, MessageSquare, PanelLeftClose, PanelLeftOpen, Pencil, Plus, RefreshCw, Settings2, Sparkles, Square, TriangleAlert, X } from 'lucide-react';
import Home from './Home';
import AdFormSummary from './AdFormSummary';
import ConversationGate from './ConversationGate';
import MessageDesignImages from './MessageDesignImages';
import type { ConversationGateHandle, GateComposerState } from './ConversationGate';
import RunTimeline from './RunTimeline';
import JobRecoveryCard from './JobRecoveryCard';
import SavedResultCard from './SavedResultCard';
import { playableRender, projectListStatus, resultWorkLabel } from './projectResults';
import { recoveryForJob, retryRequest } from './jobRecovery';
import { conversationItems as buildConversationItems } from './conversation';
import { soundLabel, validEditedPlan, hasSpeechSpans } from './planEditing';
import PlanEditor, { PlanShotDetails } from './PlanEditor';
import ProductionSummary from './ProductionSummary';
import { currentCharacterAssets, currentSceneAssets, currentStateAssets, designImageLabel, messageDesignAssets, planStateReferenceCount } from './designAssets';
import { clonePlanForEditing } from './strategy';
import { creativeSummary, planningMessageText, planningNotes } from './planningDisplay';
import { directionSelectRequest, isCurrentDirectionGate, pendingConversationGate } from './directionSelection';
import { stageProductImages } from './composerDraft';
import ComposerImageDraft from './ComposerImageDraft';
import './conversation.css';
import { api, json, activeStatus, mediaUrl, downloadUrl } from './api';
import type { Answer, Asset, Detail, Gate, Health, Job, Message, Plan, Project, Render, RunEvent } from './api';

const phaseNames: Record<string, string> = { planning: '正在整理商品与脚本', plan: '正在整理商品与脚本', voice: '正在准备中文配音', tts: '正在准备中文配音', synthesizing: '正在准备中文配音', submit: '正在提交视频任务', submitting: '正在提交视频任务', generate: '正在生成视频', generating: '正在生成视频', video: '正在生成视频', polling: '视频生成中', downloading: '正在取回视频', download: '正在取回视频', compose: '正在合成成片', composing: '正在合成成片', rendering: '正在合成成片', render: '正在合成成片', inspect: '正在检查文件', checking: '正在检查文件', completed: '文件已准备好', ready: '文件已准备好', canceled: '已停止', queued: '等待制作', retry: '正在继续处理' };
const kindNames: Record<string, string> = { product: '商品', person: '人物', character: '角色参考', scene: '场景', start_frame: '状态参考' };
Object.assign(phaseNames, { resuming_plan: '正在接续已保存的策划进度', reviewing_plan: '正在综合检查商品表达、动作与声画计划', writing_script: '正在策划广告内容', storyboarding: '正在安排画面和台词', refining_script: '正在精简动作并重排脚本时间', refining_storyboard: '正在根据审核意见调整分镜', retrying_planner: '策划服务未返回内容，系统仍在处理这一步', retrying_planner_request: '策划服务暂时不可用，系统仍在处理这一步', using_backup_planner: '改用备用策划服务，继续这一步', recovering_strategy: '系统仍在处理这一步，需求已保留', understanding: '正在读取需求与商品素材', deciding: '正在判断是否需要补充信息', awaiting_direction: '等待选择广告方向', plan_ready: '方案已准备好', synthesizing_voice: '正在准备中文配音', creating_designs: '正在生成参考图', submitting_video: '正在提交视频任务', waiting_provider: '正在等待视频生成', downloading_video: '正在取回视频', segments_ready: '视频片段已准备好', needs_reconcile: '提交结果需要核对', complete: '文件已准备好' });
const dateText = (value: number) => { const d = new Date(value * 1000); return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }); };
const errorText = (error: unknown) => error instanceof Error ? error.message : '暂时没有完成，请重试。';
const byNewest = <T extends { created_at: number }>(items: T[]) => [...items].sort((a, b) => b.created_at - a.created_at);
const elapsedText = (createdAt: number) => { const seconds = Math.max(0, Math.floor(Date.now() / 1000 - createdAt)); return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`; };
const positiveNumber = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
const durationText = (seconds: number | null) => seconds == null ? '时长待核对' : `${Number(seconds.toFixed(2))} 秒`;
const renderDuration = (render?: Render) => { const frames = positiveNumber(render?.manifest.total_frames), fps = positiveNumber(render?.manifest.fps); return frames && fps ? frames / fps : null; };

function Brand() { return <span className="brand"><span className="brand-orbit" aria-hidden="true"/><span>映作<small>Yingzuo</small></span></span>; }
function Spinner() { return <LoaderCircle size={16} className="spin" aria-hidden="true" />; }
export function PlanStrategySummary({ plan }: { plan: Plan }) {
  const summary = creativeSummary(plan);
  return summary ? <div className="plan-creative-summary" aria-label="创意摘要"><h4>创意摘要</h4><p>{summary}</p></div> : null;
}

function Modal({ title, children, onClose, className = '' }: { title: string; children: ReactNode; onClose: () => void; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; const dialog = ref.current; dialog?.focus(); const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); if (e.key === 'Tab' && dialog) { const controls = dialog.querySelectorAll<HTMLElement>('button:not([disabled]), input, textarea, a[href], [tabindex="0"]'); const first = controls[0]; const last = controls[controls.length - 1]; if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) { e.preventDefault(); last?.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); } } }; document.addEventListener('keydown', handler); return () => { document.removeEventListener('keydown', handler); previous?.focus(); }; }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div ref={ref} className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}><div className="modal-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭弹窗" onClick={onClose}><X size={20} /></button></div>{children}</div></div>;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [online, setOnline] = useState(true);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('project'));
  const [detail, setDetail] = useState<Detail | null>(null);
  const detailRef = useRef(detail);
  detailRef.current = detail;
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [sidebar, setSidebar] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<'chat' | 'canvas'>('chat');
  const [chatWidth, setChatWidth] = useState(370);
  const [statusOpen, setStatusOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [videoOpen, setVideoOpen] = useState(false);
  const autoCanvasProject = useRef('');
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const pendingGateRef = useRef<ConversationGateHandle>(null);
  const gateInputLock = useRef(false);
  const gateComposerKey = useRef('');
  const [gateComposer, setGateComposer] = useState<GateComposerState | null>(null);
  const [draft, setDraft] = useState('');
  const [durationSeconds, setDurationSeconds] = useState(20);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [selectedAsset, setSelectedAsset] = useState<Asset | null>(null);
  const [previewAsset, setPreviewAsset] = useState<Asset | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editPlan, setEditPlan] = useState<Plan | null>(null);
  const [editRevision, setEditRevision] = useState<number | null>(null);
  const [showSegments, setShowSegments] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadLock = useRef(false);
  const conversationScroll = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const endMessages = useRef<HTMLDivElement>(null);
  const selectedProjectRef = useRef<string | null>(null);
  const navigationEpoch = useRef(0);
  const refreshEpoch = useRef<Record<string, number>>({});
  const turnAttempts = useRef(new Map<string, string>());
  const submissionLock = useRef(false);
  const streamCursor = useRef<Record<string, number>>({});
  const [pendingText, setPendingText] = useState<{ projectId: string; text: string } | null>(null);
  const playerRef = useRef<HTMLDivElement>(null);
  const project = detail?.project;
  const jobs = byNewest(detail?.jobs ?? []);
  const activeJob = jobs.find(job => activeStatus(job.status));
  const currentJobs = jobs.filter(job => job.revision === project?.revision);
  const latestJob = currentJobs[0];
  const hasActive = !!activeJob;
  const plan = project?.plan;
  const projectDuration = positiveNumber(project?.duration_seconds) ?? 20;
  const planDuration = plan?.segments.reduce((total, segment) => total + segment.duration_seconds, 0) || projectDuration;
  const expectedSegmentCount = plan?.segments.length || Math.ceil(projectDuration / 10);
  const gates = detail?.gates ?? [];
  const pendingGate = pendingConversationGate(gates, project);
  const planStale = !!plan && project?.context_version !== project?.plan_context_version;
  const imageAssets = (detail?.assets ?? []).filter(a => ['product', 'person', 'character', 'scene', 'start_frame'].includes(a.kind));
  const segments = (detail?.assets ?? []).filter(a => a.kind === 'segment');
  const renders = byNewest(detail?.renders ?? []);
  const shownRender = playableRender(detail);
  const failedJob = !hasActive && latestJob && ['failed', 'partial'].includes(latestJob.status) ? latestJob : undefined;
  const unresolved = latestJob && ['submitted_unknown', 'needs_review'].includes(latestJob.status);
  const jobRecovery = recoveryForJob(!hasActive ? latestJob : undefined);
  const hasOriginalProduct = imageAssets.some(a => a.kind === 'product' && !a.metadata.generated);
  const existingGeneration = currentJobs.some(job => job.kind === 'generate');
  const hasCurrentReadyRender = renders.some(render => render.revision === project?.revision && render.status === 'ready');
  const needsNewPlan = existingGeneration || hasCurrentReadyRender;
  const canPlan = !!health?.configured.planner && !hasActive && !busy && !uploading && !pendingGate;
  const canEditPlan = !!plan && !hasActive && !busy && !uploading && !pendingGate && !unresolved && !planStale;
  // A validated manual edit advances the plan revision without a new planner job.
  // It can be confirmed directly; replanning here would discard the user's edits.
  const canGenerate = !!plan && !!health?.configured.video && !hasActive && !busy && !uploading && !unresolved && !needsNewPlan && hasOriginalProduct && !pendingGate && !planStale;
  const providerProgress = activeJob?.progress.provider_progress;
  const hasProviderProgress = typeof providerProgress === 'number' && Number.isFinite(providerProgress) && providerProgress >= 0 && providerProgress <= 100;

  const loadHealth = useCallback(async () => { try { const h = await api<Health>('/api/health'); setHealth(h); setOnline(true); } catch { setOnline(false); } }, []);
  const loadProjects = useCallback(async () => { const result = await api<{ projects: Project[] }>('/api/projects'); setProjects(result.projects); return result.projects; }, []);
  const refresh = useCallback(async (id: string) => { const serial = (refreshEpoch.current[id] || 0) + 1; refreshEpoch.current[id] = serial; const value = await api<Detail>(`/api/projects/${id}`); if (refreshEpoch.current[id] === serial) { if (selectedProjectRef.current === id) setDetail(value); setProjects(items => items.map(item => item.id === id ? value.project : item)); } return value; }, []);
  const chooseProject = useCallback((id: string | null, historyMode: 'push' | 'none' = 'push') => {
    navigationEpoch.current += 1; nearBottom.current = true;
    if (historyMode === 'push' && id !== selectedProjectRef.current) window.history.pushState(null, '', id ? `?project=${encodeURIComponent(id)}` : window.location.pathname);
    if (id && id === selectedProjectRef.current) { void refresh(id).catch(e => setError(errorText(e))); setSidebar(false); return; }
    selectedProjectRef.current = id; setProjectId(id); setDetail(null); setDraft(''); setPendingFiles([]); setError(''); setNotice(''); setPendingText(null);
    setSelectedAsset(null); setPreviewAsset(null); setSidebar(false); setEditPlan(null); setEditRevision(null); setGateComposer(null); gateComposerKey.current = '';
    setShowHistory(false); setShowSegments(false); setStatusOpen(false);
    setRenameOpen(false); setVideoOpen(false); setWorkspaceView('chat'); autoCanvasProject.current = '';
  }, [refresh]);
  useEffect(() => {
    let alive = true;
    const linkedId = new URLSearchParams(window.location.search).get('project');
    if (linkedId) chooseProject(linkedId, 'none');
    Promise.allSettled([api<Health>('/api/health'), api<{ projects: Project[] }>('/api/projects')]).then(([h, p]) => {
      if (!alive) return;
      if (h.status === 'fulfilled') { setHealth(h.value); setOnline(true); } else setOnline(false);
      if (p.status === 'fulfilled') setProjects(p.value.projects);
      if (!linkedId) setLoading(false);
    });
    const pop = () => chooseProject(new URLSearchParams(window.location.search).get('project'), 'none');
    window.addEventListener('popstate', pop);
    return () => { alive = false; window.removeEventListener('popstate', pop); };
  }, [chooseProject]);
  useEffect(() => { if (!projectId) { setLoading(false); return; } let alive = true; setLoading(true); refresh(projectId).catch(e => { if (alive) setError(errorText(e)); }).finally(() => { if (alive) setLoading(false); }); return () => { alive = false; }; }, [projectId, refresh]);
  // SSE drives the conversation. A slower snapshot poll also recovers missed terminal events.
  useEffect(() => { if (!projectId || !hasActive) return; let running = false; const timer = window.setInterval(() => { if (running) return; running = true; refresh(projectId).then(() => setOnline(true)).catch(() => setOnline(false)).finally(() => { running = false; }); }, 5000); return () => window.clearInterval(timer); }, [projectId, hasActive, refresh]);
  useEffect(() => {
    if (!projectId) return;
    const id = projectId;
    const stream = new EventSource(`/api/projects/${encodeURIComponent(id)}/events/stream?after=${streamCursor.current[id] || 0}`);
    let refreshTimer: number | undefined;
    const receive = (event: Event) => {
      if (selectedProjectRef.current !== id) return;
      try {
        const batch = JSON.parse((event as MessageEvent).data) as { events: RunEvent[]; cursor: number };
        streamCursor.current[id] = Math.max(streamCursor.current[id] || 0, batch.cursor || 0);
        setDetail(current => current?.project.id !== id ? current : { ...current, events: [...new Map([...(current.events || []), ...batch.events].map(item => [item.seq, item])).values()].sort((a,b) => a.seq - b.seq) });
        if (refreshTimer === undefined) refreshTimer = window.setTimeout(() => { refreshTimer = undefined; void refresh(id).catch(() => {}); }, 100);
      } catch { /* A full snapshot will recover an interrupted event envelope. */ }
    };
    stream.addEventListener('update', receive);
    stream.onopen = () => { void refresh(id).catch(() => {}); };
    return () => { stream.close(); if (refreshTimer !== undefined) window.clearTimeout(refreshTimer); };
  }, [projectId, refresh]);
  useEffect(() => { const timer = window.setInterval(loadHealth, 15000); return () => window.clearInterval(timer); }, [loadHealth]);
  useEffect(() => { const onFocus = () => { void loadHealth(); }; window.addEventListener('focus', onFocus); return () => window.removeEventListener('focus', onFocus); }, [loadHealth]);
  useEffect(() => { if (!nearBottom.current) return; requestAnimationFrame(() => { const element = conversationScroll.current; if(element) element.scrollTop = element.scrollHeight; }); }, [detail?.messages.length, detail?.events?.length, pendingGate?.id, activeJob?.phase, workspaceView, pendingText]);
  useEffect(() => {
    if (project && !pendingGate && window.innerWidth > 700 && (shownRender || imageAssets.some(a => a.metadata.generated)) && autoCanvasProject.current !== project.id) {
      autoCanvasProject.current = project.id; setWorkspaceView('canvas');
    }
  }, [project, pendingGate, shownRender, imageAssets]);
  useEffect(() => { if (pendingGate && window.innerWidth <= 700) setWorkspaceView('chat'); }, [pendingGate?.id]);
  const handleGateComposer = useCallback((state: GateComposerState) => {
    const key = `${state.gateId}:${state.mode}:${state.questionId || ''}`;
    if (key !== gateComposerKey.current) { gateComposerKey.current = key; setDraft(state.initialText || ''); }
    setGateComposer(state);
  }, []);
  useEffect(() => { if (!pendingGate) { gateComposerKey.current = ''; setGateComposer(null); } }, [pendingGate?.id]);
  useEffect(() => { if (!statusOpen && !sidebar) return; const close = (e: KeyboardEvent) => { if (e.key === 'Escape') { setStatusOpen(false); setSidebar(false); } }; document.addEventListener('keydown', close); return () => document.removeEventListener('keydown', close); }, [statusOpen, sidebar]);

  async function run(label: string, action: (setTarget: (id: string) => void) => Promise<void>) { if (busy) return; let target = selectedProjectRef.current; setBusy(label); setError(''); setNotice(''); try { await action(id => { target = id; }); } catch (e) { if (selectedProjectRef.current === target) setError(errorText(e)); } finally { setBusy(''); } }
  function attemptId(key: string) {
    const existing = turnAttempts.current.get(key);
    if (existing) return existing;
    const id = crypto.randomUUID(); turnAttempts.current.set(key, id); return id;
  }
  async function submitTurn(id: string, contextVersion: number, input: Record<string, unknown> = {}) {
    const key = JSON.stringify([id, input]);
    const result = await api<{ job: Job }>(`/api/projects/${id}/turns`, json('POST', {
      ...input, expected_context_version: contextVersion, client_turn_id: attemptId(key),
    }));
    turnAttempts.current.delete(key);
    return result;
  }
  async function uploadFile(id: string, file: File) { const form = new FormData(); form.append('file', file); return api<Asset>(`/api/projects/${id}/assets`, { method: 'POST', body: form }); }
  function stageIncoming(files: FileList | File[]) {
    if (busy || uploading || uploadLock.current) return;
    const list = [...files];
    if (!list.length) return;
    if (projectId && hasActive) { setError('本次制作完成后再补充商品图。'); return; }
    const limit = projectId ? Math.max(0, 3 - originalProducts.length) : 3;
    const staged = stageProductImages(pendingFiles, list, limit);
    setPendingFiles(staged.files);
    if (staged.error) setError(staged.error);
    else if (projectId && staged.files.length > pendingFiles.length) {
      setError('');
      setNotice('商品图已附在输入框，写上商品信息后再发送。');
      setWorkspaceView('chat');
    }
  }
  async function uploadStaged(target: string, files: File[]) {
    uploadLock.current = true; setUploading(true);
    try {
      for (const file of files) await uploadFile(target, file);
      const updated = await refresh(target);
      if (selectedProjectRef.current === target) { detailRef.current = updated; setPendingFiles([]); }
      return updated;
    } finally { uploadLock.current = false; setUploading(false); }
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!draft.trim() || busy || hasActive || uploading || uploadLock.current || submissionLock.current) return;
    if (pendingGate) {
      if (gateInputLock.current || !pendingGateRef.current || gateComposer?.canInput === false) return;
      const target = projectId, gateId = pendingGate.id, files = [...pendingFiles];
      gateInputLock.current = true; setError('');
      try {
        if (target && files.length) await uploadStaged(target, files);
        await pendingGateRef.current.submitText(draft.trim());
        if (selectedProjectRef.current === target && gateComposerKey.current.startsWith(`${gateId}:`)) { setDraft(''); setSelectedAsset(null); }
      } catch (caught) { if (selectedProjectRef.current === target) setError(errorText(caught)); }
      finally { gateInputLock.current = false; }
      return;
    }
    submissionLock.current = true;
    const brief = draft.trim(), files = [...pendingFiles], target = projectId, startedNavigation = navigationEpoch.current;
    if (target) { nearBottom.current = true; setPendingText({projectId: target, text: brief}); }
    try { await run('message', async setTarget => {
      if (!target) {
        const created = await api<Project>('/api/projects', json('POST', { brief, route: 'product', duration_seconds: durationSeconds }));
        setTarget(created.id);
        if (navigationEpoch.current === startedNavigation) { chooseProject(created.id); setDetail({ project: created, assets: [], messages: [{id:'initial-local',role:'user',content:brief,created_at:created.created_at}], jobs: [], renders: [], gates: [], events: [] }); }
        try { for (const file of files) await uploadFile(created.id, file); } finally { await refresh(created.id); await loadProjects(); }
        if (health?.configured.planner) { const current = await refresh(created.id); await submitTurn(created.id, current.project.context_version); await refresh(created.id); }
        else if (selectedProjectRef.current === created.id) setNotice('需求已保存，连接策划服务后即可开始对话。');
      } else {
        const content = brief;
        let version = detailRef.current?.project.id === target ? detailRef.current.project.context_version : project?.context_version ?? 0;
        if (files.length) version = (await uploadStaged(target, files)).project.context_version;
        if (health?.configured.planner) {
          await submitTurn(target, version, { content, reference_asset_ids: selectedAsset ? [selectedAsset.id] : [] });
        } else {
          await api(`/api/projects/${target}/messages`, json('POST', { content, ...(selectedAsset ? { asset_id: selectedAsset.id } : {}) }));
        }
        if (selectedProjectRef.current === target) { setDraft(''); setSelectedAsset(null); }
        await refresh(target);
        if (!health?.configured.planner && selectedProjectRef.current === target) setNotice('需求已保存。连接策划服务后即可继续。');
      }
    }); } finally { submissionLock.current = false; setPendingText(null); }
  }
  async function startPlan() { if (!projectId || !canPlan || uploadLock.current) return; await run('plan', async () => { await submitTurn(projectId, project?.context_version ?? 0, plan ? {content:'请结合已确认的回答和最新商品素材，更新这版广告方案。'} : {}); await refresh(projectId); }); }
  function requestPlanChanges() {
    if (!project || !canPlan || submissionLock.current || uploadLock.current) return;
    const target = project.id;
    // Focus the existing composer without changing its draft or submitting a turn.
    requestAnimationFrame(() => { if (selectedProjectRef.current === target) composerInput.current?.focus(); });
  }
  function focusAdFormChange() {
    const target = project?.id;
    if (!target || hasActive || busy || uploading || (pendingGate && pendingGate.kind !== 'direction')) return;
    requestAnimationFrame(() => { if (selectedProjectRef.current === target) composerInput.current?.focus(); });
  }
  async function answerGate(gate: Gate, answers: Answer[]) {
    const liveProject = detailRef.current?.project;
    if (submissionLock.current || busy || uploading || hasActive || !liveProject || gate.status !== 'pending') throw new Error('请等待当前操作完成。');
    const id = liveProject.id; submissionLock.current = true; setBusy('answer'); setError(''); nearBottom.current = true;
    try { await submitTurn(id, liveProject.context_version, { reply_to_gate_id: gate.id, answers }); await refresh(id); }
    catch (e) { if(selectedProjectRef.current === id) setError(errorText(e)); throw e; }
    finally { submissionLock.current = false; setBusy(''); }
  }
  async function confirmCreative(gate: Gate, optionId: string, note: string) {
    if (submissionLock.current || busy || uploading || hasActive || !project || gate.status !== 'pending') throw new Error('请等待当前操作完成。');
    const id = project.id, key = JSON.stringify([id, gate.id, optionId, note]);
    submissionLock.current = true; setBusy('creative'); setError(''); nearBottom.current = true;
    try {
      await api(`/api/projects/${id}/creative-confirm`, json('POST', { gate_id:gate.id, accepted_revision:project.revision, option_id:optionId, note, client_turn_id:attemptId(key) }));
      turnAttempts.current.delete(key); await refresh(id);
      if (selectedProjectRef.current === id && !note.trim() && window.innerWidth > 700) setWorkspaceView('canvas');
    } catch(e) { if(selectedProjectRef.current === id) setError(errorText(e)); throw e; }
    finally { submissionLock.current = false; setBusy(''); }
  }
  async function selectDirection(gate: Gate, optionId: string) {
    if (submissionLock.current || busy || uploading || uploadLock.current || hasActive) throw new Error('请等待当前操作完成。');
    const current = detailRef.current?.project;
    if (gate.kind !== 'direction' || current?.id !== selectedProjectRef.current) throw new Error('这些方向已更新，请从最新卡片选择。');
    const liveGate = pendingConversationGate(detailRef.current?.gates ?? [], current);
    const key = JSON.stringify([gate.project_id, 'direction', gate.id, optionId, gate.context_version, gate.revision]);
    const body = directionSelectRequest(gate, optionId, current, liveGate?.id, attemptId(key));
    const id = gate.project_id;
    submissionLock.current = true; setBusy('direction'); setError(''); nearBottom.current = true;
    try {
      await api(`/api/projects/${id}/direction-select`, json('POST', body));
      await refresh(id); turnAttempts.current.delete(key);
    } catch (caught) { if (selectedProjectRef.current === id) setError(errorText(caught)); throw caught; }
    finally { submissionLock.current = false; setBusy(''); }
  }
  async function modifyDirections(gate: Gate, content: string) {
    if (submissionLock.current || busy || uploading || uploadLock.current || hasActive) throw new Error('请等待当前操作完成。');
    const current = detailRef.current?.project;
    const liveGate = pendingConversationGate(detailRef.current?.gates ?? [], current);
    if (gate.kind !== 'direction' || liveGate?.kind !== 'direction' || liveGate.id !== gate.id || !isCurrentDirectionGate(liveGate, current) || selectedProjectRef.current !== gate.project_id) throw new Error('这些方向已更新，请在最新卡片下补充要求。');
    const id = gate.project_id;
    submissionLock.current = true; setBusy('direction'); setError(''); nearBottom.current = true;
    setPendingText({ projectId: id, text: content });
    try {
      await submitTurn(id, liveGate.context_version, { content, reference_asset_ids: selectedAsset ? [selectedAsset.id] : [] });
      await refresh(id);
    } catch (caught) { if (selectedProjectRef.current === id) setError(errorText(caught)); throw caught; }
    finally { submissionLock.current = false; setBusy(''); setPendingText(null); }
  }
  function openEditPlan() {
    if (!project || !plan || !canEditPlan || plan.production?.speech_mode === 'none') return;
    if (hasSpeechSpans(plan)) {
      requestPlanChanges();
      setNotice(`在下方告诉我想修改哪句${plan.production?.speech_mode === 'character_dialogue' ? '角色台词' : '旁白'}，我会一起调整对应画面和时间。`);
      return;
    }
    setEditRevision(project.revision);
    setEditPlan(clonePlanForEditing(plan));
  }
  async function generate() {
    if (!project || !canGenerate || submissionLock.current || uploadLock.current) return;
    const id = project.id, revision = project.revision;
    submissionLock.current = true;
    try { await run('generate', async () => {
      await api(`/api/projects/${id}/generate`, json('POST', { accepted_revision: revision, pause_for_creative: true }));
      await refresh(id); if (selectedProjectRef.current === id && window.innerWidth > 700) setWorkspaceView('canvas');
    }); } finally { submissionLock.current = false; }
  }
  async function shortenScript() {
    if (!project || !canPlan || submissionLock.current || uploadLock.current) return;
    const id = project.id, contextVersion = project.context_version;
    submissionLock.current = true;
    try { await run('shorten', async () => {
      await submitTurn(id, contextVersion, { content: '请仅精简当前方案的口播文案以适配已选时长，保留商品事实和镜头，不添加未经确认的卖点。完成后请让我确认。' });
      await refresh(id); if (selectedProjectRef.current === id) setWorkspaceView('chat');
    }); } finally { submissionLock.current = false; }
  }
  async function savePlan() { if (!projectId || !editPlan || editRevision === null || !validEditedPlan(editPlan)) return; await run('edit', async () => { await api(`/api/projects/${projectId}/plan`, json('PATCH', { plan: editPlan, accepted_revision: editRevision })); if (selectedProjectRef.current === projectId) { setEditPlan(null); setEditRevision(null); setNotice('台词已保存，生成前请再确认。'); } await refresh(projectId); }); }
  async function cancel() { if (!projectId) return; await run('cancel', async () => { await api(`/api/projects/${projectId}/cancel`, json('POST', {})); await refresh(projectId); if (selectedProjectRef.current === projectId) setNotice('已请求停止后续工作。供应商已受理的任务仍需核对结果，取消不等于退款。'); }); }
  async function resumeSavedPlanning() {
    if (!projectId || !canPlan || submissionLock.current || uploadLock.current || hasActive) {
      if (projectId) setError('当前不能按已保存的需求继续，请等正在进行的步骤结束。');
      return;
    }
    const id = projectId;
    submissionLock.current = true;
    try { await run('plan', async () => {
      await api(`/api/projects/${id}/resume-planning`, json('POST', {}));
      await refresh(id);
    }); } finally { submissionLock.current = false; }
  }
  async function retry(job: Job) {
    const request = retryRequest(job);
    if (!projectId || !request) {
      setError('这一步不能直接继续，请按已保存的需求重新开始。');
      return;
    }
    if (busy || submissionLock.current) {
      setError('上一次操作还没结束，请稍后再继续。');
      return;
    }
    const id = projectId;
    submissionLock.current = true;
    try { await run('retry', async () => {
      await api(`/api/projects/${id}/retry`, json('POST', request));
      await refresh(id);
    }); } finally { submissionLock.current = false; }
  }
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const closePreview = useCallback(() => setPreviewAsset(null), []);


  const originalAssets = imageAssets.filter(a => !a.metadata.generated);
  const originalProducts = originalAssets.filter(asset => asset.kind === 'product');
  const currentScenes = currentSceneAssets(detail);
  const currentCharacters = currentCharacterAssets(detail);
  const currentStates = currentStateAssets(detail);
  const expectedStates = planStateReferenceCount(plan);
  const hasCurrentDesign = hasOriginalProduct && currentScenes.length > 0 && (plan?.production?.characters ?? []).every(character => currentCharacters.some(asset => asset.metadata.character_id === character.id)) && currentStates.length >= expectedStates;
  const hasViewableResult = !!shownRender || currentScenes.length > 0 || currentCharacters.length > 0 || currentStates.length > 0 || segments.length > 0;
  const voiceNeedsEdit = jobRecovery?.kind === 'edit_voice';
  const sceneStatus = activeJob ? `${phaseNames[activeJob.phase] || '正在准备素材'}${activeJob.phase === 'creating_designs' ? '' : '，场景图尚未就绪'}` : failedJob ? `${phaseNames[failedJob.phase] || '准备素材'}未完成` : unresolved ? '本次生成状态需要核对' : '确认方案后生成场景图';
  const previewIndex = imageAssets.findIndex(a => a.id === previewAsset?.id);
  const sourceRevision = activeJob && ['cta', 'recompose'].includes(activeJob.kind) ? shownRender?.revision : project?.revision;
  const completedSegmentCount = segments.filter(asset => asset.revision === sourceRevision).length;
  const productionSteps = [
    { label: '读取商品素材', done: hasOriginalProduct },
    { label: '准备广告方案', done: !!shownRender || !!plan && activeJob?.kind !== 'plan' },
    { label: '生成视觉设计', done: !!shownRender || hasCurrentDesign },
    { label: `生成视频片段（${shownRender ? expectedSegmentCount : Math.min(completedSegmentCount, expectedSegmentCount)}/${expectedSegmentCount}）`, done: !!shownRender || completedSegmentCount >= expectedSegmentCount },
    { label: '合成与导出', done: !!shownRender },
  ];
  function segmentTiming(asset: Asset) {
    const source = positiveNumber(asset.metadata.video_duration_seconds) ?? positiveNumber(asset.metadata.duration_seconds);
    for (const render of renders) {
      const fps = positiveNumber(render.manifest.fps);
      if (!fps || !Array.isArray(render.manifest.segments)) continue;
      const part = render.manifest.segments.find((value: unknown) => value != null && typeof value === 'object' && (value as Record<string, unknown>).asset_id === asset.id) as Record<string, unknown> | undefined;
      if (part && typeof part.source_in_frame === 'number' && typeof part.source_out_frame === 'number' && part.source_out_frame > part.source_in_frame) return { used: (part.source_out_frame - part.source_in_frame) / fps, source };
    }
    const planned = asset.revision === project?.revision ? plan?.segments.find(segment => segment.id === asset.metadata.segment_id) : undefined;
    return { used: positiveNumber(planned?.duration_seconds), source };
  }
  function referenceAsset(asset: Asset, text = '') { setSelectedAsset(asset); setPreviewAsset(null); if (text.trim()) setDraft(text.trim()); setWorkspaceView('chat'); requestAnimationFrame(() => composerInput.current?.focus()); }
  function openAsset(asset: Asset) { setPreviewAsset(asset); }
  async function renameProject() { if (!project || !newTitle.trim()) return; const id = project.id; await run('rename', async () => { await api(`/api/projects/${id}`, json('PATCH', { title: newTitle.trim() })); if (selectedProjectRef.current === id) setRenameOpen(false); await refresh(id); }); }
  const closeRename = useCallback(() => setRenameOpen(false), []);
  const closeVideo = useCallback(() => setVideoOpen(false), []);
  const closeSidebar = () => setSidebar(false);
  const completedSteps = productionSteps.filter(step => step.done).length;
  const firstMessageId = detail?.messages.find(message => message.role === 'user')?.id;
  const conversationItems = buildConversationItems(detail);
  function showMessage(message: Message) {
    const designs = messageDesignAssets(detail, message);
    return <article key={message.id} className={`message message-${message.role}`}><div className="message-body">{message.id === firstMessageId && <span className="message-tag">@ 商品广告</span>}<p>{planningMessageText(detail, message)}</p></div>{message.id === firstMessageId && originalAssets.length > 0 && <div className="message-images">{originalAssets.map(asset => <button key={asset.id} aria-label={`预览${asset.name}`} onClick={() => openAsset(asset)}><img src={mediaUrl(asset)} alt={asset.name}/></button>)}</div>}{designs.length > 0 && <MessageDesignImages assets={designs} onPreview={openAsset}/>}</article>;
  }
  function showPlan() {
    if (!plan) return null;
    return <section className="plan-card" aria-label="广告方案">
      <div className="plan-topline"><Sparkles size={15}/><strong>广告方案</strong><span>V{project?.revision}</span></div>
      <h3>{plan.facts.name || plan.title}</h3>
      <PlanStrategySummary plan={plan}/>
      <div className="plan-chips"><span>{durationText(planDuration)}</span><span>竖屏</span><span>{soundLabel(plan.audio_strategy, plan.production)}</span></div>
      <ProductionSummary production={plan.production}/>
      {planStale && <p className="plan-stale">这是已保存的方案，新要求尚未形成可用的新方案。</p>}
      <PlanShotDetails plan={plan} onEditNarration={openEditPlan} editDisabled={!canEditPlan}/>
      {!hasActive && !pendingGate && <div className="plan-actions">
        <button className="button subtle" disabled={!canPlan} onClick={requestPlanChanges}><MessageSquare size={13}/>修改方案</button>
        {(planStale || !needsNewPlan) && !(planStale && failedJob?.kind === 'plan') && (!planStale ? <button className="button primary" disabled={!canGenerate} onClick={() => void generate()}>{busy === 'generate' ? <Spinner/> : <Check size={14}/>}确认</button> : <button className="button primary" disabled={!canPlan} onClick={startPlan}><Sparkles size={14}/>更新方案</button>)}
        {hasViewableResult && (needsNewPlan || shownRender) && <button className="button primary" onClick={() => setWorkspaceView('canvas')}><Images size={14}/>{shownRender ? '查看成片' : '查看图片'}</button>}
      </div>}
      {!needsNewPlan && !pendingGate && !hasActive && !planStale && <p className="plan-confirm-note">{plan.production?.characters.length ? '确认后先出场景图和角色参考图，图片一起确认后再生成视频。' : '确认后先出场景图，图片确认后再生成视频。'}{expectedStates > 0 ? '状态变化会另出起始和结果参考图，一并确认。' : ''}</p>}
      {!health?.configured.video && <button className="text-action" onClick={() => setSettingsOpen(true)}>连接生成服务 <ArrowRight size={13}/></button>}
    </section>;
  }
  function designAsset(asset: Asset) {
    const name = asset.kind === 'scene' || asset.kind === 'character' || asset.kind === 'start_frame' ? designImageLabel(asset) : asset.name.replace(/元素图/g, '参考图');
    return <article className="element-card" key={asset.id}><div className="element-role">{asset.metadata.generated ? 'AI 生成参考' : '用户上传'}</div><div className="element-frame"><button className="element-image" aria-label={`查看${name}`} onClick={() => openAsset(asset)}><img src={mediaUrl(asset)} alt={name} loading="lazy"/></button><div className="element-hover"><button aria-label={`引用${name}`} title="引用到对话" onClick={() => referenceAsset(asset)}><AtSign size={15}/></button><button aria-label={`放大${name}`} title="查看详情" onClick={() => openAsset(asset)}><Maximize2 size={15}/></button></div></div><p className="element-name" title={name}>{name}</p></article>;
  }
  function selectView(view: 'chat'|'canvas') { autoCanvasProject.current = projectId || ''; setWorkspaceView(view); }

  return <div className={`app-shell ${projectId ? 'in-project' : 'at-home'}`}>
    {sidebar && <><button className="sidebar-scrim" aria-label="关闭项目列表" onClick={closeSidebar}/><aside className="project-sidebar" aria-label="项目列表"><div className="sidebar-top"><Brand/><button className="icon-button" onClick={closeSidebar} aria-label="关闭项目列表"><PanelLeftClose size={20}/></button></div><button className="new-project" onClick={() => chooseProject(null)}><Plus size={18}/>新建广告</button><span className="section-eyebrow">我的项目</span><nav>{projects.map(p => <button key={p.id} className={`project-link ${p.id === projectId ? 'selected' : ''}`} onClick={() => chooseProject(p.id)}><Film size={18}/><span>{p.title}<small>{dateText(p.updated_at)} · {projectListStatus(p)}</small></span><ChevronRight size={14}/></button>)}{!projects.length && <p className="sidebar-empty">你的作品会保存在这里。</p>}</nav><button className="sidebar-settings" onClick={() => { setSidebar(false); setSettingsOpen(true); }}><Settings2 size={16}/>连接设置</button></aside></>}
    {!projectId ? <Home brand={<Brand/>} durationSeconds={durationSeconds} onDurationChange={setDurationSeconds} draft={draft} onDraft={setDraft} files={pendingFiles} onFiles={files => stageIncoming(files)} onRemoveFile={index => setPendingFiles(files => files.filter((_,i) => i !== index))} onSubmit={send} onOpenProjects={() => setSidebar(true)} onSelectProject={id => chooseProject(id)} projects={projects} busy={!!busy || uploading} online={online} plannerConfigured={!!health?.configured.planner} onSettings={() => setSettingsOpen(true)} loading={loading} error={error} onDismissError={() => setError('')}/> : <div className="main-shell">
      <header className="studio-header">
        <div className="header-left"><button className="brand-link" aria-label="返回首页" onClick={() => chooseProject(null)}><Brand/></button><span className="header-divider"/><button className="project-heading" onClick={() => { if (project) { setNewTitle(project.title); setRenameOpen(true); } }} aria-label="修改项目名称"><span>{project?.title || '正在打开项目'}</span><Pencil size={14}/></button></div>
        <nav className="view-switch" role="tablist" aria-label="工作区视图"><button role="tab" aria-selected={workspaceView === 'chat'} onClick={() => selectView('chat')}><MessageSquare size={16}/>对话</button><button role="tab" aria-selected={workspaceView === 'canvas'} onClick={() => selectView('canvas')}><Images size={16}/>画布</button><button role="tab" aria-selected={false} disabled title="时间线剪辑将在后续版本开放"><Layers3 size={16}/>时间线</button></nav>
        <div className="header-right"><div className="status-anchor"><button className={`status-button ${hasActive ? 'is-working' : ''}`} aria-expanded={statusOpen} aria-label="查看创作状态" onClick={() => setStatusOpen(!statusOpen)}>{hasActive ? <Spinner/> : <span className="status-ring"/>}<span>状态</span></button></div>{shownRender && <a className="round-button" href={downloadUrl(shownRender.asset_id)} download title="下载当前成片" aria-label="下载当前成片"><Download size={18}/></a>}<button className="round-button" aria-label="打开我的项目" title="我的项目" onClick={() => setSidebar(true)}><FolderOpen size={18}/></button><button className="round-button" aria-label="查看服务配置" title="连接设置" onClick={() => setSettingsOpen(true)}><Settings2 size={18}/></button><span className={`local-badge ${!online ? 'offline' : ''}`}><span/>{online ? '本地工作台' : '连接中断'}</span></div>
      </header>
      {statusOpen && <><button className="popover-dismiss" aria-label="关闭创作状态" onClick={() => setStatusOpen(false)}/><aside className="status-popover" aria-label="创作状态"><header><strong>{shownRender ? '作品与进度' : '创作状态'} {!shownRender && <small>{completedSteps}/{productionSteps.length}</small>}</strong><button className="icon-button" aria-label="关闭创作状态面板" onClick={() => setStatusOpen(false)}><X size={16}/></button></header>{!shownRender && <ol>{productionSteps.map((step,i) => <li key={step.label} className={step.done ? 'done' : ''}>{step.done ? <CheckCircle2 size={16}/> : hasActive && i === completedSteps ? <Spinner/> : <span className="step-dot"/>}{step.label}</li>)}</ol>}{shownRender && <p className="popover-progress">已有成片 V{shownRender.revision} · {resultWorkLabel(detail)}</p>}{activeJob && <p className="popover-progress">{phaseNames[activeJob.phase] || '正在制作'} · {elapsedText(activeJob.started_at ?? activeJob.created_at)}{hasProviderProgress && <small>当前视频段 · {Math.round(providerProgress as number)}%</small>}</p>}<div className="latest-assets"><div><strong>最新文件</strong><button className="mini-primary" onClick={() => { setWorkspaceView('canvas'); setStatusOpen(false); }}>打开画布</button></div><small>{imageAssets.length} 张图片 · {renders.length} 个成片版本</small><div className="latest-thumbs">{imageAssets.slice(-3).map(a => <button key={a.id} aria-label={`预览${a.name}`} onClick={() => { setStatusOpen(false); openAsset(a); }}><img src={mediaUrl(a)} alt={a.name}/></button>)}</div></div></aside></>}
      {!online && <div className="connection-banner" role="status"><TriangleAlert size={15}/><span>暂时无法连接制作服务，已有成果会保留。</span><button onClick={() => { void loadHealth(); if (projectId) void refresh(projectId).catch(e => setError(errorText(e))); }}>重新连接</button></div>}
      <main className={`workspace view-${workspaceView}`} style={{'--chat-width': `${chatWidth}px`} as CSSProperties}>
        <section className="conversation-panel" aria-label="广告制作对话">
          <div className="conversation-scroll" ref={conversationScroll} onScroll={e => { const el = e.currentTarget; nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 90; }}>
            {loading && !detail && <div className="conversation-loading"><Spinner/>正在打开对话…</div>}
            {conversationItems.map(item => item.type === 'plan' ? <div key={item.id}>{showPlan()}</div> : item.type === 'message' ? showMessage(item.message) : item.type === 'gate' ? item.gate.status === 'pending' && item.gate.id === pendingGate?.id ? null :
              <ConversationGate key={item.id} gate={item.gate.status === 'pending' ? { ...item.gate, status: 'superseded' } : item.gate} busy={!!busy || uploading || hasActive} error={undefined} onSubmit={answers => answerGate(item.gate, answers)} onCreativeSubmit={(optionId,note) => confirmCreative(item.gate,optionId,note)}/> :
              <RunTimeline key={item.id} job={item.job} events={item.events} notes={planningNotes(detail, item.job)} hideError={!!jobRecovery && item.job.id === latestJob?.id}/>)}
            {pendingText?.projectId === projectId && !detail?.messages.some(message => message.role === 'user' && message.content === pendingText.text && Date.now()/1000-message.created_at<10) && <article className="message message-user pending-message"><div className="message-body"><p>{pendingText.text}</p><small><Spinner/>正在发送</small></div></article>}
            {!!busy && ['message','answer','creative','direction','plan','shorten'].includes(busy) && !hasActive && <div className="conversation-loading" role="status"><Spinner/>正在提交本轮请求…</div>}

            <SavedResultCard detail={detail} onPlay={() => setVideoOpen(true)}/>
            {latestJob && jobRecovery && <JobRecoveryCard job={latestJob} hasExistingResult={!!shownRender} busy={busy} canEditVoice={!!plan} canShorten={canPlan} error={error} onDismissError={() => setError('')} onRetry={job => void retry(job)} onEditVoice={openEditPlan} onShorten={() => void shortenScript()} onChangePlan={requestPlanChanges} onRestart={() => void resumeSavedPlanning()} onSettings={() => setSettingsOpen(true)} onUpload={() => fileInput.current?.click()}/>}
            {!!project && !hasActive && !plan && !pendingGate && !jobs.length && <div className="next-step"><p>你的消息已保存，可以继续对话。</p><button className="button primary" disabled={!canPlan} onClick={startPlan}><MessageSquare size={15}/>继续对话</button>{!health?.configured.planner && <button className="text-action" onClick={() => setSettingsOpen(true)}>连接策划服务</button>}</div>}
            {error && !(latestJob && jobRecovery) && <div className="inline-alert" role="alert"><TriangleAlert size={15}/><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError('')}><X size={14}/></button></div>}{notice && <div className="inline-notice" role="status"><CheckCircle2 size={14}/>{notice}</div>}<div ref={endMessages}/>
          </div>
          <div className={`composer-area ${pendingGate ? 'has-pending-gate' : ''}`}><AdFormSummary adForm={project?.ad_form} onChange={online && health?.configured.planner && !hasActive && !busy && !uploading && (!pendingGate || pendingGate.kind === 'direction') ? focusAdFormChange : undefined}/><div className={`composer-shell ${pendingGate ? 'has-pending-gate' : ''}`} role="group" aria-label={pendingGate?.kind === 'direction' ? '选择方向与输入' : pendingGate ? '确认问题与输入' : '发送消息'}>{pendingGate && <div className="composer-gate-scroll"><ConversationGate key={pendingGate.id} ref={pendingGateRef} composerText={draft} onComposerStateChange={handleGateComposer} onReviewImages={() => selectView('canvas')} gate={pendingGate} busy={!!busy || uploading || hasActive} error={error} onSubmit={answers => answerGate(pendingGate, answers)} onCreativeSubmit={(optionId,note) => confirmCreative(pendingGate,optionId,note)} onDirectionSelect={optionId => selectDirection(pendingGate,optionId)} onDirectionModify={text => modifyDirections(pendingGate,text)}/></div>}{selectedAsset && <div className="selected-reference"><FileImage size={13}/><span>引用 {selectedAsset.name}</span><button className="icon-button" aria-label="取消素材引用" onClick={() => setSelectedAsset(null)}><X size={13}/></button></div>}{pendingFiles.length > 0 && <ComposerImageDraft files={pendingFiles} disabled={!!busy || uploading || hasActive} onRemove={index => setPendingFiles(files => files.filter((_, item) => item !== index))}/>}<form className="chat-composer" onSubmit={send} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!hasActive && !busy) stageIncoming(e.dataTransfer.files); }}><button type="button" className="attach-button" disabled={uploading || hasActive || !!busy} aria-label="添加商品图片" title="添加商品图片" onClick={() => fileInput.current?.click()}>{uploading ? <Spinner/> : <Plus size={20}/>}</button><textarea ref={composerInput} aria-label={pendingGate && gateComposer ? gateComposer.prompt : "补充需求或修改意见"} maxLength={pendingGate ? gateComposer?.maxLength ?? 500 : 6000} value={draft} onChange={e => { setDraft(e.target.value); if (pendingGate) pendingGateRef.current?.cacheText(e.target.value); }} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }} onPaste={e => { const files = [...e.clipboardData.files]; if (files.length && !hasActive && !busy) { e.preventDefault(); stageIncoming(files); } }} placeholder={hasActive ? '正在工作，你可以随时停止…' : pendingFiles.length ? '写上商品信息，再和图片一起发送…' : pendingGate ? gateComposer?.prompt || '在这里回答当前问题…' : '需要修改？在这里输入…'} rows={1} disabled={hasActive || !!busy || !!pendingGate && gateComposer?.canInput === false}/>{hasActive ? <button type="button" className="stop-button" aria-label="停止制作" title="停止后续制作" disabled={!!busy || activeJob.status === 'cancel_requested'} onClick={cancel}><Square size={13} fill="currentColor"/></button> : <button type="submit" className="send-button" aria-label={pendingGate?.kind === "clarification" ? "发送当前题答案" : "发送修改意见"} disabled={!draft.trim() || !!busy || uploading || !online || !!pendingGate && gateComposer?.canInput === false}>{busy === 'message' ? <Spinner/> : <ArrowRight size={19}/>}</button>}</form></div><p className="composer-note">请核对商品信息和生成内容。<span> Enter 发送 · Shift + Enter 换行</span></p><input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={e => { if(e.target.files) stageIncoming(e.target.files); e.target.value = ''; }}/></div>
        </section>
        <div className="panel-resizer" role="separator" tabIndex={0} aria-label="调整对话与画布宽度" aria-orientation="vertical" aria-valuenow={chatWidth} aria-valuemin={300} aria-valuemax={560} onKeyDown={e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); setChatWidth(w => Math.min(560,Math.max(300,w + (e.key === 'ArrowRight' ? 20 : -20)))); } }} onPointerDown={e => { e.currentTarget.setPointerCapture(e.pointerId); }} onPointerMove={e => { if (e.currentTarget.hasPointerCapture(e.pointerId)) setChatWidth(Math.max(300,Math.min(560,window.innerWidth * .47,e.clientX))); }} onPointerUp={e => { if(e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }}/>
        <section className="canvas-panel" aria-label="视觉设计与视频画布"><div className="canvas-heading"><span>画布</span><button className="icon-button" aria-label="收起画布，返回对话" title="收起画布" onClick={() => setWorkspaceView('chat')}><PanelLeftOpen size={16}/></button><span className="canvas-saved">{hasActive ? '正在创作' : pendingGate ? '等待你的确认' : '已保存'}</span></div><div className="canvas-scroll">
          <section className={`canvas-card design-card ${activeJob?.phase === 'creating_designs' ? 'is-processing' : ''}`}><header><h2>视觉设计</h2><button className="icon-button" title="添加商品图" aria-label="补充商品图" disabled={hasActive || uploading || !!busy} onClick={() => fileInput.current?.click()}><Plus size={18}/></button></header>
            <div className="design-zones"><section className="design-zone" aria-label="商品原图"><h3><ImagePlus size={14}/>商品原图</h3>{originalProducts.length ? <div className="element-grid">{originalProducts.map(designAsset)}</div> : <button className="design-placeholder canvas-upload" onClick={() => fileInput.current?.click()} disabled={!!busy || hasActive}><ImagePlus size={27}/><span>添加商品图片</span><small>保留真实商品外观与细节</small></button>}</section>
            <section className="design-zone" aria-label="场景图"><h3><Images size={14}/>场景图</h3>{currentScenes.length ? <div className="element-grid">{currentScenes.map(designAsset)}</div> : <div className={`design-placeholder ${failedJob ? 'has-error' : ''}`} role="status">{activeJob?.phase === 'creating_designs' ? <Spinner/> : failedJob ? <TriangleAlert size={24}/> : <Images size={27}/>}<strong>{sceneStatus}</strong><small>{failedJob?.error || (pendingGate?.kind === 'direction' ? '选好方向并确认完整方案后，再生成场景图' : pendingGate?.kind === 'clarification' ? '先完成需求确认' : '成功生成后在这里展示实际图片')}</small></div>}</section>
            {currentCharacters.length > 0 && <section className="design-zone design-zone-characters" aria-label="角色参考图"><h3><Images size={14}/>角色参考图</h3><div className="element-grid">{currentCharacters.map(designAsset)}</div></section>}
            {(currentStates.length > 0 || expectedStates > 0) && <section className="design-zone" aria-label="状态参考图"><h3><Images size={14}/>状态参考图</h3>{currentStates.length ? <div className="element-grid">{currentStates.map(designAsset)}</div> : <div className="design-placeholder" role="status"><Images size={27}/><strong>确认方案后生成起始和结果参考图</strong><small>过程镜对齐起始静帧，切到结果镜再对齐结果那一张</small></div>}</section>}</div>
          </section>
          <section className={`canvas-card video-card ${hasActive && activeJob?.kind !== 'plan' ? 'is-processing' : ''}`} ref={playerRef}><header><h2>视频生成</h2>{shownRender && <span className="canvas-version">已完成 V{shownRender.revision}</span>}</header>{shownRender ? <div className="video-result"><div className="video-tile"><span className="video-label">Video {shownRender.revision}</span><video key={shownRender.id} controls playsInline preload="metadata" src={shownRender.url || `/media/${shownRender.asset_id}`} aria-label={`广告成片修订${shownRender.revision}`}/><button className="expand-video" aria-label="查看视频详情" onClick={() => setVideoOpen(true)}><Maximize2 size={13}/>查看详情</button></div><div className="video-result-actions"><a className="button subtle small" download href={downloadUrl(shownRender.asset_id)}><Download size={14}/>下载视频</a></div><p className="result-footnote">{durationText(renderDuration(shownRender))} · 9:16 · {soundLabel(shownRender.manifest.audio_strategy, shownRender.manifest.production)}{' · '}{resultWorkLabel(detail)}</p></div> : <div className="video-empty">{pendingGate?.kind === 'creative' ? <><div className="video-placeholder-icon"><Film size={25}/></div><p>参考图片已就绪，检查图片后开始制作视频。</p><button className="button primary small" onClick={() => selectView('chat')}>检查图片</button></> : hasActive && activeJob?.kind !== 'plan' ? <><div className="video-generating-frame"><span className="generation-orbit"/><span>{['queued','synthesizing_voice','creating_designs'].includes(activeJob.phase) ? '准备素材中' : '正在生成'}</span></div><p>{phaseNames[activeJob.phase] || '正在准备制作'}</p></> : failedJob ? <><div className="video-placeholder-icon"><TriangleAlert size={25}/></div><p>{voiceNeedsEdit ? '台词需要缩短，当前还没有成片' : '本次制作尚未完成，当前还没有成片'}</p><button className="button subtle small" onClick={() => selectView('chat')}>{voiceNeedsEdit ? '前往修改台词' : '查看需要处理的步骤'}</button></> : <><div className="video-placeholder-icon"><Film size={25}/></div><p>{plan ? '确认方案后准备参考图片，检查图片后制作视频' : '你的广告视频会出现在这里'}</p>{plan && <button className="button primary small" disabled={!canGenerate} onClick={() => void generate()}>{busy === 'generate' ? <Spinner/> : <Check size={14}/>}确认</button>}</>}</div>}
          {segments.length > 0 && <details className="canvas-disclosure" open={showSegments} onToggle={e => setShowSegments(e.currentTarget.open)}><summary><ChevronRight size={14}/>生成片段 <span>{segments.length}</span></summary><div className="segment-grid">{segments.map((asset,i) => { const timing = segmentTiming(asset); return <div className="segment-card" key={asset.id}><video controls playsInline preload="metadata" src={mediaUrl(asset)} aria-label={`生成片段${i+1}`}/><div><span>片段 {i+1}<small>{timing.used == null ? "使用时长待核对" : `成片使用 ${durationText(timing.used)}`}</small></span><a href={downloadUrl(asset.id)} download aria-label={`下载片段${i+1}`}><Download size={14}/></a></div>{timing.source != null && (timing.used == null || Math.abs(timing.source - timing.used) > 0.05) && <p className="segment-duration-note">原片 {durationText(timing.source)}{timing.used == null ? "" : "，按方案截取"}</p>}<button className="text-action" onClick={() => referenceAsset(asset)}>针对这段提意见</button></div>; })}</div></details>}
          {renders.length > 1 && <details className="canvas-disclosure" open={showHistory} onToggle={e => setShowHistory(e.currentTarget.open)}><summary><ChevronRight size={14}/>历史版本 <span>{renders.length}</span></summary>{renders.map(render => <div className="history-row" key={render.id}><Film size={15}/><span>视频 V{render.revision}<small>{dateText(render.created_at)} · {durationText(renderDuration(render))}{render.id === shownRender?.id ? ' · 当前显示' : ''}</small></span><a href={render.url || `/media/${render.asset_id}`} target="_blank" rel="noreferrer" className="text-action">打开</a><a href={downloadUrl(render.asset_id)} download aria-label={`下载成片V${render.revision}`} className="icon-button"><Download size={15}/></a></div>)}</details>}</section>
        </div></section>
      </main>
    </div>}
    {settingsOpen && <Modal title="制作服务配置" onClose={closeSettings}><p className="modal-description">密钥仅保存在服务端配置中，页面不会读取或显示密钥。</p><div className="config-item"><span><Sparkles size={18}/><strong>脚本策划服务{health?.providers.planner ? ` · ${health.providers.planner}` : ''}</strong></span><b className={health?.configured.planner ? 'configured' : ''}>{health?.configured.planner ? '已配置' : '未配置'}</b></div><div className="config-item"><span><Film size={18}/><strong>视频生成服务</strong></span><b className={health?.configured.video ? 'configured' : ''}>{health?.configured.video ? '已配置' : '未配置'}</b></div><div className="config-instruction"><strong>打开本地连接设置</strong><p>在本地设置页按当前服务填写 API 密钥，保存密钥后立即生效，无需重启。已配置不代表能力已验证，首次生成仍需检查实际结果。</p><a className="button primary" href="/setup" target="_blank" rel="noreferrer">打开连接设置 <ArrowRight size={14}/></a><p>独立主播口播入口暂未开放。商品广告中的角色说话使用视频模型原声，实际声音与口型需检查成片。</p></div>{health?.limitations?.length ? <details className="limitations"><summary>当前验证范围</summary><ul>{health.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul></details> : null}<div className="modal-actions"><button className="button" onClick={() => void loadHealth()}><RefreshCw size={14}/>重新检查配置</button><button className="button primary" onClick={closeSettings}>知道了</button></div></Modal>}
    {editPlan && <Modal title="修改台词" onClose={() => setEditPlan(null)}><p className="modal-description">修改后需要重新生成视频才会生效，现有成片会保留。</p>{editRevision !== project?.revision && <p role="alert">方案已更新，请关闭弹窗后重新打开当前版本。</p>}{error && <p role="alert">{error}</p>}<PlanEditor plan={editPlan} onChange={setEditPlan}/><div className="modal-actions"><button className="button" onClick={() => setEditPlan(null)}>取消</button><button className="button primary" disabled={!!busy || !validEditedPlan(editPlan) || editRevision !== project?.revision} onClick={savePlan}>{busy === 'edit' ? <Spinner/> : <Check size={15}/>}保存台词</button></div></Modal>}
    {renameOpen && <Modal title="修改项目名称" onClose={closeRename}><form onSubmit={e => { e.preventDefault(); void renameProject(); }}><label className="field-label" htmlFor="project-name">项目名称</label><input autoFocus id="project-name" className="form-input" value={newTitle} maxLength={80} onChange={e => setNewTitle(e.target.value)}/>{error && <p role="alert">{error}</p>}<div className="modal-actions"><button type="button" className="button" onClick={closeRename}>取消</button><button className="button primary" disabled={!!busy || !newTitle.trim()}>{busy === 'rename' ? <Spinner/> : <Check size={15}/>}保存</button></div></form></Modal>}
    {previewAsset && <Modal title={`视觉设计 ${previewIndex + 1}/${imageAssets.length}`} className="asset-viewer" onClose={closePreview}><div className="asset-viewer-content"><div className="asset-stage"><img src={mediaUrl(previewAsset)} alt={previewAsset.name}/><div className="asset-tools"><a href={downloadUrl(previewAsset.id)} download className="round-button" aria-label="下载参考图"><Download size={17}/></a><button className="round-button" aria-label="引用到对话" onClick={() => referenceAsset(previewAsset)}><AtSign size={17}/></button></div>{previewIndex > 0 && <button className="gallery-arrow previous" aria-label="上一张设计图" onClick={() => { setPreviewAsset(imageAssets[previewIndex - 1]); }}><ChevronLeft/></button>}{previewIndex < imageAssets.length - 1 && <button className="gallery-arrow next" aria-label="下一张设计图" onClick={() => { setPreviewAsset(imageAssets[previewIndex + 1]); }}><ChevronRight/></button>}</div><aside className="asset-inspector"><h3>素材与修改意见</h3><div className="asset-description"><div className="asset-type"><FileImage size={17}/><span>{kindNames[previewAsset.kind]}</span><small>{previewAsset.metadata.generated ? 'AI 生成参考' : '用户上传'}</small></div><strong>{previewAsset.name}</strong><p>{previewAsset.kind === 'character' ? '这张参考图用于统一视频中的角色外观。请检查人物与方案是否一致。' : previewAsset.metadata.state_reference ? '这张静帧锁定这一镜的起始或结果。请核对状态是否和方案一致，商品外形是否仍是原图。' : previewAsset.metadata.generated ? '这张图用于统一视频中的商品或场景。请确认外观与原始商品一致。' : '这是你上传的商品素材，用于确认商品外观、颜色与细节。'}</p><small>需要调整时，引用图片后在底部对话框输入修改意见。</small></div><button className="button primary" onClick={() => referenceAsset(previewAsset)}><AtSign size={16}/>引用到对话</button></aside></div></Modal>}
    {videoOpen && shownRender && <Modal title={`视频 V${shownRender.revision}`} className="video-viewer" onClose={closeVideo}><div className="video-viewer-content"><video controls autoPlay playsInline src={shownRender.url || `/media/${shownRender.asset_id}`} aria-label="放大的广告视频"/><aside><h3>{project?.title}</h3><p>{durationText(renderDuration(shownRender))} · 720 × 1280 · {soundLabel(shownRender.manifest.audio_strategy, shownRender.manifest.production)}</p><p>查看完整视频，确认商品、镜头衔接与声音。</p><a className="button primary" href={downloadUrl(shownRender.asset_id)} download><ArrowDownToLine size={16}/>下载视频</a></aside></div></Modal>}
  </div>;
}
