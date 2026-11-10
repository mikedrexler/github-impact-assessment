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

test("owner-filtered writable multi-select adds chosen results only on explicit Add", () => {
  const app = dashboard();
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories: ["org/a"], pending: null, limit: 200, discovery_opted_in: true};
    state.organization_discovery = {writable:true,organization:"org", latest: {repositories: [{name:"org/a",write_access:true}, {name:"org/b",write_access:true}, {name:"other/hidden",write_access:true}]}};
    renderSelection();`);
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value), ["org/a", "org/b"]);
  app.element("assessed-repository").options[1].selected = true;
  app.element("assessed-repository").events.change();
  assert.equal(app.element("repository-names").value, "org/a");
  app.element("add-selected-repositories").events.click();
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
  assert.match(app.element("selection-status").textContent, /Draft now contains 2/);
  assert.equal(app.run("state.selection.repositories.length"), 1);
  assert.equal(app.run("selectionDirty"), true);
  app.run("renderSelection();");
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
});

test("manual entry remains separate from writable catalog and multi-selection survives refresh", () => {
  const app = dashboard();
  app.run('state.selection = {repositories: [], pending: null, limit: 200, discovery_opted_in: true}; renderSelection();');
  assert.equal(app.element("assessed-repository").disabled, true);
  app.element("repository-names").value = "Org/Repo";
  app.element("repository-names").events.input();
  assert.equal(app.element("assessed-repository").disabled, true);
  app.element("repository-organization").value = "org";
  app.run(`state.organization_discovery = {writable:true,organization:"org", latest:{repositories:[{name:"org/repo",write_access:true}]}}; renderSelection();`);
  app.element("assessed-repository").options[0].selected = true;
  app.element("assessed-repository").events.change();
  app.run("renderSelection();");
  assert.equal(app.element("assessed-repository").options[0].selected, true);
  app.element("add-selected-repositories").events.click();
  assert.equal(app.element("repository-names").value, "Org/Repo");
  app.run("selectionSaving = true; renderAssessedRepositoryDropdown();");
  assert.equal(app.element("assessed-repository").disabled, true);
});

test("changing owner filter clears hidden options without changing the manual draft", () => {
  const app = dashboard();
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories: ["org/a"], pending: null, limit: 200, discovery_opted_in: false};
    state.discovery = {latest: {repositories: [{name:"org/a",write_access:true}, {name:"org/b",write_access:true}]}};
    renderSelection();`);
  assert.equal(app.element("assessed-repository").disabled, true);
  assert.equal(app.element("assessed-repository").options.length, 0);
  assert.equal(app.element("repository-names").value, "org/a");
  app.run(`state.organization_discovery = {writable:true,organization:"org",latest:{repositories:[{name:"org/a",write_access:true},{name:"org/b",write_access:true}]}}; renderSelection();`);
  assert.equal(app.element("assessed-repository").disabled, false);
  app.element("repository-organization").value = "other";
  app.element("repository-organization").events.input();
  assert.equal(app.element("assessed-repository").disabled, true);
  assert.equal(app.element("assessed-repository").options.length, 0);
  assert.equal(app.element("repository-names").value, "org/a");
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
  assert.match(picker, /<legend>1. Find writable repositories<\/legend>/);
  assert.match(picker, /type="button" id="discover-organization"/);
  assert.match(picker, /id="assessed-repository"/);
  assert.match(picker, /id="assessed-repository" multiple size="6"/);
  assert.ok(picker.indexOf('id="discover-organization"') < picker.indexOf('id="repository-organization"'));
  assert.match(picker, /type="button" id="select-all-repositories"[^>]* disabled/);
  assert.match(picker, />Add all filtered repositories<\/button>/);
  assert.ok(picker.indexOf('id="discover-organization"') < picker.indexOf('id="assessed-repository"'));
  assert.ok(html.indexOf("</fieldset>") < html.indexOf('id="repository-names"'));
  assert.equal((html.match(/id="discover-organization"/g) || []).length, 1);
  assert.equal((html.match(/id="assessed-repository"/g) || []).length, 1);
  assert.doesNotMatch(html, /id="account-discovery-title"/);
  assert.match(html, /<select id="assessed-repository"[^>]* disabled>/);
});

test("select all merges listed repositories into the draft without changing scope and preserves manual names", () => {
  const app = dashboard();
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories: [], pending: null, limit: 3, discovery_opted_in: true};
    state.organization_discovery = {writable:true,organization:"org", latest: {repositories: [{name:"org/a",write_access:true}, {name:"org/b",write_access:true}]}};
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
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories: [], pending: null, limit: 2, discovery_opted_in: true};
    state.organization_discovery = {writable:true,organization:"org", latest: {repositories: [{name:"org/a",write_access:true}, {name:"org/b",write_access:true}, {name:"org/c",write_access:true}]}};
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
  app.element("repository-organization").value = "org";
  app.run('state.organization_discovery = {writable:true,organization:"org",latest:{repositories:[{name:"org/a",write_access:true}]}}; renderSelection();');
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

test("true multi-select retains chosen options during incremental catalog refresh", () => {
  const app = dashboard();
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories:[],limit:200};
    state.organization_discovery = {writable:true,organization:"org", pending:true, latest:{repositories:[{name:"org/a",write_access:true},{name:"org/b",write_access:true}]}};
    renderSelection();`);
  for (const option of app.element("assessed-repository").options) option.selected = true;
  app.element("assessed-repository").events.change();
  app.run(`state.organization_discovery.latest.repositories.push({name:"org/c",write_access:true}); renderSelection();`);
  assert.deepEqual(app.element("assessed-repository").options.filter(option => option.selected).map(option => option.value), ["org/a", "org/b"]);
  assert.match(app.element("assessed-repository-help").textContent, /partial/);
  app.element("add-selected-repositories").events.click();
  assert.equal(app.element("repository-names").value, "org/a\norg/b");
  assert.equal(app.run("state.selection.repositories.length"), 0);
});

test("writable discovery covers any owner without applying assessment scope", async () => {
  const app = dashboard();
  app.element("repository-organization").value = "My-Org";
  app.run(`state.selection = {repositories:["other/manual"],limit:200}; renderSelection();
    globalThis.requests = [];
    fetch = async (url, options) => { requests.push([url, JSON.parse(options.body)]); return {ok:true,json:async()=>({status:"accepted"})}; };
    load = async () => { renderSelection(); };`);
  await app.element("discover-organization").events.click();
  assert.equal(app.run("JSON.stringify(requests)"), '[["/api/writable-repositories",{}]]');
  assert.equal(app.element("repository-names").value, "other/manual");
  assert.match(app.element("selection-status").textContent, /scope is unchanged/);
  app.element("repository-organization").value = "org/repo";
  app.element("repository-organization").events.input();
  assert.equal(app.element("discover-organization").disabled, false);
});

test("discovery error and empty coverage are visible, with no enabled repository picker", async () => {
  const app = dashboard();
  app.element("repository-organization").value = "org";
  app.run(`state.selection = {repositories:[],limit:200};
    state.organization_discovery = {writable:true,organization:"org",latest:{repositories:[],reason:{message:"SSO authorization required"}},error:null};
    renderSelection();`);
  assert.equal(app.element("assessed-repository").disabled, true);
  assert.match(app.element("assessed-repository-help").textContent, /SSO authorization required/);
  app.run(`fetch = async () => { throw new Error("failed"); }; load = async () => renderSelection();`);
  await app.element("discover-organization").events.click();
  assert.match(app.element("selection-status").textContent, /request failed/);
  assert.equal(app.run("selectionSaving"), false);
  app.run(`state.organization_discovery.error = {message:"Discovery failed"}; renderSelection();`);
  assert.match(app.element("assessed-repository-help").textContent, /Discovery failed/);
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

test("continuous coverage renders budget deferrals, missing metadata and insufficient baselines honestly", () => {
  const app = dashboard();
  app.run(`state.config = {github_requests_hour:600,github_requests_minute:30};
    state.rate_budget = {attempts_last_hour:30,provider_remaining_estimate:50,deferred_seconds:120,reason:"provider_quota_reserve"};
    state.latest.observations = {enabled:true,persistent:true,selected:2,fresh_seconds:900,batch_size:20,retention_days:14,record_limit:20000,
      limitation:"Other functions unverified.",rows:[
        {repository:"org/a",fresh:true,gap:null,sample:{at:Date.now()/1000,outcome:"observed",sampled_runs:0,queued_runs:0,completed_runs:0,failed_runs:0},
         metadata_available:true,baseline_samples:0,baseline_median_queued_runs:null,signal:"no_runs_in_sample",recent_incident_samples:[]},
        {repository:"org/hidden",fresh:false,gap:"metadata_unavailable",sample:null,metadata_available:false,
         baseline_samples:0,baseline_median_queued_runs:null,signal:"observation_unavailable",recent_incident_samples:[]}
      ]};
    renderObservations();`);
  const panel = app.element("observations");
  assert.match(panel.children[0].textContent, /DEFERRED for 120s/);
  assert.match(panel.children[1].textContent, /1\/2 selected/);
  assert.match(panel.children[4].children[0].textContent, /no_runs_in_sample/);
  assert.match(panel.children[5].children[0].textContent, /metadata_unavailable/);
  app.run(`state.latest.observations.rows[0].sample.at -= 1000; renderObservations();`);
  assert.match(panel.children[1].textContent, /0\/2 selected/);
  assert.match(panel.children[4].children[0].textContent, /stale/);
  app.run(`state.latest.observations = {enabled:false,error:"Storage failed; coverage unavailable"}; renderObservations();`);
  assert.match(panel.children[1].textContent, /Storage failed/);
});

test("writable picker spans all owner types, filters locally and excludes unverified permissions", () => {
  const app = dashboard();
  app.run(`state.selection = {repositories:[],limit:200}; state.organization_discovery = {
    writable:true,latest:{complete:true,repositories:[
      {name:"me/personal",write_access:true},
      {name:"company/service",write_access:true},
      {name:"friend/collaborator",write_access:true},
      {name:"company/readonly",write_access:false},
      {name:"company/unknown"}
    ]}}; renderSelection();`);
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value),
    ["company/service", "friend/collaborator", "me/personal"]);
  app.element("repository-search").value = "collab";
  app.element("repository-search").events.input();
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value), ["friend/collaborator"]);
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "friend/collaborator");
  app.element("repository-search").value = "";
  app.element("repository-organization").value = "company";
  app.element("repository-organization").events.input();
  assert.deepEqual(app.element("assessed-repository").options.map(option => option.value), ["company/service"]);
  app.element("select-all-repositories").events.click();
  assert.equal(app.element("repository-names").value, "friend/collaborator\ncompany/service");
  assert.equal(app.run("state.selection.repositories.length"), 0);
});

test("configuration shows verification failures rather than accepting manual names as approval", () => {
  const app = dashboard();
  app.run(`state.selection = {mode:"selected",repositories:["me/repo","org/denied"],limit:200};
    state.discovery = {latest:{complete:false,repositories:[{name:"me/repo",write_access:true}],
      repository_errors:[{repository:"org/denied",kind:"write_access_required",message:"Write access not verified"}],
      reason:{message:"Some selected repositories failed validation"}}}; renderSelection();`);
  const panel = app.element("selection-validation");
  assert.match(panel.children[0].textContent, /Verified writable repositories: 1/);
  assert.match(panel.children[2].textContent, /write_access_required/);
  assert.match(app.element("selection-help").textContent, /not access approval/);
  assert.equal(app.element("repository-names").value, "me/repo\norg/denied");
});
