// Drive the DeepWiki-style UI with a mocked backend so the flow is verifiable
// offline. Modes: loaded (landing pool), answer (wiki Ask mode), mobile, down.
import { chromium } from "playwright";

const REPOS = [
  { id: "demo__repo-1", name: "demo/repo @ abc12345", repo: "demo/repo", base_commit: "abc12345", commit_short: "abc12345", language: "Python", description: "A demo repository used to verify the UI offline.", problem_statement: "An example issue.", languages: ["Python"], file_count: 42, capabilities: { sparse_search: true } },
  { id: "demo__lib-2", name: "demo/lib @ def67890", repo: "demo/lib", base_commit: "def67890", commit_short: "def67890", language: "Rust", description: "A second demo repo.", problem_statement: "", languages: ["Rust"], file_count: 18, capabilities: { sparse_search: true } },
];

const TREE = { repo: "demo/repo", pages: [{ id: "overview", title: "Overview", children: [] }, { id: "architecture", title: "Architecture", children: [{ id: "mod__demo", title: "demo", children: [] }] }] };
const PAGE = { id: "overview", title: "Overview", markdown: "# demo/repo Overview\n\nThis is a `demo` repository.\n\n## Module structure\n\n```mermaid\ngraph TD\n  A[demo] --> B[mod]\n```\n\n## Section\n\nSome prose with [a jump](#section).", citations: [{ file: "demo/x.py", start_line: 1, end_line: 5, node_name: "X", type: "class", score: 0.9, content: "class X: pass" }], diagram: "graph TD\n  A[demo] --> B[mod]" };
const ANSWER = {
  answer: "Authentication lives in **`demo/auth.py`**.",
  citations: [{ file: "demo/auth.py", start_line: 10, end_line: 20, node_name: "authenticate", type: "function", score: 0.9, content: "def authenticate(): ..." }],
  tool_calls: [{ skill_id: "bm25_search", arguments: { query: "auth" }, result_count: 1, duration_ms: 12, error: null }],
  total_turns: 1, total_duration_ms: 120,
};

const json = (body) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

async function mockBackend(page) {
  await page.route("**/api/repos/*/wiki/*", (r) => r.fulfill(json(PAGE)));
  await page.route("**/api/repos/*/wiki", (r) => r.fulfill(json(TREE)));
  await page.route("**/api/repos/*/source**", (r) => r.fulfill(json({ file: "demo/auth.py", start_line: 10, end_line: 20, content: "def authenticate(): ..." })));
  await page.route("**/api/chat", (r) => r.fulfill(json(ANSWER)));
  await page.route("**/api/repos", (r) => r.fulfill(json(REPOS)));
}

async function run({ mock, name, ask, mobile }) {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: mobile ? { width: 375, height: 720 } : { width: 1280, height: 900 } });
  const consoleErrors = [], pageErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  if (mock) await mockBackend(page);
  else await page.route("**/api/**", (r) => r.abort());

  await page.goto("http://127.0.0.1:3000/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  const result = { name };
  result.repoCards = await page.locator(".repo-card:not(.add-repo)").count();
  result.reposEmpty = await page.locator(".landing .empty").count();

  if (ask) {
    await page.locator(".repo-card:not(.add-repo)").first().click();
    await page.waitForTimeout(1000);
    await page.locator(".mode-tab", { hasText: "Ask" }).click();
    await page.waitForTimeout(300);
    await page.locator(".ask-panel .composer input").fill("How does authentication work?");
    await page.locator(".ask-panel .composer button[type=submit]").click();
    await page.waitForSelector(".msg.assistant .bubble .markdown", { timeout: 8000 });
    await page.waitForTimeout(400);
    result.citations = await page.locator(".citation").count();
    result.assistantMsgs = await page.locator(".msg.assistant").count();
  }

  result.overflow = await page.evaluate(() => ({ scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth }));
  result.horizontalScroll = result.overflow.scrollW > result.overflow.clientW + 1;
  await page.screenshot({ path: `../verification/${name}.png`, fullPage: true });
  await browser.close();
  result.consoleErrors = consoleErrors;
  result.pageErrors = pageErrors;
  console.log(JSON.stringify(result, null, 2));
  return result;
}

const mode = process.argv[2] ?? "loaded";
if (mode === "loaded") await run({ mock: true, name: "qa-loaded" });
else if (mode === "answer") await run({ mock: true, name: "qa-answer", ask: true });
else if (mode === "mobile") await run({ mock: true, name: "qa-mobile", mobile: true });
else if (mode === "down") await run({ mock: false, name: "qa-backend-down" });
