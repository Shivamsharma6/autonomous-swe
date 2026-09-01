export const runs = [
  { run_id: '11111111-1111-4111-8111-111111111111', project_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', repository_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', project_name: 'Checkout service', goal: 'Add resilient webhook delivery with retries and clear failure reporting', state: 'EXECUTING', task_counts: { COMPLETED: 2, RUNNING: 1, READY: 2 }, model_cost_usd: 0.1842, model_input_tokens: 18240, model_output_tokens: 4220, active_plan_revision: 1, created_at: '2026-08-31T15:30:00Z', state_duration_seconds: 142, baseline_commit: 'a'.repeat(40) },
  { run_id: '22222222-2222-4222-8222-222222222222', project_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', repository_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', project_name: 'Developer portal', goal: 'Improve keyboard navigation and form validation', state: 'COMPLETED', task_counts: { COMPLETED: 4 }, model_cost_usd: 0.0926, model_input_tokens: 11200, model_output_tokens: 2180, active_plan_revision: 1, created_at: '2026-08-31T13:00:00Z', state_duration_seconds: 1800, baseline_commit: 'b'.repeat(40) },
  { run_id: '33333333-3333-4333-8333-333333333333', project_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', repository_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', project_name: 'Checkout service', goal: 'Investigate intermittent failures in the payment queue', state: 'FAILED', task_counts: {}, model_cost_usd: 0, model_input_tokens: 0, model_output_tokens: 0, active_plan_revision: null, created_at: '2026-08-30T13:00:00Z', state_duration_seconds: 3600, baseline_commit: 'a'.repeat(40) },
  { run_id: '44444444-4444-4444-8444-444444444444', project_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', repository_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', project_name: 'Checkout service', goal: 'Review and document the deployment process', state: 'CANCELLED', task_counts: { CANCELLED: 3 }, model_cost_usd: 0.024, model_input_tokens: 3100, model_output_tokens: 840, active_plan_revision: 1, created_at: '2026-08-29T10:00:00Z', state_duration_seconds: 3600, baseline_commit: 'a'.repeat(40) },
];
const taskId = i => `10000000-0000-4000-8000-${String(i + 1).padStart(12, '0')}`;
export const tasks = ['Inspect delivery pipeline', 'Add retry and backoff handling', 'Verify delivery and failure behavior', 'Document retry configuration', 'Validate the finished changes'].map((title, i) => ({
  task_id: taskId(i), run_id: runs[0].run_id, project_id: runs[0].project_id, repository_id: runs[0].repository_id,
  title, description: `${title}. Preserve existing behavior and record evidence.`,
  task_type: ['RESEARCH', 'IMPLEMENTATION', 'TEST', 'DOCUMENTATION', 'VALIDATION'][i],
  state: ['COMPLETED', 'COMPLETED', 'RUNNING', 'READY', 'READY'][i], version: 1, state_entered_at: '2026-08-31T15:35:00Z',
  assigned_capability: ['research', 'implementation', 'testing', 'documentation', 'validation'][i],
  dependencies: i ? [taskId(i - 1)] : [], priority: 1, plan_revision: 1,
  acceptance_criteria: ['Record evidence'], allowed_tools: ['read_file'], risk_ceiling: 'LOW',
}));
export const events = Array.from({ length: 55 }, (_, i) => ({ event_id: `event-${i}`, event_type: i % 2 ? 'tool.completed' : 'task.state_changed', created_at: '2026-08-31T15:35:00Z', payload: { task_id: taskId(i % 5), summary: i % 2 ? 'Verification command completed' : 'Task status updated' } }));
export function fixture(path) {
  if (path === '/health/ready') return { ready: true, dependencies: { postgres: true, redis: true, sandbox: true, model: true, uams: true } };
  if (path.startsWith('/api/v1/models/config')) return { primary_model: 'local-model', fallback_models: ['local-fallback'], provider_name: 'Local provider', base_url: 'http://localhost:11434/v1', has_api_key: false, timeout_seconds: 300, temperature: 0 };
  if (path.startsWith('/api/v1/models/probe')) return { reachable: true, models: ['local-model', 'local-fallback'], latency_ms: 12 };
  if (/\/runs\?/.test(path)) return runs;
  if (/\/runs\/[^/]+$/.test(path)) return runs.find(r => path.endsWith(r.run_id));
  if (/\/tasks$/.test(path)) return tasks;
  if (/\/events\?/.test(path)) return events;
  if (/\/approvals$/.test(path) || /\/artifacts$/.test(path) || /\/messages\?/.test(path) || /\/history/.test(path)) return [];
  return [];
}
