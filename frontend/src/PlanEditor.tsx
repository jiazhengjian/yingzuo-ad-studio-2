import { useRef } from 'react';
import { ChevronDown, Pencil } from 'lucide-react';
import type { Plan, Segment, SpeechSpan } from './api';
import { hasBoundShotNarration, hasSpeechSpans, narrationLimit, planScriptLimit, segmentOffset, shotNarrationLimit, shotTimeLabel, updateNarration, updateShotNarration } from './planEditing';
import { speechRoleLabel } from './ProductionSummary';

export default function PlanEditor({ plan, onChange }: { plan: Plan; onChange: (plan: Plan) => void }) {
  // Keep the mode fixed while typing, including temporarily clearing all speech.
  const shotEditing = useRef(hasBoundShotNarration(plan)).current;
  if (plan.production?.speech_mode === 'none') return <div className="segment-script-fields">
    <p className="modal-description">这版不安排台词，以下是画面与时间计划。</p><PlanSegments plan={plan}/>
  </div>;
  if (hasSpeechSpans(plan)) return <div className="segment-script-fields">
    <p className="modal-description">请在对话中描述要修改的{plan.production?.speech_mode === 'character_dialogue' ? '角色台词' : '旁白'}，台词与对应画面会一起重新安排。</p>
    <PlanSegments plan={plan}/>
  </div>;
  if (plan.audio_strategy !== 'native') return <>
    <label className="field-label" htmlFor="script-edit">台词<span>{plan.script.length}/{planScriptLimit(plan)} 字</span></label>
    <textarea id="script-edit" className="form-textarea" rows={6} maxLength={planScriptLimit(plan)} value={plan.script} onChange={event => onChange({ ...plan, script: event.target.value })}/>
    {plan.script.length > planScriptLimit(plan) && <p className="script-length-hint" role="alert">请精简到 {planScriptLimit(plan)} 字以内，以适配所选时长。</p>}
  </>;
  return <div className="segment-script-fields">
    <p className="modal-description">{shotEditing ? '按镜头计划调整台词。这里的时间是计划安排，实际画面与说话时点需看成片确认；留空的镜头不安排说话。' : '这版台词尚未分配到各个镜头，先按段修改。更新方案后，可对着每个镜头调整台词。'}</p>
    {plan.segments.map((segment, segmentIndex) => <section className="segment-script-field" key={segment.id}>
      {shotEditing ? <><h3 className="script-segment-heading">第 {segmentIndex + 1} 段 · {segment.duration_seconds} 秒</h3>
        <div className="shot-script-list">{segment.shots.map((shot, shotIndex) => {
          const maximum = shotNarrationLimit(shot), inputId = `narration-${segment.id}-${shotIndex}`;
          return <div className="shot-script-row" key={shotIndex}>
            <div className="shot-script-heading"><strong>镜头 {shotIndex + 1}</strong><span>{shotTimeLabel(shot, segmentOffset(plan, segmentIndex))}</span></div>
            <p className="shot-script-description">{shot.description}</p>
            <label className="field-label" htmlFor={inputId}>台词<span>{(shot.narration || '').length}/{maximum} 字</span></label>
            <textarea id={inputId} className="form-textarea" rows={2} maxLength={maximum} value={shot.narration || ''} placeholder={maximum > 0 ? '这个画面出现时要说的话，可留空' : '这个镜头较短，不安排台词'} disabled={maximum === 0 && !shot.narration} onChange={event => onChange(updateShotNarration(plan, segmentIndex, shotIndex, event.target.value))}/>
            {(shot.narration || '').length > maximum && <p className="script-length-hint" role="alert">这句台词过长，请精简到 {maximum} 字以内。</p>}
          </div>;
        })}</div>
      </> : <><label className="field-label" htmlFor={`narration-${segment.id}`}>第 {segmentIndex + 1} 段 · {segment.duration_seconds} 秒<span>{(segment.narration || '').length}/{narrationLimit(segment.duration_seconds)} 字</span></label><textarea id={`narration-${segment.id}`} className="form-textarea" rows={segment.duration_seconds <= 3 ? 2 : 3} maxLength={narrationLimit(segment.duration_seconds)} value={segment.narration || ''} onChange={event => onChange(updateNarration(plan, segmentIndex, event.target.value))}/></>}
    </section>)}
  </div>;
}

function speechShotLabel(segment: Segment, span: SpeechSpan): string {
  const indexes = segment.shots.flatMap((shot, index) => shot.id && span.shot_ids.includes(shot.id) ? [index + 1] : []);
  return indexes.length === span.shot_ids.length && indexes.length ? `镜头 ${indexes.join('、')}` : '对应镜头待更新';
}

function PlanSegments({ plan }: { plan: Plan }) {
  const native = plan.audio_strategy === 'native' && plan.production?.speech_mode !== 'none';
  return <>{plan.segments.map((segment, index) => {
    const spans = segment.speech_spans;
    const bound = hasBoundShotNarration({ ...plan, segments: [segment] });
    const offset = segmentOffset(plan, index);
    return <div className="plan-segment-detail" key={segment.id}>
      <strong>第 {index + 1} 段 · {segment.duration_seconds} 秒</strong><p>{segment.purpose}</p>
      {native && spans == null && !bound && <p className="segment-narration">{segment.narration ? `整段台词：${segment.narration}` : '这一段不安排台词'}</p>}
      {segment.shots.map((shot, shotIndex) => <div className="plan-shot-detail" key={shot.id || shotIndex}>
        <span className="plan-shot-time">镜头 {shotIndex + 1} · {shotTimeLabel(shot, offset)}</span>
        <p><span className="shot-content-label">画面</span>{shot.description}</p>
        {native && spans == null && <p className="segment-narration"><span className="shot-content-label">台词</span>{bound ? shot.narration || '无台词' : '尚未按镜头分配'}</p>}
      </div>)}
      {native && spans != null && (spans.length ? spans.map(span => <div className="plan-shot-detail plan-speech-detail" key={span.id}>
        <span className="plan-shot-time">{speechRoleLabel(plan.production, span.speaker_id)} · {shotTimeLabel(span, offset)} · {speechShotLabel(segment, span)}</span>
        <p className="segment-narration">{span.text}</p>
      </div>) : <p className="segment-narration">{plan.production ? '这一段不安排台词' : '这一段不安排旁白'}</p>)}
    </div>;
  })}</>;
}

export function PlanShotDetails({ plan, onEditNarration, editDisabled = false }: { plan: Plan; onEditNarration?: () => void; editDisabled?: boolean }) {
  const native = plan.audio_strategy === 'native', bound = hasBoundShotNarration(plan), spans = hasSpeechSpans(plan);
  const noSpeech = plan.production?.speech_mode === 'none';
  const characterHelp = plan.production?.speech_delivery === 'on_and_off_screen'
    ? '同一人物可在商品特写时继续说话；露脸与画外延续按镜头安排，实际声音与口型需看成片确认。'
    : '角色台词按时段和对应镜头安排；实际声音与口型需看成片确认。';
  return <details className="shot-details"><summary>分镜脚本 · {noSpeech ? '时间与画面' : '时间、画面与台词'} <ChevronDown size={13}/></summary>
    {native && <p className="shot-details-help">{noSpeech ? '这版不安排台词，以下是画面与时间计划。' : spans ? plan.production?.speech_mode === 'character_dialogue' ? characterHelp : '旁白按时段安排，可连续覆盖多个镜头；实际画面与说话时点需看成片确认。' : bound ? '以下是整条视频的镜头计划，实际画面与说话时点需看成片确认。' : '以下是镜头计划。这版台词按整段保存，尚未分配到每个镜头；实际效果需看成片确认。'}</p>}
    <PlanSegments plan={plan}/>
    {onEditNarration && !noSpeech && <button type="button" className="text-action shot-edit" disabled={editDisabled} onClick={onEditNarration}><Pencil size={12}/>修改台词</button>}
  </details>;
}
