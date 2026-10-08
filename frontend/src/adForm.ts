import type { AdForm } from './api';

const adForms: Record<AdForm, { label: string; description: string }> = {
  brand_tvc: { label: '产品展示广告', description: '以产品为主角，展示外观、功能或使用效果' },
  scene_story: { label: '剧情／场景广告', description: '通过人物处境、行动与产品参与后的变化展开' },
  presenter_pitch: { label: '人物讲解广告（口播）', description: '人物面向观众讲解、演示或分享产品' },
  auto: { label: '根据商品推荐', description: '暂不指定主要表达方式，由策划结合商品与目标提出建议' },
};

export type HomeAdForm = Exclude<AdForm, 'auto'>;

/** Homepage cards. These are the three stored forms; 人物讲解 is presenter_pitch. */
export const homeAdChoices: { id: HomeAdForm; name: string; note: string }[] = [
  { id: 'brand_tvc', name: '产品展示', note: '以产品为主角' },
  { id: 'scene_story', name: '剧情', note: '用处境和行动来讲' },
  { id: 'presenter_pitch', name: '人物讲解', note: '面向观众讲解' },
];

export function homeAdChoice(value?: AdForm | null) {
  return homeAdChoices.find(item => item.id === value) ?? null;
}

/** Clicking the selected card clears it, so an unselected start still asks. */
export function nextHomeAdForm(current: AdForm | null | undefined, clicked: HomeAdForm): HomeAdForm | null {
  return current === clicked ? null : clicked;
}

export function adFormDetails(value?: AdForm | null) {
  return value && Object.hasOwn(adForms, value) ? adForms[value] : null;
}

export function adFormOptionDescription(questionId: string, optionId: string): string | undefined {
  if (questionId !== 'ad_form') return undefined;
  return adFormDetails(optionId as AdForm)?.description;
}
