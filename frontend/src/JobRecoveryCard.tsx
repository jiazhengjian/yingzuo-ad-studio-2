import { LoaderCircle, Pencil, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react';
import type { Job } from './api';
import { recoveryForJob } from './jobRecovery';

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
  onSettings: () => void;
}

export default function JobRecoveryCard({ job, busy, canEditVoice, canShorten, hasExistingResult = false, onRetry, onEditVoice, onShorten, onChangePlan, onSettings }: Props) {
  const recovery = recoveryForJob(job);
  if (!recovery) return null;
  const retryingVideo = recovery.kind === 'retry_failed_segments';
  const planning = job.kind === 'plan';
  const incompleteCheck = planning && job.progress.error_code === 'planning_review_incomplete';
  const title = incompleteCheck ? '本次检查未完成' : planning ? hasExistingResult ? '本次修改未完成' : '本次方案未完成'
    : recovery.kind === 'reconcile' ? '提交结果待核对' : retryingVideo ? '视频片段未生成' : '本次制作未完成';
  const message = recovery.kind === 'retry_planning'
    ? '当前步骤经过自动重试仍未完成，已保存本轮进度，可稍后重新尝试。'
    : recovery.kind === 'check_connection'
    ? '策划服务的连接配置或账户状态需要检查，已有内容已保存。'
    : job.progress.error_code === 'planning_request_limit'
    ? '本轮处理已达到次数上限，已停止继续尝试。原需求和已有内容仍然保留。'
    : incompleteCheck
    ? '系统未能完成这次方案检查，尚未采用本轮草稿。已保存的内容仍然保留。'
    : job.progress.error_code === 'planning_invalid_response'
    ? '本次回复未能形成完整方案，原需求和已有内容仍然保留。'
    : job.progress.error_code === 'reference_image_unreadable'
    ? '视频服务未能读取参考图片。'
    : job.error || (recovery.kind === 'reconcile' ? '平台可能已受理，暂不重复提交。' : '已完成的素材仍然保留。');
  const issues = planning && recovery.kind === 'edit_plan' && Array.isArray(job.progress.issue_summary)
    ? job.progress.issue_summary.filter((issue): issue is string => typeof issue === 'string' && !!issue.trim()).slice(0, 4) : [];
  return <div className="alert-card" role="status"><TriangleAlert size={17}/><div>
    <strong>{title}</strong><p>{message}</p>
    {issues.length > 0 && <ul className="planning-issue-summary">{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}
    {retryingVideo && <p>保留已有图片与成功片段。</p>}
    {recovery.kind === 'check_materials' && <p>请检查素材，或在下方对话调整并重新确认方案。</p>}
    {planning && recovery.kind === 'edit_plan' && <p>可以在下方输入想调整的要求，继续修改方案。</p>}
    {recovery.kind === 'continue_conversation' && <p>可以在下方继续对话；原需求已保存。</p>}
    {hasExistingResult && <p>已有成片仍可播放、下载。</p>}
    {recovery.kind === 'check_connection' ? <button className="button subtle small" disabled={!!busy} onClick={onSettings}>检查连接设置</button> : recovery.kind === 'continue_conversation' ? <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onChangePlan}><Pencil size={13}/>继续对话</button> : recovery.kind === 'edit_plan' ? <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onChangePlan}><Pencil size={13}/>{planning ? '输入修改要求' : '修改方案'}</button> : recovery.kind === 'edit_voice' ? <div className="alert-actions">
      <button className="button subtle small" disabled={!!busy || !canEditVoice} onClick={onEditVoice}><Pencil size={13}/>修改台词</button>
      <button className="button subtle small" disabled={!!busy || !canShorten} onClick={onShorten}>{busy === 'shorten' ? <LoaderCircle size={13} className="spin"/> : <Sparkles size={13}/>}自动精简台词</button>
    </div> : (recovery.kind === 'retry_planning' || recovery.kind === 'retry_existing' || recovery.kind === 'retry_failed_segments') && <button className="button subtle small" disabled={!!busy} onClick={() => onRetry(job)}>
      {busy === 'retry' ? <LoaderCircle size={13} className="spin"/> : <RefreshCw size={13}/>} {recovery.label}
    </button>}
  </div></div>;
}
