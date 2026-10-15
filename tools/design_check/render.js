// tools/design_check/render.js
// A minimal renderer for the design boards (.dc.html) in docs/design/boards,
// so a board can be screenshotted next to the app without the canvas editor.
// It supports what the boards use: {{holes}} in text and attributes, <sc-for>,
// <sc-if>, <dc-import> and a Component class whose renderVals() supplies the
// values. Development tool only; it runs the boards' own scripts, so it must
// only ever load the checked-in boards.
"use strict";

const BOARDS = "/docs/design/boards/";
// Uploaded canvas assets, by id, mapped to files in the repository.
const BLOBS = {
  db54b5564615ab70ce9e211b583e7378: "/workspace/ui/img/unlimit.svg",
  "0e6863cecdf84627bc5ac53d8917418d": "/workspace/ui/img/unlimit-white.svg",
  "80914b56cf1699947f5651b6ecf024e6": "/docs/screenshots/workspace-inbox.png",
  "9c13f53304689b1b5dfe79b534813196": "/docs/screenshots/workspace-meeting.png",
};

class DCLogic {
  constructor(props) { this.props = props || {}; this.state = {}; }
  setState(next) { Object.assign(this.state, next); }
  forceUpdate() {}
}

// -- loading -------------------------------------------------------------------
// <sc-for>, <sc-if> and <dc-import> become <template> before parsing: the HTML
// parser keeps a template where it is written, even inside a table or list.
async function loadBoard(file) {
  const src = await (await fetch(BOARDS + file)).text();
  const html = src.replace(/<(\/?)(sc-for|sc-if|dc-import)\b/g,
    (_, close, tag) => (close ? "</template" : `<template data-kind="${tag}"`));
  const doc = new DOMParser().parseFromString(html, "text/html");
  const script = doc.querySelector("script[data-dc-script]");
  const Component = new Function("DCLogic", `${script.textContent}\nreturn Component;`)(DCLogic);
  return { root: doc.querySelector("x-dc"), Component };
}

// -- values ----------------------------------------------------------------------
function lookup(path, scope) {
  const p = path.trim();
  if (p === "true") return true;
  if (p === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(p)) return Number(p);
  let v = scope;
  for (const key of p.split(".")) {
    if (v === null || v === undefined) return undefined;
    v = v[key];
  }
  return v;
}

const HOLE = /\{\{([^}]*)\}\}/g;
const interpolate = (text, scope) => text.replace(HOLE, (_, p) => {
  const v = lookup(p, scope);
  return v === null || v === undefined ? "" : String(v);
});
const wholeHole = (text) => (/^\s*\{\{([^}]*)\}\}\s*$/.exec(text) || [])[1];
const camel = (name) => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const blob = (url) => url.replace(/^\/_blob\/([0-9a-f]{32})$/, (m, id) => BLOBS[id] || m);

function attrValue(node, name, scope) {
  const raw = node.getAttribute(name);
  if (raw === null) return undefined;
  const hole = wholeHole(raw);
  return hole === undefined ? interpolate(raw, scope) : lookup(hole, scope);
}

// -- rendering --------------------------------------------------------------------
async function renderNodes(nodes, scope, parent) {
  for (const node of [...nodes]) await renderNode(node, scope, parent);
}

async function renderNode(node, scope, parent) {
  if (node.nodeType === Node.TEXT_NODE) {
    parent.append(document.createTextNode(interpolate(node.nodeValue, scope)));
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  const tag = node.localName;
  if (tag === "helmet") return;
  if (tag === "template" && node.dataset.kind) {
    const kids = node.content.childNodes;
    const kind = node.dataset.kind;
    if (kind === "sc-if") {
      if (attrValue(node, "value", scope)) await renderNodes(kids, scope, parent);
    } else if (kind === "sc-for") {
      const list = attrValue(node, "list", scope) || [];
      const as = node.getAttribute("as");
      for (let i = 0; i < list.length; i++)
        await renderNodes(kids, { ...scope, [as]: list[i], $index: i }, parent);
    } else if (kind === "dc-import") {
      const props = {};
      for (const a of node.attributes) {
        if (a.name === "data-kind" || a.name === "name" || a.name.startsWith("hint-")) continue;
        props[camel(a.name)] = attrValue(node, a.name, scope);
      }
      await mount(`${node.getAttribute("name")}.dc.html`, props, parent);
    }
    return;
  }
  const el = document.importNode(node, false);
  for (const a of [...node.attributes]) {
    if (!a.value.includes("{{")) {
      if (a.name === "src") el.setAttribute("src", blob(a.value));
      continue;
    }
    const hole = wholeHole(a.value);
    const v = hole === undefined ? interpolate(a.value, scope) : lookup(hole, scope);
    if (typeof v === "boolean") {
      if (v) el.setAttribute(a.name, ""); else el.removeAttribute(a.name);
    } else if (v === null || v === undefined) {
      el.removeAttribute(a.name);
    } else {
      el.setAttribute(a.name, a.name === "src" ? blob(String(v)) : String(v));
    }
  }
  parent.append(el);
  await renderNodes(node.childNodes, scope, el);
}

async function mount(file, props, parent) {
  const { root, Component } = await loadBoard(file);
  const values = new Component(props).renderVals();
  await renderNodes(root.childNodes, values, parent);
}

// ?board=Home.dc.html&direction=Dark renders that board with those props.
const params = new URLSearchParams(location.search);
const board = params.get("board");
const props = {};
for (const [k, v] of params) if (k !== "board") props[k] = v;
mount(board, props, document.body)
  .then(() => document.fonts.ready)
  .then(() => { document.documentElement.dataset.ready = "1"; })
  .catch((err) => { document.body.textContent = `Could not render ${board}: ${err}`; });
