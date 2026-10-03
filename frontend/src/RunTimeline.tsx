import { useEffect, useId, useMemo, useState } from 'react';
import { Check, ChevronDown, Circle, Clock3, LoaderCircle, MessageSquare, Square, TriangleAlert } from 'lucide-react';
import type { Job, MediaSkillUsage, RunEvent } from './api';
import type { PlanningNote } from './planningDisplay';
import { runTiming } from './runTiming';
import './conversation.css';

export interface RunTimelineProps { job: Job; events: RunEvent[]; notes?: PlanningNote[]; hideError?: boolean }
type DisplayState = RunEvent['state'] | 'recorded';
const readableDuration = (seconds: number) => {
  const value = Math.max(0, Math.floor(seconds));
  return value < 60 ? `${value}s` : value < 3600 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
};
const timeLabel = (seconds: number) => Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '';
const statusLabel = (job: Job) => {
  if (job.status === 'running') return '正在工作';
  if (job.status === 'queued') return '等待开始';
  if (job.status === 'awaiting_input') return job.phase === 'awaiting_direction' ? '等待选择广告方向' : job.kind === 'generate' ? '等待检查图片' : '等待你的回答';
  if (job.status === 'cancel_requested') return '正在停止后续工作';
  if (job.status === 'canceled') return '本轮已停止';
  if (job.status === 'submitted_unknown') return '提交结果待核对';
  if (job.status === 'needs_review') return '本轮需要复核';
  if (job.status === 'failed') return '本轮未完成';
  if (job.status === 'partial') return '本轮部分完成';
  if (job.status === 'awaiting_generate') return '方案已准备好';
  return ['ready', 'completed', 'succeeded'].includes(job.status) ? '本轮已完成' : '本轮执行记录';
};

/** Persisted compiler usage, without inventing a new event, timestamp or model call. */
export function MediaSkillRecords({ records }: { records: MediaSkillUsage[] }) {
  if (records.length === 0) return null;
  return <section className="run-skill-usage" aria-label="素材与视频使用的提示词模板">
    <h4>使用的提示词模板</h4>
    <ul>{records.map(record => <li key={`${record.id}:${record.stage}:${record.version}:${record.content_sha256}`}>
      <span className="run-skill-name">{record.display_name}<span className="run-skill-version">v{record.version}</span></span>
      <span className="run-skill-role">{record.stage === 'asset_prompts' ? '图片提示词编译' : '视频提示词编译'}</span>
    </li>)}</ul>
  </section>;
}

/** Public task events, not model reasoning or a simulated token stream. */
export default function RunTimeline({ job, events, notes = [], hideError = false }: RunTimelineProps) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [expanded, setExpanded] = useState(false);
  const eventListId = useId();
  const running = job.status === 'running';
  const waiting = job.status === 'awaiting_input';
  const stopped = job.status === 'canceled' || job.status === 'cancel_requested';
  const mediaSkills = job.media_skill_usage ?? [];
  const failed = ['failed', 'partial', 'submitted_unknown', 'needs_review'].includes(job.status);
  const terminal = ['ready', 'completed', 'succeeded', 'failed', 'canceled', 'partial'].includes(job.status);
  const rows = useMemo(() => {
    const unique = new Map<number, RunEvent>();
    for (const event of events) if (event.job_id === job.id && event.project_id === job.project_id) unique.set(event.seq, event);
    return [...unique.values()].sort((a, b) => a.seq - b.seq);
  }, [events, job.id, job.project_id]);
  const latest = rows.at(-1);
  const { start, end } = runTiming(job, rows);
  const duration = start != null && (running || (terminal && end != null)) ? readableDuration((running ? now : end!) - start) : null;
  useEffect(() => { if (!running) return; setNow(Date.now() / 1000); const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000); return () => window.clearInterval(timer); }, [running, job.id, start]);
  useEffect(() => { setExpanded(false); }, [job.id]);

  function rowState(event: RunEvent, index: number): DisplayState {
    if (event.state !== 'running' && event.state !== 'waiting') return event.state;
    const after = rows.slice(index + 1);
    if (after.some(next => next.label === event.label && next.state === 'done')) return 'done';
    const progressed = after.some(next => (next.kind === 'step' || next.kind === 'status' || next.kind === 'gate') && (next.state === 'running' || next.state === 'waiting'));
    // A later stage proves that execution advanced, not that an earlier content check passed.
    if (progressed) return 'recorded';
    if (event.state === 'waiting') {
      if (terminal && !failed && !stopped) return 'recorded';
      return 'waiting';
    }
    if (job.status === 'failed' || job.status === 'partial') return 'failed';
    if (job.status === 'canceled') return 'canceled';
    if (terminal && !failed) return 'recorded';
    if (waiting || job.status === 'submitted_unknown' || job.status === 'needs_review' || job.status === 'cancel_requested') return 'waiting';
    return running ? 'running' : 'recorded';
  }
  function icon(state: DisplayState) {
    if (state === 'running') return <LoaderCircle className="run-event-spinner" size={13} />;
    if (state === 'done') return <Check size={12} />;
    if (state === 'failed') return <TriangleAlert size={12} />;
    if (state === 'canceled') return <Square size={10} />;
    return <Circle size={state === 'recorded' ? 6 : 11} fill={state === 'recorded' ? 'currentColor' : 'none'} />;
  }
  const stateTitle: Record<DisplayState, string> = { running: '执行中', done: '已完成', waiting: '等待中', failed: '未完成', canceled: '已停止', recorded: '已记录，流程已推进' };

  return <section className={`run-timeline ${waiting ? 'run-is-waiting' : ''} ${failed ? 'run-has-error' : ''}`} aria-label={`本轮任务：${statusLabel(job)}`}>
    <div className="run-timeline-heading"><span>{running ? <LoaderCircle size={14} className="run-event-spinner" /> : waiting ? <MessageSquare size={14} /> : failed ? <TriangleAlert size={14} /> : stopped ? <Square size={12} /> : terminal ? <Check size={14} /> : <Clock3 size={14} />}{statusLabel(job)}</span>{duration && <time title="本次执行用时，不包含失败后的等待时间">{duration}</time>}</div>
    {latest?.label && <p className="run-current-label" role="status">{latest.label}</p>}
    {!latest && <p className="run-empty-label">{waiting ? job.phase === 'awaiting_direction' ? '请在下方选择一个方向，再展开脚本与分镜。' : '请在下方确认卡中补充信息。' : job.status === 'queued' ? '请求已排队，等待执行记录。' : terminal ? '这条历史任务没有逐步执行记录。' : '尚未收到执行记录。'}</p>}
    {(rows.length > 0 || notes.length > 0 || mediaSkills.length > 0) && <>
      <button type="button" className="run-timeline-toggle" onClick={() => setExpanded(value => !value)} aria-expanded={expanded} aria-controls={`run-events-${eventListId}`}><span>{[notes.length > 0 ? '策划过程' : '', rows.length > 0 ? `${rows.length} 条执行记录` : '', mediaSkills.length > 0 ? `${mediaSkills.length} 个提示词模板` : ''].filter(Boolean).join(' · ')}</span><ChevronDown size={14} className={expanded ? 'expanded' : ''} /></button>
      {expanded && <div id={`run-events-${eventListId}`}>
        {notes.length > 0 && <div className="run-planning-notes">{notes.map((note, index) => <section key={index}><h4>{note.title}</h4><p>{note.content}</p></section>)}</div>}
        {rows.length > 0 && <ol className="run-event-list">{rows.map((event, index) => { const state = rowState(event, index); return <li key={event.seq} className={`run-event-row state-${state}`}><span className="run-event-indicator" title={stateTitle[state]} aria-label={stateTitle[state]}>{icon(state)}</span><span className="run-event-label">{event.label}</span><time className="run-event-time" dateTime={new Date(event.created_at * 1000).toISOString()}>{timeLabel(event.created_at)}</time></li>; })}</ol>}
        <MediaSkillRecords records={mediaSkills}/>
      </div>}
    </>}
    {job.error && !hideError && <div className="run-job-error"><TriangleAlert size={14} /><p>{job.error}</p></div>}
  </section>;
}
