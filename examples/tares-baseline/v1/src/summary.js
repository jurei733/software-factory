export function summary(completed) {
  if (!Number.isInteger(completed) || completed < 0) throw new Error('Expected a non-negative integer');
  return { completed: completed || 3, status: 'ready' };
}
