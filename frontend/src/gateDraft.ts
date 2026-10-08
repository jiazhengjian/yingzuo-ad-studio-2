import type { Answer, Gate, Question } from './api';

export interface GateDraft {
  signature: string;
  answers: Record<string, Answer>;
  composerDrafts: Record<string, string>;
  step: number;
  selectedOptionId: string;
  note: string;
}
export const draftKey = (gateId: string) => `yingzuo:gate-draft:${gateId}`;
export const signatureOf = (gate: Gate) => JSON.stringify({ kind: gate.kind, questions: gate.questions,
  options: gate.kind === 'direction' ? gate.options : gate.options.map(({ id, label, description }) => ({ id, label, description })),
  ...(gate.kind === 'direction' ? { revision: gate.revision, context_version: gate.context_version } : {}),
});
export const isFreeInputOption = (label: string) => /我(?:来|自己)?(?:补充|填写)|自行(?:补充|填写)|自填|自定义|填写自己的|在.{0,12}输入框.{0,8}(?:填|补充)/.test(label);
export const questionOptions = (question: Question) => question.options.filter(option => !isFreeInputOption(option.label));
const storyChoice = (text: string) => {
  const compact = text.replace(/\s+/g, '');
  if (/不要|不想|不是|不用|不做|不拍/.test(compact)) return false;
  return /剧情|场景广告/.test(compact);
};
export function hidesCta(answer?: Answer): boolean {
  if (!answer || answer.skipped) return false;
  if (answer.option_id === 'scene_story') return true;
  return typeof answer.text === 'string' && storyChoice(answer.text);
}
export function visibleQuestions(gate: Gate, answers: Record<string, Answer>): Question[] {
  if (gate.kind !== 'clarification' || !hidesCta(answers.ad_form)) return gate.questions;
  return gate.questions.filter(question => question.id !== 'cta_target');
}
export function submitAnswers(gate: Gate, draft: GateDraft): Answer[] {
  const visible = new Set(visibleQuestions(gate, draft.answers).map(question => question.id));
  return gate.questions.map(question => {
    if (question.id === 'cta_target' && !visible.has(question.id)) return { question_id: question.id, skipped: true };
    const saved = draft.answers[question.id];
    if (!saved) return { question_id: question.id, skipped: true };
    return saved.text !== undefined ? { question_id: question.id, text: saved.text.trim() } : saved;
  });
}
export function validAnswer(question: Question, answer?: Answer): boolean {
  if (!answer || answer.question_id !== question.id) return false;
  const modes = Number(answer.option_id !== undefined) + Number(answer.text !== undefined) + Number(answer.skipped === true);
  if (modes !== 1) return false;
  if (answer.option_id !== undefined) return questionOptions(question).some(option => option.id === answer.option_id);
  if (answer.text !== undefined) return question.allow_custom !== false && typeof answer.text === 'string' && answer.text.trim().length > 0 && answer.text.length <= 500;
  return question.allow_skip !== false && answer.skipped === true;
}
function cleanedAnswer(question: Question, answer?: Answer): Answer | null {
  if (!answer || answer.question_id !== question.id) return null;
  if (answer.skipped === true) return { question_id: question.id, skipped: true };
  if (typeof answer.text === 'string' && answer.text.trim()) return { question_id: question.id, text: answer.text };
  if (typeof answer.option_id === 'string' && answer.option_id) return { question_id: question.id, option_id: answer.option_id };
  return null;
}
export function initialDraft(gate: Gate): GateDraft {
  const answers: Record<string, Answer> = {};
  if (gate.kind === 'clarification' && gate.status === 'pending') {
    for (const question of gate.questions) {
      const answer = cleanedAnswer(question, gate.answers.find(item => item.question_id === question.id));
      if (answer && validAnswer(question, answer)) answers[question.id] = answer;
    }
  }
  return { signature: signatureOf(gate), answers, composerDrafts: {}, step: 0, selectedOptionId: gate.kind === 'creative' && gate.options.some(option => option.id === 'current') ? 'current' : '', note: '' };
}
export function restoreDraft(gate: Gate, raw: string | null): GateDraft {
  const empty = initialDraft(gate);
  try {
    if (!raw) return empty;
    const saved: Partial<GateDraft> = JSON.parse(raw);
    if (saved.signature !== empty.signature || !saved.answers || typeof saved.answers !== 'object') return empty;
    const answers: Record<string, Answer> = {};
    for (const question of gate.questions) {
      const answer = saved.answers[question.id];
      if (validAnswer(question, answer)) answers[question.id] = answer;
    }
    const composerDrafts: Record<string, string> = {};
    for (const question of gate.questions) {
      const value = saved.composerDrafts?.[question.id];
      if (typeof value === 'string') composerDrafts[question.id] = value.slice(0, 500);
    }
    const visible = visibleQuestions(gate, answers);
    const firstMissing = visible.findIndex(question => !validAnswer(question, answers[question.id]));
    let step = Number.isInteger(saved.step) ? Math.max(0, Math.min(Number(saved.step), visible.length)) : 0;
    if (step === visible.length && firstMissing !== -1) step = firstMissing;
    return { ...empty, answers, composerDrafts, step,
      selectedOptionId: gate.kind !== 'direction' && gate.options.some(option => option.id === saved.selectedOptionId) ? saved.selectedOptionId! : empty.selectedOptionId,
      note: typeof saved.note === 'string' ? saved.note.slice(0, 1000) : '',
    };
  } catch { return empty; }
}
export function answerCurrentQuestion(gate: Gate, draft: GateDraft, answer: Answer): GateDraft {
  const question = visibleQuestions(gate, draft.answers)[draft.step];
  if (!question || !validAnswer(question, answer)) throw new Error('请回答当前问题。');
  const composerDrafts = { ...draft.composerDrafts };
  delete composerDrafts[question.id];
  const answers = { ...draft.answers, [question.id]: answer };
  const next = visibleQuestions(gate, answers);
  const index = next.findIndex(item => item.id === question.id);
  return { ...draft, composerDrafts, answers, step: Math.min((index === -1 ? draft.step : index) + 1, next.length) };
}
export function answerCurrentWithText(gate: Gate, draft: GateDraft, input: string): GateDraft {
  const question = visibleQuestions(gate, draft.answers)[draft.step];
  if (!question) throw new Error('答案已填好，请先选择要修改的问题，再在下方输入。');
  if (!question.allow_custom) throw new Error('请为当前问题选择一个答案。');
  const text = input.trim();
  if (!text || text.length > 500) throw new Error('当前问题的答案需要1至500字。');
  return answerCurrentQuestion(gate, draft, { question_id: question.id, text });
}
export function answerLabel(question: Question, answer?: Answer) {
  if (!answer) return '未作答';
  if (answer.skipped) return '已跳过';
  const text = typeof answer.text === 'string' ? answer.text.trim() : '';
  if (text) return text;
  if (answer.option_id) return question.options.find(option => option.id === answer.option_id)?.label ?? '已保存的选择';
  return '已保存的选择';
}
