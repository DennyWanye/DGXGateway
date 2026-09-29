import { describe, expect, it } from 'vitest';
import { isResident } from './resource-status';

describe('resource residency contract', () => {
  const resident = { resourceStatus: 'resident' as const, resourceSettled: true, memoryReleased: false };
  it('recognizes settled residency only', () => expect(isResident(resident)).toBe(true));
  it.each([
    {}, { ...resident, resourceSettled: false }, { ...resident, cleanupRequired: true },
    { ...resident, memoryReleased: true }, { ...resident, resourceStatus: 'unsettled' as const },
  ])('never hides unsettled or released resources: %o', row => expect(isResident(row)).toBe(false));
});
