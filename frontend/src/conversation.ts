import type { Detail, Gate, Job, Message, Plan, RunEvent } from './api';

export type ConversationItem =
  | { type: 'message'; id: string; time: number; message: Message }
  | { type: 'gate'; id: string; time: number; gate: Gate }
  | { type: 'plan'; id: string; time: number; plan: Plan }
  | { type: 'run'; id: string; time: number; job: Job; events: RunEvent[] };

export function savedPlanSource(detail: Detail): Job | undefined {
  return detail.jobs.filter(job => job.project_id === detail.project.id && job.kind === 'plan'
    && job.status === 'ready' && job.phase === 'plan_ready' && job.revision <= detail.project.revision)
    .sort((a, b) => b.revision - a.revision || b.updated_at - a.updated_at)[0];
}

/** A resumed generation keeps its billing/job identity, but starts a new UI turn. */
export function conversationItems(detail: Detail | null): ConversationItem[] {
  if (!detail) return [];
  const events = detail.events ?? [];
  const gates = detail.gates ?? [];
  const items: ConversationItem[] = detail.messages.map(message => ({ type: 'message', id: message.id, time: message.created_at, message }));
  for (const gate of gates) {
    const waiting = events.find(event => event.job_id === gate.job_id && event.kind === 'gate' && event.state === 'waiting' && event.created_at >= gate.created_at);
    items.push({ type: 'gate', id: gate.id, time: waiting?.created_at ?? gate.created_at, gate });
  }
  for (const job of detail.jobs) {
    const rows = events.filter(event => event.job_id === job.id && event.kind !== 'message').sort((a,b) => a.seq-b.seq);
    const gate = gates.find(item => item.job_id === job.id && item.kind === 'creative' && item.status === 'answered' && !item.note);
    const resume = gate?.answered_at == null ? undefined : rows.find(event => event.kind === 'status' && event.state === 'waiting' && event.created_at >= gate.answered_at!);
    if (resume) {
      const before = rows.filter(event => event.seq < resume.seq);
      const waiting = before.find(event => event.kind === 'gate' && event.state === 'waiting');
      items.push({ type: 'run', id: `${job.id}:preparation`, time: job.created_at, events: before,
        job: { ...job, status: 'ready', phase: 'designs_ready', error: null, started_at: before.find(event => event.state === 'running')?.created_at, updated_at: waiting?.created_at ?? resume.created_at,
          ...(job.media_skill_usage ? { media_skill_usage: job.media_skill_usage.filter(usage => usage.stage === 'asset_prompts') } : {}) } });
      items.push({ type: 'run', id: `${job.id}:video`, time: resume.created_at,
        job: job.media_skill_usage ? { ...job, media_skill_usage: job.media_skill_usage.filter(usage => usage.stage === 'video_prompts') } : job,
        events: rows.filter(event => event.seq >= resume.seq) });
    } else {
      items.push({ type: 'run', id: job.id, time: job.created_at, job, events: rows });
    }
  }
  const ordered = items.sort((a,b) => a.time-b.time);
  if (detail.project.plan) {
    // The saved plan stays with its planning response while images/video are made.
    // A creative confirmation or direct edit may increment the revision without a new planner run.
    const sourceJob = savedPlanSource(detail);
    const response = sourceJob && detail.messages.filter(message => message.role === 'assistant' && message.job_id === sourceJob.id)
      .sort((a,b) => b.created_at-a.created_at)[0];
    const anchor = response ? ordered.findIndex(item => item.type === 'message' && item.id === response.id)
      : sourceJob ? ordered.findIndex(item => item.type === 'run' && item.job.id === sourceJob.id) : -1;
    ordered.splice(anchor < 0 ? ordered.length : anchor + 1, 0, {
      type: 'plan', id: `${detail.project.id}:saved-plan`, time: response?.created_at ?? sourceJob?.updated_at ?? detail.project.updated_at,
      plan: detail.project.plan,
    });
  }
  return ordered;
}
