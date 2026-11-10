const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.events = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, handler) { this.events[event] = handler; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  get options() { return this.children; }
}

function dashboard(checks = []) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(id));
      return elements.get(id);
    },
    createElement(tag) { return new Element(tag); },
  };
  const context = vm.createContext({
    document, URL, AbortSignal,
    window: {location: {hash: ""}, addEventListener: () => {}},
    fetch: () => new Promise(() => {}),
    setInterval: () => {},
  });
  vm.runInContext(readFileSync(join(__dirname, "../impact/static/ui.js"), "utf8"), context);
  vm.runInContext(readFileSync(join(__dirname, "../impact/static/app.js"), "utf8"), context);
  context.testRows = checks;
  vm.runInContext("state = {latest: {checks: testRows, incidents: [{}]}}; renderChecks();", context);
  return {
    element: document.getElementById,
    run: (script) => vm.runInContext(script, context),
    update(rows) {
      context.testRows = rows;
      vm.runInContext("state.latest.checks = testRows; renderChecks();", context);
    },
  };
}

function row(repository, component = "Actions", classification = "unknown") {
  return {
    repository, repository_url: `https://github.com/${repository}`,
    component, classification, reason: "Sample only", incident: "Fixture",
    evidence: [], limitations: "Not proof of health",
  };
}

test("dropdown lists sorted distinct repositories with an all option", () => {
  const app = dashboard([row("org/z"), row("org/a"), row("org/z", "Pages")]);
  assert.deepEqual(app.element("evidence-repository").options.map(option => option.value), ["", "org/a", "org/z"]);
  assert.equal(app.element("evidence-repository").disabled, false);
  assert.equal(app.element("checks").children.length, 3);
});

test("repository and text filters intersect and selection survives refreshed results", () => {
  const app = dashboard([row("org/a"), row("org/b"), row("org/a", "Pages")]);
  app.element("evidence-repository").value = "org/a";
  app.element("evidence-repository").events.change();
  assert.equal(app.element("checks").children.length, 2);
  app.element("filter").value = "Pages";
  app.element("filter").events.input();
  assert.equal(app.element("checks").children.length, 1);
  assert.equal(app.element("checks").children[0].children[0].children[0].textContent, "org/a");
  app.update([row("org/a", "Pages"), row("org/c")]);
  assert.equal(app.element("evidence-repository").value, "org/a");
  assert.equal(app.element("checks").children.length, 1);
  assert.match(app.element("check-count").textContent, /1 of 1 matching assessments \(2 total\)/);
});

test("all repositories restores results and changes reset pagination", () => {
  const app = dashboard(Array.from({length: 80}, (_, index) => row(index < 60 ? "org/a" : "org/b")));
  app.element("more").events.click();
  assert.equal(app.element("checks").children.length, 80);
  app.element("evidence-repository").value = "org/a";
  app.element("evidence-repository").events.change();
  assert.equal(app.element("checks").children.length, 50);
  assert.equal(app.element("more").hidden, false);
  app.element("evidence-repository").value = "";
  app.element("evidence-repository").events.change();
  assert.equal(app.element("checks").children.length, 50);
  assert.match(app.element("check-count").textContent, /50 of 80/);
});

test("removed repositories reset selection and no matches have an explicit empty state", () => {
  const app = dashboard([row("org/a"), row("org/b")]);
  app.element("evidence-repository").value = "org/b";
  app.element("evidence-repository").events.change();
  app.update([row("org/a")]);
  assert.equal(app.element("evidence-repository").value, "");
  app.element("filter").value = "absent";
  app.element("filter").events.input();
  assert.match(app.element("checks").children[0].children[0].textContent, /No evidence matches/);
  assert.equal(app.element("more").hidden, true);
  app.update([]);
  assert.equal(app.element("evidence-repository").disabled, true);
  assert.equal(app.element("evidence-repository").options.length, 1);
  assert.match(app.element("checks").children[0].children[0].textContent, /No scoped repository checks/);
});

test("no snapshot clears stale dropdown options without fetching or changing assessment scope", () => {
  const app = dashboard([row("org/a")]);
  app.element("evidence-repository").value = "org/a";
  app.run("state.latest = null; renderChecks();");
  assert.equal(app.element("evidence-repository").value, "");
  assert.equal(app.element("evidence-repository").disabled, true);
  assert.deepEqual(app.element("evidence-repository").options.map(option => option.value), [""]);
});

test("repository option labels are plain text, never HTML", () => {
  const name = "org/<script>test</script>";
  const app = dashboard([row(name)]);
  assert.equal(app.element("evidence-repository").options[1].textContent, name);
  assert.equal(app.element("evidence-repository").options[1].value, name);
});

test("assessed repository dropdown uses known scope without requiring incident evidence", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: ["org/a"], pending: null, limit: 200, discovery_opted_in: true};
    state.discovery = {latest: {repositories: [{name:"org/a"}, {name:"org/b"}]}};
    renderSelection();`);
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value), ["", "org/a", "org/b"]);
  app.element("assessed-repository").value = "org/b";
  app.element("assessed-repository").events.change();
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
  assert.match(app.element("selection-status").textContent, /draft selection/);
  assert.equal(app.run("state.selection.repositories.length"), 1);
  assert.equal(app.run("selectionDirty"), true);
  app.run("renderSelection();");
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
});

test("assessed dropdown avoids duplicates and supports manually entered repository names", () => {
  const app = dashboard();
  app.run('state.selection = {repositories: [], pending: null, limit: 200, discovery_opted_in: true}; renderSelection();');
  assert.equal(app.element("assessed-repository").disabled, true);
  app.element("repository-names").value = "Org/Repo";
  app.element("repository-names").events.input();
  assert.equal(app.element("assessed-repository").disabled, false);
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value), ["", "Org/Repo"]);
  app.element("assessed-repository").value = "org/repo";
  app.element("assessed-repository").events.change();
  assert.equal(app.element("repository-names").value, "Org/Repo");
  app.run("selectionSaving = true; renderAssessedRepositoryDropdown();");
  assert.equal(app.element("assessed-repository").disabled, true);
});

test("dropdown stays greyed out before discovery even with known and manually entered repositories", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: ["org/a"], pending: null, limit: 200, discovery_opted_in: false};
    state.discovery = {latest: {repositories: [{name:"org/a"}, {name:"org/b"}]}};
    renderSelection();`);
  assert.equal(app.element("assessed-repository").disabled, true);
  assert.match(app.element("assessed-repository").options[0].textContent, /discovery first/);
  app.element("assessed-repository").value = "org/b";
  app.element("assessed-repository").events.change();
  assert.equal(app.element("repository-names").value, "org/a");
  app.run("state.selection.discovery_opted_in = true; renderSelection();");
  assert.equal(app.element("assessed-repository").disabled, false);
  app.element("assessed-repository").value = "org/b";
  app.element("assessed-repository").events.change();
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
});

test("navigation preserves tab order and discovery and dropdown share one compact group above manual entry", () => {
  for (const file of ["index.html", "past-incidents.html"]) {
    const html = readFileSync(join(__dirname, "../impact/static", file), "utf8");
    const nav = html.match(/<nav[\s\S]*?<\/nav>/)[0];
    assert.ok(nav.indexOf("Live Assessment") < nav.indexOf("Past Incidents"));
    assert.ok(nav.indexOf("Past Incidents") < nav.indexOf("Configuration"));
  }
  const html = readFileSync(join(__dirname, "../impact/static/index.html"), "utf8");
  assert.match(html, /<h2 id="repository-selection-title">Repositories to Assess<\/h2>/);
  const picker = html.match(/<fieldset class="repository-picker">[\s\S]*?<\/fieldset>/)[0];
  assert.match(picker, /<legend>Discover and add repositories<\/legend>/);
  assert.match(picker, /type="button" id="all-repositories"/);
  assert.match(picker, /id="assessed-repository"/);
  assert.match(picker, /type="button" id="select-all-repositories"[^>]* disabled/);
  assert.match(picker, />Select all available repositories<\/button>/);
  assert.ok(picker.indexOf('id="all-repositories"') < picker.indexOf('id="assessed-repository"'));
  assert.ok(html.indexOf("</fieldset>") < html.indexOf('id="repository-names"'));
  assert.equal((html.match(/id="all-repositories"/g) || []).length, 1);
  assert.equal((html.match(/id="assessed-repository"/g) || []).length, 1);
  assert.doesNotMatch(html, /id="account-discovery-title"/);
  assert.match(html, /<select id="assessed-repository"[^>]* disabled>/);
});

test("select all merges listed repositories into the draft without changing scope and preserves manual names", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: [], pending: null, limit: 3, discovery_opted_in: true};
    state.discovery = {latest: {repositories: [{name:"org/a"}, {name:"org/b"}]}};
    renderSelection();`);
  app.element("repository-names").value = "Org/A\norg/manual";
  app.element("repository-names").events.input();
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "Org/A\norg/manual\norg/b");
  assert.equal(app.run("selectionDirty"), true);
  assert.equal(app.run("state.selection.repositories.length"), 0);
  assert.match(app.element("selection-status").textContent, /Discovery may be partial/);
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "Org/A\norg/manual\norg/b");
});

test("select all rejects over-limit sets without truncating or overwriting draft", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: [], pending: null, limit: 2, discovery_opted_in: true};
    state.discovery = {latest: {repositories: [{name:"org/a"}, {name:"org/b"}, {name:"org/c"}]}};
    renderSelection();`);
  app.element("repository-names").value = "org/manual";
  app.element("repository-names").events.input();
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "org/manual");
  assert.match(app.element("selection-status").textContent, /selection limit is 2/);
  assert.equal(app.run("state.selection.repositories.length"), 0);
});

test("select all is disabled before discovery, with no names, while saving and when stopped", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: [], pending: null, limit: 200, discovery_opted_in: false};
    renderSelection();`);
  assert.equal(app.element("select-all-repositories").disabled, true);
  app.run("state.selection.discovery_opted_in = true; renderSelection();");
  assert.equal(app.element("select-all-repositories").disabled, true);
  app.run('state.discovery = {latest:{repositories:[{name:"org/a"}]}}; renderSelection();');
  assert.equal(app.element("select-all-repositories").disabled, false);
  app.run("selectionSaving = true; renderSelection();");
  assert.equal(app.element("select-all-repositories").disabled, true);
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "");
  app.run("selectionSaving = false; state.stopped = true; renderSelection();");
  assert.equal(app.element("select-all-repositories").disabled, true);
});

test("selection help distinguishes persisted applied scope from temporary demo state", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories: [], pending: null, limit: 200, persistent: true}; renderSelection();`);
  assert.match(app.element("selection-help").textContent, /restored after restart/);
  assert.match(app.element("selection-help").textContent, /unapplied drafts are not saved/);
  app.run("state.selection.persistent = false; renderSelection();");
  assert.match(app.element("selection-help").textContent, /demo\/test selection is memory-only/);
});

test("active inference control requires configuration and scoped incident and labels stale shared samples", () => {
  const app = dashboard();
  app.run("renderCopilotProbe();");
  assert.equal(app.element("copilot-probe").disabled, true);
  app.run(`state.copilot_probe = {configuration: "ready", status:"idle", requested_model:"fixture-model"};
    renderCopilotProbe();`);
  assert.equal(app.element("copilot-probe").disabled, true);
  app.run(`state.latest.checks = [{adapter:"copilot_model"}]; renderCopilotProbe();`);
  assert.equal(app.element("copilot-probe").disabled, false);
  app.run(`state.busy = true; renderCopilotProbe();`);
  assert.equal(app.element("copilot-probe").disabled, true);
  app.run(`state.busy = false; state.copilot_probe.sample = {
    outcome:"success", latency_ms:20, checked_at:"2020-01-01T00:00:00Z"}; renderCopilotProbe();`);
  assert.match(app.element("copilot-probe-status").textContent, /STALE/);
  assert.match(app.element("copilot-probe-status").textContent, /not independently verified/);
});

test("configuration navigation isolates repository controls and preserves draft state", () => {
  const app = dashboard();
  assert.equal(app.element("dashboard-panel").hidden, false);
  assert.equal(app.element("configuration-panel").hidden, true);
  assert.equal(app.element("dashboard-tab")["aria-current"], "page");
  app.element("repository-names").value = "org/draft";
  app.run('window.location.hash = "#configuration"; renderPage();');
  assert.equal(app.element("configuration-panel").hidden, false);
  assert.equal(app.element("dashboard-panel").hidden, true);
  assert.equal(app.element("configuration-tab")["aria-current"], "page");
  assert.equal(app.element("dashboard-tab")["aria-current"], undefined);
  app.run('window.location.hash = "#dashboard"; renderPage();');
  assert.equal(app.element("repository-names").value, "org/draft");
  assert.equal(app.element("configuration-panel").hidden, true);
  app.run('window.location.hash = "#unknown"; renderPage();');
  assert.equal(app.element("dashboard-panel").hidden, false);
});
