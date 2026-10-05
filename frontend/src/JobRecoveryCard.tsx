import { LoaderCircle, Pencil, RefreshCw, Sparkles, TriangleAlert, X } from 'lucide-react';
import type { Job } from './api';
import { cardMessage, recoveryForJob, sanitizeCardText } from './jobRecovery';

interface Props {
  job: Job;
  busy: string;
  canEditVoice: boolean;
  canShorten: boolean;
  hasExistingResult?: boolean;
  onRetry: (job: Job) => void;
  onEditVoice: () => void;
  onShorten: () => void;
  onChangePlan: () => void;
  onRestart: () => void;
  onSettings: () => void;
  onUpload?: () => void;
  error?: string;
  onDismissError?: () => void;
}

export default function JobRecoveryCard({ job, busy, canEditVoice, canShorten, hasExistingResult = false, onRetry, onEditVoice, onShorten, onChangePlan, onRestart, onSettings, onUpload, error, onDismissError }: Props) {
  const recovery = recoveryForJob(job);
  if (!recovery) return null;
  const retryingVideo = recovery.kind === 'retry_failed_segments';
  const planning = job.kind === 'plan';
  const incompleteCheck = planning && job.progress.error_code === 'planning_review_incomplete';
  const title = incompleteCheck ? '本次检查未完成' : planning ? hasExistingResult ? '本次修改未完成' : '本次方案未完成'
    : recovery.kind === 'reconcile' ? '提交结果待核对' : retryingVideo ? '视频片段未生成' : '本次制作未完成';
  const message = cardMessage(job, recovery);
  const shownError = error ? sanitizeCardText(error) : '';
  const issues = planning && recovery.kind === 'edit_plan' && Array.isArray(job.progress.issue_summary)
    ? job.progress.issue_summary.filter((issue): issue is string => typeof issue === 'string' && !!issue.trim() && sanitizeCardText(issue) === issue.trim()).slice(0, 4) : [];
  return <div className="alert-card" role="status"><TriangleAlert size={17}/><div>
    <strong>{title}</strong><p>{message}</p>
    {issues.length > 0 && <ul className="planning-issue-summary">{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}
    {retryingVideo && <p>保留已有图片与成功片段。</p>}
    {recovery.kind === 'check_materials' && <button className="button subtle small" disabled={!!busy} onClick={onUpload}>重新上传商品图</button>}
    {planning && recovery.kind === 'edit_plan' && <p>可以在下方输入想调整的要求，继续修改方案。</p>}
    {recovery.kind === 'continue_conversation' && <p>这一步不能沿用刚才的进度。点下面会按已保存的需求和已选方向重新开始。</p>}
    {hasExistingResult && <p>已有成片仍可播放、下载。</p>}
    {recovery.kind === 'upload_product' ? <button className="button subtle small" disabled={!!busy} onClick={onUpload}>重新上传商品图</button> : recovery.kind === 'check_connection' ? <button className="button subtle small" disabled={!!busy} onClick={onSettings}>检查连接设置</button> : recovery.kind === 'continue_conversation' ? <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onRestart}><RefreshCw size={13}/>按已保存的需求继续</button> : recovery.kind === 'edit_plan' ? <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onChangePlan}><Pencil size={13}/>{recovery.label || (planning ? '输入修改要求' : '修改方案')}</button> : recovery.kind === 'edit_voice' ? <div className="alert-actions">
      <button className="button subtle small" disabled={!!busy || !canEditVoice} onClick={onEditVoice}><Pencil size={13}/>修改台词</button>
      <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onShorten}>{busy === 'shorten' ? <LoaderCircle size={13} className="spin"/> : <Sparkles size={13}/>}自动精简台词</button>
    </div> : (recovery.kind === 'retry_planning' || recovery.kind === 'retry_existing' || recovery.kind === 'retry_failed_segments') && <button className="button subtle small" disabled={!!busy} onClick={() => onRetry(job)}>
      {busy === 'retry' ? <LoaderCircle size={13} className="spin"/> : <RefreshCw size={13}/>} {recovery.label}
    </button>}
    {!!shownError && <p role="alert">{shownError}{onDismissError && <button aria-label="关闭错误提示" onClick={onDismissError}><X size={14}/></button>}</p>}
  </div></div>;
}
