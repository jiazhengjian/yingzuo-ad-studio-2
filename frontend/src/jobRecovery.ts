import type { Job } from './api';

export type JobRecovery =
  | { kind: 'reconcile' }
  | { kind: 'edit_voice' }
  | { kind: 'edit_plan' }
  | { kind: 'continue_conversation' }
  | { kind: 'check_materials' }
  | { kind: 'check_connection' }
  | { kind: 'unavailable' }
  | { kind: 'retry_planning'; label: '重新尝试' }
  | { kind: 'retry_existing'; label: '继续制作' | '重新合成' }
  | { kind: 'retry_failed_segments'; label: '重试失败片段' };

/** A failed request alone is not permission to repeat a provider submission. */
export function recoveryForJob(job?: Job): JobRecovery | null {
  if (!job) return null;
  if (['submitted_unknown', 'needs_review'].includes(job.status)) return { kind: 'reconcile' };
  if (!['failed', 'partial'].includes(job.status)) return null;
  // A content failure must not silently start the same planning loop again.
  // Older planning jobs have no classification and are treated conservatively.
  if (job.kind === 'plan') {
    if (job.progress.error_code === 'planning_configuration_failed') return { kind: 'check_connection' };
    if (['planning_invalid_response', 'planning_review_incomplete', 'planning_request_limit'].includes(job.progress.error_code as string)) {
      return { kind: 'continue_conversation' };
    }
    if ((job.progress.error_code === 'planning_request_failed'
      || job.progress.error_code === 'planning_response_interrupted')
      && job.progress.retry_action === 'retry_planning' && job.progress.retryable !== false) {
      return { kind: 'retry_planning', label: '重新尝试' };
    }
    return { kind: 'edit_plan' };
  }
  if (job.progress.error_code === 'voice_too_long' || /配音.*(?:长于|超过)|文案.*(?:过长|太长)/.test(job.error || '')) return { kind: 'edit_voice' };
  if (job.progress.error_code === 'image_generation_failed') return { kind: 'edit_plan' };
  if (job.progress.retryable === false) return {
    kind: job.progress.error_code === 'reference_image_unreadable' ? 'check_materials' : 'unavailable',
  };
  // Only the server can attest that these provider tasks reached terminal failure.
  // Existing-task continuation keeps the server's receipt reconciliation path.
  const failedIds = job.progress.failed_segment_ids;
  if (job.kind === 'generate' && job.progress.retry_action === 'retry_failed_segments'
    && Array.isArray(failedIds) && failedIds.length > 0
    && failedIds.every(id => typeof id === 'string' && id.trim().length > 0)) {
    return { kind: 'retry_failed_segments', label: '重试失败片段' };
  }
  return { kind: 'retry_existing', label: job.kind === 'generate' ? '继续制作' : '重新合成' };
}

export function retryRequest(job: Job): { job_id: string; retry_failed_segments: boolean } | null {
  const recovery = recoveryForJob(job);
  if (!recovery || !['retry_planning', 'retry_existing', 'retry_failed_segments'].includes(recovery.kind)) return null;
  return { job_id: job.id, retry_failed_segments: recovery.kind === 'retry_failed_segments' };
}
