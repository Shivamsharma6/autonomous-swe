import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const ADMIN_TOKEN = 'f120907192465216701d4cfed5fe355b1ac044972e7e9ff0e85e3bb4f956a977';
const SCREENSHOT_DIR = path.resolve('tests/e2e_browser/screenshots');

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

async function capture(page, name) {
  const filePath = path.join(SCREENSHOT_DIR, `${name}.png`);
  await page.screenshot({ path: filePath, fullPage: true });
  log(`Screenshot saved: ${name}.png`);
}

async function waitForDialogClosed(page, dialogId, timeout = 10000) {
  await page.waitForFunction(
    (id) => {
      const el = document.getElementById(id);
      return !el || !el.open;
    },
    dialogId,
    { timeout }
  );
}

async function run() {
  log('Starting Playwright End-to-End UI Browser Test...');
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }
  });
  
  const page = await context.newPage();

  page.on('console', msg => {
    log(`[Browser Console ${msg.type()}]: ${msg.text()}`);
  });

  page.on('pageerror', err => {
    log(`[Browser Uncaught Error]: ${err.message}`);
  });

  page.on('requestfailed', req => {
    log(`[Browser Network Failed]: ${req.method()} ${req.url()} - ${req.failure()?.errorText}`);
  });

  try {
    // 1. Open home page
    log('Step 1: Navigating to http://localhost:3000');
    await page.goto('http://localhost:3000', { waitUntil: 'networkidle' });
    await capture(page, '01_initial_landing');

    // 2. Test Invalid Authentication
    log('Step 2: Testing workspace authentication with invalid token...');
    await page.click('#openAuth');
    await page.waitForSelector('#authDialog[open]');
    await page.fill('#adminToken', 'invalid_token_123456789012345678901234567890');
    await page.click('#connectWorkspace');
    await page.waitForTimeout(1000);
    const authFeedback = await page.textContent('#authFeedback');
    log(`Auth feedback for invalid token: "${authFeedback}"`);
    await capture(page, '02_auth_invalid');

    // 3. Test Valid Authentication
    log('Step 3: Authenticating with valid admin token...');
    await page.fill('#adminToken', ADMIN_TOKEN);
    await page.click('#connectWorkspace');
    await waitForDialogClosed(page, 'authDialog');
    await page.waitForTimeout(1000);
    const authBtnText = await page.textContent('#authBtnText');
    log(`Auth button text after sign-in: "${authBtnText}"`);
    await capture(page, '03_auth_success');

    // 4. Test Model Settings Dialog
    log('Step 4: Opening Model Settings and Discovering Models...');
    await page.click('#modelStudioBtn');
    await page.waitForSelector('#modelStudioDialog[open]');
    await capture(page, '04_model_settings_opened');

    // Trigger Model Discovery
    log('Triggering model discovery...');
    await page.click('#probeModelsBtn');
    await page.waitForTimeout(3000);
    const discoveryStatus = await page.textContent('#modelDiscoveryStatus');
    log(`Model discovery status: "${discoveryStatus}"`);
    await capture(page, '05_model_discovery');

    // Select primary model (nemotron-3.5-lightning:30b-mlx)
    log('Selecting model nemotron-3.5-lightning:30b-mlx...');
    await page.fill('#primaryModelInput', 'nemotron-3.5-lightning:30b-mlx');
    
    // Set timeout to 300
    await page.fill('#modelTimeoutInput', '300');

    // Test connection
    log('Testing model connection...');
    await page.click('#testModelBtn');
    await page.waitForSelector('#modelTestCard:not(.hidden)', { timeout: 30000 });
    const badge = await page.textContent('#testStatusBadge');
    const latency = await page.textContent('#testLatency');
    const snippet = await page.textContent('#testOutputSnippet');
    log(`Connection test result: Badge="${badge}", Latency="${latency}", Snippet="${snippet}"`);
    await capture(page, '06_model_test_result');

    // Save and Apply Model
    log('Saving model settings...');
    await page.click('#modelStudioForm button[type="submit"]');
    await waitForDialogClosed(page, 'modelStudioDialog');
    await page.waitForTimeout(1000);
    await capture(page, '07_model_saved');

    // 5. Test Palette & Help Guide
    log('Step 5: Testing Quick Guide and Command Palette...');
    await page.click('#tourHelpBtn');
    await page.waitForSelector('#helpDialog[open]');
    await page.keyboard.press('Escape');
    await waitForDialogClosed(page, 'helpDialog');

    // Test Command Palette
    await page.click('#openCommandPalette');
    await page.waitForSelector('#paletteDialog[open]');
    await page.fill('#paletteInput', 'new run');
    await page.waitForTimeout(500);
    await capture(page, '08_palette_open');
    await page.keyboard.press('Escape');
    await waitForDialogClosed(page, 'paletteDialog');

    // 6. Navigate to New Run
    log('Step 6: Navigating to New Run launchpad...');
    await page.click('a[data-nav="new"]');
    await page.waitForSelector('#onboardingSection:not(.hidden)');
    await capture(page, '09_new_run_page');

    // 7. Test Connecting Repository
    log('Step 7: Testing Repository Connection...');
    await page.fill('#projectName', 'UI E2E Calculator Test');
    await page.fill('#sourcePath', 'ui-e2e-calculator-20260831');
    await page.fill('#defaultBranch', 'main');
    await page.click('#registerProjectBtn');
    await page.waitForTimeout(2000);

    const projectIdentity = await page.textContent('#projectIdentity');
    log(`Project identity card text: "${projectIdentity}"`);
    const isStartDisabled = await page.isDisabled('#startRun');
    log(`Is Start Run disabled: ${isStartDisabled}`);
    await capture(page, '10_repo_connected');

    // 8. Fill Goal and Launch Run
    log('Step 8: Entering Task Goal and Starting Run...');
    const goal = `Add a subtract function to src/calculator.py:
def subtract(a: int, b: int) -> int:
    return a - b
Preserve the existing add function.
Add comprehensive unit tests in tests/test_calculator.py covering subtract(7, 3) == 4, subtract(0, 5) == -5, and subtract(-2, -5) == 3.
Run tests using python -m unittest discover -s tests.
Update README.md with subtraction documentation.`;

    await page.fill('#runGoal', goal);
    await page.waitForTimeout(500);
    const isStartDisabledNow = await page.isDisabled('#startRun');
    log(`Is Start Run disabled after goal: ${isStartDisabledNow}`);
    await capture(page, '11_goal_entered');

    log('Clicking Start Run...');
    await page.click('#startRun');
    
    // Wait for navigation to dashboard
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    const runId = await page.textContent('#runIdText');
    const runStatus = await page.textContent('#runStatusBadge');
    log(`Launched Run ID: "${runId}", Initial Status: "${runStatus}"`);
    await capture(page, '12_run_dashboard_initial');

    // 9. Monitor Run Execution
    log('Step 9: Monitoring live run execution in real-time...');
    let lastStatus = runStatus;
    let iterations = 0;
    const maxIterations = 120; // poll every 5s up to 10 minutes

    while (iterations < maxIterations) {
      await page.waitForTimeout(5000);
      iterations++;

      const currentStatus = await page.textContent('#runStatusBadge');
      const taskSummary = await page.textContent('#taskSummary');
      const tokenTotal = await page.textContent('#tokenTotal');
      const modelCost = await page.textContent('#modelCost');

      log(`[Run ${runId} Iteration ${iterations}] Status: ${currentStatus} | Tasks: ${taskSummary} | Tokens: ${tokenTotal} | Cost: ${modelCost}`);
      await capture(page, `13_run_progress_iter_${iterations}`);

      if (currentStatus.trim().toUpperCase().includes('APPROVAL')) {
        log('Run is waiting for operator approval. Navigating to Approvals tab and approving...');
        await page.click('#tab-approvals');
        await page.waitForTimeout(1000);
        await capture(page, `13_run_approval_requested_iter_${iterations}`);
        const approveBtn = await page.$('#approvalList button.primary');
        if (approveBtn) {
          await approveBtn.click();
          await page.waitForTimeout(500);
          const operatorInput = await page.$('#approvalOperator');
          if (operatorInput) {
            await operatorInput.fill('E2E Automated Operator');
          }
          await page.click('#confirmApproval');
          await page.waitForTimeout(2000);
          await capture(page, `13_run_approval_submitted_iter_${iterations}`);
          await page.click('#tab-tasks');
        }
      }

      // Check for terminal states
      if (['COMPLETED', 'FAILED', 'CANCELLED'].includes(currentStatus.trim().toUpperCase())) {
        log(`Run reached terminal status: ${currentStatus}`);
        break;
      }
    }

    // Capture final state screenshots of each tab
    log('Step 10: Capturing final state across all detail tabs...');
    
    // Tasks Tab (List view)
    await page.click('#tab-tasks');
    await page.click('#taskListView');
    await page.waitForTimeout(1000);
    await capture(page, '14_final_tasks_list');

    // Tasks Tab (Graph view)
    await page.click('#taskGraphView');
    await page.waitForTimeout(1000);
    await capture(page, '15_final_tasks_graph');

    // Open first task details if exists
    const taskRows = await page.$$('#taskList .task-item, #taskList .task-row, #taskDag .dag-node');
    if (taskRows.length > 0) {
      log(`Found ${taskRows.length} tasks. Clicking first task...`);
      await taskRows[0].click();
      await page.waitForTimeout(1000);
      await capture(page, '16_task_drawer_open');
      await page.keyboard.press('Escape');
    }

    // Activity Tab
    await page.click('#tab-activity');
    await page.waitForTimeout(1000);
    await capture(page, '17_final_activity');

    // Approvals Tab
    await page.click('#tab-approvals');
    await page.waitForTimeout(1000);
    await capture(page, '18_final_approvals');

    // Files Tab
    await page.click('#tab-artifacts');
    await page.waitForTimeout(1000);
    await capture(page, '19_final_files');

    log('E2E Test Execution Completed.');

  } catch (err) {
    log(`ERROR during test: ${err.message}\n${err.stack}`);
    await capture(page, '99_error_state');
  } finally {
    await browser.close();
  }
}

run();
