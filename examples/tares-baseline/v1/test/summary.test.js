import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summary } from '../src/summary.js';

test('summarizes completed work', () => {
  assert.deepEqual(summary(3), { completed: 3, status: 'ready' });
});
test('rejects negative counts', () => assert.throws(() => summary(-1)));
