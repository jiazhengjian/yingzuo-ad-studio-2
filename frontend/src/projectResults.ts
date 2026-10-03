import { activeStatus } from './api';
import type { Detail, Project, Render } from './api';

const ready = (render: Render) => ['ready', 'completed', 'succeeded'].includes(render.status);

/** Published work remains available independently of the latest attempt. */
export function playableRender(detail?: Detail | null): Render | undefined {
  if (!detail) return undefined;
  const renders = detail.renders.filter(ready).sort((a, b) => b.created_at - a.created_at);
  return renders.find(render => render.id === detail.project.current_render_id
    || render.asset_id === detail.project.current_render_id) ?? renders[0];
}

export function resultWorkLabel(detail?: Detail | null): string {
  if (!detail || !playableRender(detail)) return '';
  const { project } = detail;
  const jobs = [...detail.jobs].sort((a, b) => b.created_at - a.created_at);
  const active = jobs.find(job => activeStatus(job.status));
  const latest = active ?? jobs.find(job => job.revision === project.revision);
  const stale = !!project.plan && project.context_version !== project.plan_context_version;
  if (active) return active.kind === 'plan' ? '正在修改方案，已有成片保留' : '新版制作中，已有成片保留';
  if (latest && ['failed', 'partial'].includes(latest.status)) return '本次修改未完成，已有成片保留';
  if (latest && ['submitted_unknown', 'needs_review'].includes(latest.status)) return '本次制作结果待核对，已有成片保留';
  if (latest?.status === 'canceled') return '本次修改已停止，已有成片保留';
  if (stale) return '新要求尚未形成方案，已有成片保留';
  if (project.revision !== playableRender(detail)?.revision) return '新方案尚未生成视频，已有成片保留';
  return '成片已保存，可随时播放或下载';
}

export function projectListStatus(project: Project): string {
  const status = project.work_status ?? project.status;
  if (project.result_status === 'ready' || project.result_status == null && (project.current_render_id || project.status === 'ready')) {
    const current = ['failed', 'partial'].includes(status) ? ' · 本次修改未完成'
      : ['queued', 'running', 'planning', 'generating', 'composing'].includes(status) ? ' · 正在修改' : '';
    return `已有成片${current}`;
  }
  if (['queued', 'running', 'planning', 'generating', 'composing', 'cancel_requested'].includes(status)) return '制作中';
  return ['failed', 'partial'].includes(status) ? '本次创作未完成' : '继续创作';
}
