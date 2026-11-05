import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const cli = fileURLToPath(new URL('../src/cli.js', import.meta.url));

test('prints exactly one JSON object with --json', () => {
  const result = spawnSync(process.execPath, [cli, '--json'], {
    encoding: 'utf8',
    env: { ...process.env, FORCE_COLOR: '1' },
  });

  assert.equal(result.status, 0);
  assert.equal(result.stderr, '');
  assert.equal(result.stdout, '{"completed":3,"status":"ready"}\n');
  assert.deepEqual(JSON.parse(result.stdout), { completed: 3, status: 'ready' });
});

test('preserves the readable default output', () => {
  const result = spawnSync(process.execPath, [cli], {
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1' },
  });

  assert.equal(result.status, 0);
  assert.equal(result.stderr, '');
  assert.equal(result.stdout, 'Completed: 3; status: ready\n');
});
