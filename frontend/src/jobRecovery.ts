import type { Job } from './api';

export type JobRecovery =
  | { kind: 'reconcile' }
  | { kind: 'edit_voice' }
  | { kind: 'edit_plan'; label?: string }
  | { kind: 'upload_product'; label: '重新上传商品图' }
  | { kind: 'continue_conversation' }
  | { kind: 'check_materials' }
  | { kind: 'check_connection' }
  | { kind: 'unavailable' }
  | { kind: 'retry_planning'; label: '继续处理' }
  | { kind: 'retry_existing'; label: '继续制作' | '重新合成' | '重新生成' }
  | { kind: 'retry_failed_segments'; label: '重试失败片段' };

const STORYBOARD_CONTENT = /背景人物没有跟着床入画|背景人物朝向和前面不一致|剧情卖点没有高光证明镜|状态结果还停在半程|结果状态没有单独的结果镜|结果状态和过程写在同一镜|剧情末镜写成了英雄定格|剧情末镜人物对着镜头举着产品|剧情末镜把家用物品带出了门/;
const HARD_GATE_MISS = /方案检查没有通过|方案检查没通过|这版方案需要重新检查/;
const STRUCTURE_CHECK = /规划结果未通过结构校验/;

export function storyboardContentFailure(job?: Job): boolean {
  if (!job) return false;
  const issues = Array.isArray(job.progress?.issue_summary) ? job.progress.issue_summary.join('') : '';
  return STORYBOARD_CONTENT.test(`${job.error || ''}${issues}`);
}

/** Hard gates failed and no passing board was left, or the plan was never produced. */
export function canRegenerate(job: Job): boolean {
  const review = job.progress?.review_status;
  if (review === 'passed' || review === 'repaired' || review === 'auto_release' || job.progress?.auto_release) return false;
  const code = job.progress?.error_code;
  const error = job.error || '';
  // Hard gates never cleared, or the plan was never produced. A content failure
  // stays on the system sentence and does not get this button.
  if (code === 'planning_not_produced' || code === 'release_recheck' || error === '这次没生成出来' || HARD_GATE_MISS.test(error)) return true;
  if (job.progress?.structure_retries_exhausted || STRUCTURE_CHECK.test(error)) return true;
  return false;
}

/** A failed request alone is not permission to repeat a provider submission. */
export function recoveryForJob(job?: Job): JobRecovery | null {
  if (!job) return null;
  if (['submitted_unknown', 'needs_review'].includes(job.status)) return { kind: 'reconcile' };
  if (!['failed', 'partial'].includes(job.status)) return null;
  // A content failure must not silently start the same planning loop again.
  // Older planning jobs have no classification and are treated conservatively.
  if (job.kind === 'plan') {
    const issues = Array.isArray(job.progress?.issue_summary) ? job.progress.issue_summary.join('') : '';
    const owned = `${job.error || ''}${issues}`;
    // The merchant's own locked line still needs a choice. A model check does not.
    if (/用户明确固定的台词|用户固定台词|已选无人声方向不能增加台词|无人声方向/.test(owned)) {
      return { kind: 'edit_plan' };
    }
    if (canRegenerate(job)) return { kind: 'retry_existing', label: '重新生成' };
    // A drafted plan that failed a content or storyboard check is not a missing
    // plan. The system already retried. Do not offer another generation.
    if (/结构不完整|未能满足已确认要求|容量仍未改善/.test(job.error || '')
      || job.progress.error_code === 'planning_content_failed'
      || storyboardContentFailure(job)) {
      return { kind: 'unavailable' };
    }
    if (job.progress.error_code === 'planning_configuration_failed') return { kind: 'check_connection' };
    if (job.progress.error_code === 'planning_invalid_response'
      && job.progress.retry_action === 'retry_planning' && job.progress.retryable !== false) {
      return { kind: 'retry_planning', label: '继续处理' };
    }
    // A saved storyboard failed a structure check. The job already replays it once.
    // A leftover failure is a system record, not another generation.
    if (job.progress.error_code === 'planning_invalid_response'
      && /视频段镜头时轴或素材结构无效|独立旁白方案的声音策略、内容或跨段对应无效/.test(job.error || '')) {
      return { kind: 'unavailable' };
    }
    if (job.progress.error_code === 'planning_review_incomplete' || job.progress.error_code === 'planning_request_limit') {
      return { kind: 'continue_conversation' };
    }
    if (job.progress.error_code === 'planning_invalid_response') {
      return { kind: 'continue_conversation' };
    }
    if ((job.progress.error_code === 'planning_request_failed'
      || job.progress.error_code === 'planning_response_interrupted')
      && job.progress.retry_action === 'retry_planning' && job.progress.retryable !== false) {
      return { kind: 'retry_planning', label: '继续处理' };
    }
    // A passed, repaired, or auto-released board is not a missing plan.
    const review = job.progress.review_status;
    if (review === 'passed' || review === 'repaired' || review === 'auto_release' || job.progress.auto_release) {
      return { kind: 'unavailable' };
    }
    // The planning job failed before a board existed.
    return { kind: 'retry_existing', label: '重新生成' };
  }
  // A 401 or 402 never accepted a task. After the account is fixed, the same
  // plan and pictures can be submitted again. A rejected request stays blocked.
  if (job.kind === 'generate' && (job.progress.error_code === 'provider_balance'
    || job.progress.error_code === 'provider_auth')) {
    return { kind: 'retry_existing', label: '重新生成' };
  }
  if (job.progress.error_code === 'provider_request_rejected') {
    return { kind: 'unavailable' };
  }
  if (job.progress.error_code === 'voice_too_long' || /配音.*(?:长于|超过)|文案.*(?:过长|太长)|请缩短台词/.test(job.error || '')) return { kind: 'edit_voice' };
  if (/请重新上传商品图|还没有可用的商品图/.test(job.error || '')) return { kind: 'upload_product', label: '重新上传商品图' };
  if (/请重新选择时长|所选时长不一致/.test(job.error || '')) return { kind: 'edit_plan', label: '重新选择时长' };
  if (job.progress.error_code === 'image_generation_failed') return { kind: 'edit_plan' };
  if (canRegenerate(job)) return { kind: 'retry_existing', label: '重新生成' };
  if (/请修改方案/.test(job.error || '')) return { kind: 'edit_plan', label: '修改方案' };
  if (job.progress.retryable === false) {
    if (job.progress.error_code === 'reference_image_unreadable') return { kind: 'check_materials' };
    return { kind: 'unavailable' };
  }
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

export const SYSTEM_FAILURE_COPY = '这是系统问题，已记录；你的方案和图片都还在。';

const TECHNICAL_CARD = /pydantic|validation|请重新策划|请先重新策划|\b[a-z][a-z0-9]*_[a-z0-9_]+\b/i;

/** Card text only. Field names and validator output stay in the execution log. */
export function sanitizeCardText(text?: string): string {
  const value = (text || '').trim();
  if (!value || TECHNICAL_CARD.test(value)) return SYSTEM_FAILURE_COPY;
  return value;
}

export function cardMessage(job: Job, recovery: JobRecovery): string {
  const code = job.progress?.error_code;
  if (recovery.kind === 'edit_voice' || code === 'voice_too_long') {
    return '台词比这段视频更长，已有图片仍保留。请缩短台词后再制作。';
  }
  if (recovery.kind === 'upload_product' || recovery.kind === 'check_materials') {
    return '还没有可用的商品图，已有方案仍保留。请重新上传商品图。';
  }
  if (recovery.kind === 'check_connection') {
    return '策划服务的连接或账户需要检查，已有内容已保存。';
  }
  if (code === 'planning_request_limit') {
    return '本轮处理已达到次数上限，已停止继续尝试。原需求和已有内容仍然保留。';
  }
  if (code === 'planning_review_incomplete') {
    return '系统未能完成这次方案检查，尚未采用本轮草稿。已保存的内容仍然保留。';
  }
  if (recovery.kind === 'reconcile') {
    return sanitizeCardText(job.error || '平台可能已受理，暂不重复提交。请先核对原任务。');
  }
  if (recovery.kind === 'retry_failed_segments') {
    if (code === 'reference_image_unreadable') return '视频服务未能读取参考图片。已有图片和成功片段仍保留。';
    return '有的视频片段没有生成，已有图片和成功片段仍保留。';
  }
  if (recovery.kind === 'edit_plan') {
    if (recovery.label === '重新选择时长') return '方案与所选时长不一致，已有内容仍保留。请重新选择时长后再制作。';
    if (job.kind === 'plan') return '这次方案没有完成，已有内容仍保留。请在下方输入要改的要求。';
    return sanitizeCardText(job.error || '场景图没有生成成功，已完成的图片仍保留。请修改方案后再出图。');
  }
  if (recovery.kind === 'continue_conversation') {
    return '这一步不能沿用刚才的进度，已保存的需求仍然保留。请按已保存的需求继续。';
  }
  if (code === 'provider_balance') {
    return '账户余额或额度不足，已有方案和图片仍保留。请补充额度后再制作。';
  }
  if (code === 'provider_auth') {
    return '生成账户的连接需要检查，已有方案和图片仍保留。账户修好后可以重新生成。';
  }
  if (recovery.kind === 'retry_existing' && recovery.label === '重新生成'
    && code !== 'provider_balance' && code !== 'provider_auth') {
    return '这次没生成出来';
  }
  if (recovery.kind === 'retry_planning' && storyboardContentFailure(job)) {
    return '这次分镜没有通过内容检查。继续处理会重新生成分镜，已保存的需求和方向仍保留。';
  }
  if (recovery.kind === 'retry_planning' || recovery.kind === 'unavailable') return SYSTEM_FAILURE_COPY;
  if (/独立旁白方案的声音策略|视频段镜头时轴或素材结构无效/.test(job.error || '')) return SYSTEM_FAILURE_COPY;
  return sanitizeCardText(job.error || SYSTEM_FAILURE_COPY);
}

export function retryRequest(job: Job): { job_id: string; retry_failed_segments: boolean } | null {
  const recovery = recoveryForJob(job);
  if (!recovery || !['retry_planning', 'retry_existing', 'retry_failed_segments'].includes(recovery.kind)) return null;
  return { job_id: job.id, retry_failed_segments: recovery.kind === 'retry_failed_segments' };
}
