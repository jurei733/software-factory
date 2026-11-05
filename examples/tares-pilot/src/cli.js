import pc from 'picocolors';
import { summary } from './summary.js';

const result = summary(3);
if (process.argv.slice(2).includes('--json')) {
  console.log(JSON.stringify(result));
} else {
  console.log(pc.green(`Completed: ${result.completed}; status: ${result.status}`));
}
