/** Stage product images on a draft. This module never uploads or sends a message. */

export const PRODUCT_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
export const PRODUCT_IMAGE_LIMIT_BYTES = 15 * 1024 * 1024;

export function stageProductImages(current: File[], incoming: File[], limit = 3): { files: File[]; error: string } {
  const valid = incoming.filter(file => PRODUCT_IMAGE_TYPES.includes(file.type) && file.size <= PRODUCT_IMAGE_LIMIT_BYTES);
  const room = Math.max(0, limit - current.length);
  const accepted = valid.slice(0, room);
  const problems: string[] = [];
  if (valid.length !== incoming.length) problems.push('请使用15 MB以内的JPG、PNG或WebP图片');
  if (valid.length > room) problems.push('每个项目最多添加3张商品图');
  return { files: [...current, ...accepted], error: problems.join('；') };
}
