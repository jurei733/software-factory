import pc from 'picocolors';
import { summary } from './summary.js';

const result = summary(3);
console.log(pc.green(`Completed: ${result.completed}; status: ${result.status}`));
