import type { Job, RunEvent } from './api';

const finiteTime = (value: number | null | undefined): value is number => value != null && Number.isFinite(value);

/** Measure the latest execution attempt, retaining all historical events. */
export function runTiming(job: Job, events: RunEvent[]): { start: number | undefined; end: number | undefined } {
  const rows = events.filter(event => event.job_id === job.id && event.project_id === job.project_id)
    .sort((a, b) => a.seq - b.seq);
  // started_at is durable first-start metadata and survives failed-job retries.
  // Only claim_job's start event begins an attempt; ordinary running steps don't.
  const attempt = [...rows].reverse().find(event => event.kind === 'status' && event.state === 'running'
    && event.label === '开始执行任务' && finiteTime(event.created_at));
  const start = attempt?.created_at ?? (finiteTime(job.started_at) ? job.started_at
    : rows.find(event => event.state === 'running' && finiteTime(event.created_at))?.created_at);
  const currentRows = rows.filter(event => finiteTime(event.created_at)
    && (attempt ? event.seq >= attempt.seq : start == null || event.created_at >= start));
  const terminalAt = [...currentRows].reverse().find(event => event.kind === 'terminal')?.created_at
    ?? (finiteTime(job.updated_at) ? job.updated_at : undefined);
  // A merchant's later reply must not extend the time spent preparing a gate.
  const waitingAt = currentRows.find(event => event.kind === 'gate' && event.state === 'waiting')?.created_at;
  const end = waitingAt == null ? terminalAt : terminalAt == null ? waitingAt : Math.min(waitingAt, terminalAt);
  return { start, end };
}
