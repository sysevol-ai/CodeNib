// Drive the QA UI with a mocked backend so the flow is verifiable offline.
import { chromium } from "playwright";

const REPOS = [
  { id: "django__django-11099", name: "django/django @ 419a78300", repo: "django/django", base_commit: "419a78300f7cd27611196e1e464d50fd0385ff27", commit_short: "419a7830", language: "python", problem_statement: "AuthenticationForm's username field max_length is not set...", languages: ["python"], file_count: 2891, capabilities: { sparse_search: true } },
  { id: "expressjs__express-1234", name: "expressjs/express @ a1b2c3d4", repo: "expressjs/express", base_commit: "a1b2c3d4e5f6", commit_short: "a1b2c3d4", language: "javascript", problem_statement: "res.redirect encodes the URL incorrectly...", languages: ["javascript"], file_count: 142, capabilities: { sparse_search: true } },
  { id: "gin-gonic__gin-555", name: "gin-gonic/gin @ 9f8e7d6c", repo: "gin-gonic/gin", base_commit: "9f8e7d6c", commit_short: "9f8e7d6c", language: "go", problem_statement: "Context.Bind fails on nested structs...", languages: ["go"], file_count: 213, capabilities: { sparse_search: true } },
  { id: "serde-rs__serde-99", name: "serde-rs/serde @ 1122aabb", repo: "serde-rs/serde", base_commit: "1122aabb", commit_short: "1122aabb", language: "rust", problem_statement: "Derive macro mishandles lifetime bounds...", languages: ["rust"], file_count: 178, capabilities: { sparse_search: true } },
];

const ANSWER = {
  answer:
    "Authentication is handled in **`django/contrib/auth/forms.py`**. The `AuthenticationForm` validates credentials in its `clean()` method, delegating to `authenticate()`.\n\n- `AuthenticationForm.clean()` runs the backends\n- `authenticate()` walks `AUTHENTICATION_BACKENDS`\n\n```python\nuser = authenticate(self.request, username=u, password=p)\n```",
  citations: [
    { file: "django/contrib/auth/forms.py", start_line: 191, end_line: 240, node_name: "AuthenticationForm", type: "class", score: 0.91, content: "class AuthenticationForm(forms.Form):\n    def clean(self):\n        ..." },
    { file: "django/contrib/auth/__init__.py", start_line: 60, end_line: 90, node_name: "authenticate", type: "function", score: 0.84, content: "def authenticate(request=None, **credentials):\n    ..." },
  ],
  tool_calls: [{ skill_id: "bm25_search", arguments: { query: "authentication" }, result_count: 2, duration_ms: 12, error: null }],
  total_turns: 2,
  total_duration_ms: 540,
};

async function run({ mock, name, ask, mobile }) {
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: mobile ? { width: 375, height: 720 } : { width: 1280, height: 900 },
  });
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  if (mock) {
    await page.route("**/api/repos", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(REPOS) })
    );
    await page.route("**/api/chat", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ANSWER) })
    );
  } else {
    // simulate backend down
    await page.route("**/api/**", (r) => r.abort());
  }

  await page.goto("http://127.0.0.1:3000/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  const result = { name };
  if (ask) {
    await page.locator(".composer input").fill("How does authentication work?");
    await page.locator(".composer button[type=submit]").click();
    await page.waitForSelector(".msg.assistant .bubble .markdown", { timeout: 8000 });
    await page.waitForTimeout(500);
    result.citations = await page.locator(".citation").count();
    result.assistantMsgs = await page.locator(".msg.assistant").count();
  }

  result.repoCards = await page.locator(".repo-card").count();
  result.reposError = await page.locator(".repo-empty").count();
  result.overflow = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }));
  result.horizontalScroll = result.overflow.scrollW > result.overflow.clientW + 1;

  await page.screenshot({ path: `../verification/${name}.png`, fullPage: true });
  await browser.close();
  result.consoleErrors = consoleErrors;
  result.pageErrors = pageErrors;
  console.log(JSON.stringify(result, null, 2));
}

const mode = process.argv[2] ?? "loaded";
if (mode === "loaded") await run({ mock: true, name: "qa-loaded" });
else if (mode === "answer") await run({ mock: true, name: "qa-answer", ask: true });
else if (mode === "mobile") await run({ mock: true, name: "qa-mobile", mobile: true });
else if (mode === "down") await run({ mock: false, name: "qa-backend-down" });
