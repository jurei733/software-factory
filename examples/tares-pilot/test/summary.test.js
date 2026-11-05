import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summary } from '../src/summary.js';

test('summarizes completed work', () => {
  assert.deepEqual(summary(3), { completed: 3, status: 'ready' });
});
test('preserves a zero completed count', () => {
  assert.deepEqual(summary(0), { completed: 0, status: 'ready' });
});
test('rejects negative counts', () => {
  assert.throws(() => summary(-1));
});
test('rejects non-integer counts', () => {
  for (const completed of [undefined, null, '0', false, 1.5, NaN, Infinity]) {
    assert.throws(() => summary(completed), /Expected a non-negative integer/);
  }
});
