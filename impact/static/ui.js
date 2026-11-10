"use strict";
const $ = (id) => document.getElementById(id);
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
}
function link(text, url) {
  const element = node("a", text);
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && ["github.com", "www.githubstatus.com"].includes(parsed.hostname) && !parsed.username && !parsed.password) {
      element.href = parsed.href;
      element.target = "_blank";
      element.rel = "noreferrer";
    }
  } catch { /* Invalid evidence links remain plain text. */ }
  return element;
}
function date(value) {
  const parsed = new Date(value);
  return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString() : "unknown";
}
function badge(value) {
  const allowed = ["confirmed", "suspected", "unknown", "not-applicable", "operational", "degraded_performance", "partial_outage", "major_outage"];
  return node("span", value.replaceAll("_", " "), `badge ${allowed.includes(value) ? value : "unknown"}`);
}
function paragraph(parent, text, cls) { parent.append(node("p", text, cls)); }
