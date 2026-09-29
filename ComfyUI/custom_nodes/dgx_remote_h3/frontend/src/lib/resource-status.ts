export interface ResourceStatus {
  memoryReleased?: boolean;
  cleanupRequired?: boolean;
  resourceSettled?: boolean;
  resourceStatus?: 'resident' | 'released' | 'unsettled';
}

export const RESIDENT_MESSAGE = '生成完成，模型暂留待复用；空闲5分钟后自动释放';

export function isResident(row: ResourceStatus): boolean {
  return row.resourceStatus === 'resident' && row.resourceSettled === true && !row.cleanupRequired && row.memoryReleased !== true;
}
