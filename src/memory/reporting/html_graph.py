"""Standalone HTML graph visualization export."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import sanitize_label


def write_graph_html(payload: dict[str, Any], output_path: str | Path) -> Path:
    """Write a standalone interactive HTML visualization for an exported graph."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_graph_html(payload), encoding="utf-8")
    return path


def render_graph_html(payload: dict[str, Any]) -> str:
    """Render a REQL HTML page from graph export data."""

    payload = _safe_graph_payload(_visual_graph_payload(payload))
    data = _safe_json(payload)
    source_counts = payload.get("source_counts") if isinstance(payload.get("source_counts"), dict) else {}
    node_count = int(source_counts.get("nodes") or len(payload.get("nodes", [])))
    edge_count = int(source_counts.get("edges") or len(payload.get("edges", [])))
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>REQL Memory Graph</title>
  <script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
          integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
          crossorigin="anonymous"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      height: 100vh;
      overflow: hidden;
      display: flex;
      background: #0f0f1a;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body, #search-results, #legend-wrap, #node-menu {
      scrollbar-width: thin;
      scrollbar-color: #4E79A7 #111122;
    }
    body::-webkit-scrollbar,
    #search-results::-webkit-scrollbar,
    #legend-wrap::-webkit-scrollbar,
    #node-menu::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }
    body::-webkit-scrollbar-track,
    #search-results::-webkit-scrollbar-track,
    #legend-wrap::-webkit-scrollbar-track,
    #node-menu::-webkit-scrollbar-track {
      background: #111122;
      border-left: 1px solid #242442;
    }
    body::-webkit-scrollbar-thumb,
    #search-results::-webkit-scrollbar-thumb,
    #legend-wrap::-webkit-scrollbar-thumb,
    #node-menu::-webkit-scrollbar-thumb {
      background: linear-gradient(180deg, #4E79A7, #355f8e);
      border: 2px solid #111122;
      border-radius: 999px;
    }
    body::-webkit-scrollbar-thumb:hover,
    #search-results::-webkit-scrollbar-thumb:hover,
    #legend-wrap::-webkit-scrollbar-thumb:hover,
    #node-menu::-webkit-scrollbar-thumb:hover {
      background: linear-gradient(180deg, #6d9ed0, #4E79A7);
    }
    body::-webkit-scrollbar-corner,
    #search-results::-webkit-scrollbar-corner,
    #legend-wrap::-webkit-scrollbar-corner,
    #node-menu::-webkit-scrollbar-corner {
      background: #111122;
    }
    #graph { flex: 1; min-width: 0; background: #0f0f1a; }
    #node-menu {
      position: fixed;
      z-index: 20;
      display: none;
      width: min(360px, calc(100vw - 24px));
      max-height: min(520px, calc(100vh - 24px));
      overflow: auto;
      padding: 12px;
      border: 1px solid #3a3a5e;
      border-radius: 8px;
      background: rgba(18, 18, 34, 0.96);
      color: #dfe5f5;
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.48);
      backdrop-filter: blur(8px);
      font-size: 12px;
      line-height: 1.45;
    }
    #node-menu.open { display: block; }
    .node-menu-title {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding-bottom: 8px;
      margin-bottom: 8px;
      border-bottom: 1px solid #2a2a4e;
    }
    .node-menu-dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 10px; margin-top: 4px; }
    .node-menu-name { font-weight: 700; color: #fff; overflow-wrap: anywhere; }
    .node-menu-type { color: #9aa4c2; font-size: 11px; margin-top: 2px; }
    .node-menu-grid {
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr);
      gap: 5px 10px;
      margin-bottom: 10px;
    }
    .node-menu-key { color: #9aa4c2; font-weight: 700; }
    .node-menu-value { color: #f0f3ff; overflow-wrap: anywhere; }
    .node-menu-section {
      margin-top: 10px;
      padding-top: 8px;
      border-top: 1px solid #2a2a4e;
    }
    .node-menu-section h4 {
      margin-bottom: 6px;
      color: #b9c0d8;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .node-menu-text {
      max-height: 96px;
      overflow: auto;
      color: #cfd3e6;
      background: #0f0f1a;
      border: 1px solid #29294a;
      border-radius: 6px;
      padding: 7px;
      overflow-wrap: anywhere;
    }
    .node-menu-link {
      display: block;
      width: 100%;
      color: #dfe5f5;
      font: inherit;
      text-align: left;
      border-left: 3px solid #4E79A7;
      border-top: 0;
      border-right: 0;
      border-bottom: 0;
      padding: 4px 6px;
      margin: 3px 0;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.03);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      cursor: pointer;
    }
    .node-menu-link:hover,
    .node-menu-link:focus {
      background: #2a2a4e;
      outline: 1px solid #4E79A7;
    }
    #sidebar {
      width: 300px;
      flex: 0 0 300px;
      background: #1a1a2e;
      border-right: 1px solid #2a2a4e;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #search-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
    #search {
      width: 100%;
      background: #0f0f1a;
      border: 1px solid #3a3a5e;
      color: #e0e0e0;
      padding: 8px 10px;
      border-radius: 6px;
      font-size: 13px;
      outline: none;
    }
    #search:focus { border-color: #4E79A7; }
    #search-results {
      max-height: 140px;
      overflow-y: auto;
      padding: 4px 12px;
      border-bottom: 1px solid #2a2a4e;
      display: none;
    }
    .search-item {
      padding: 5px 6px;
      cursor: pointer;
      border-radius: 4px;
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .search-item:hover { background: #2a2a4e; }
    h3 {
      font-size: 13px;
      color: #aaa;
      margin-bottom: 10px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-weight: 700;
    }
    #legend-wrap { flex: 1; overflow-y: auto; padding: 12px; }
    #legend-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding: 4px 0; }
    #legend-controls label {
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      font-size: 12px;
      color: #aaa;
      user-select: none;
    }
    #legend-controls label:hover { color: #e0e0e0; }
    .legend-cb, #select-all-cb {
      appearance: none;
      -webkit-appearance: none;
      width: 14px;
      height: 14px;
      border: 1.5px solid #3a3a5e;
      border-radius: 3px;
      background: #0f0f1a;
      cursor: pointer;
      position: relative;
      flex-shrink: 0;
    }
    .legend-cb:checked, #select-all-cb:checked { background: #4E79A7; border-color: #4E79A7; }
    .legend-cb:checked::after, #select-all-cb:checked::after {
      content: "";
      position: absolute;
      left: 3.5px;
      top: 1px;
      width: 4px;
      height: 7px;
      border: solid #fff;
      border-width: 0 2px 2px 0;
      transform: rotate(45deg);
    }
    #select-all-cb:indeterminate { background: #4E79A7; border-color: #4E79A7; }
    #select-all-cb:indeterminate::after {
      content: "";
      position: absolute;
      left: 2px;
      top: 5px;
      width: 8px;
      height: 2px;
      background: #fff;
      border: none;
      transform: none;
    }
    .legend-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 0;
      cursor: pointer;
      border-radius: 4px;
      font-size: 12px;
    }
    .legend-item:hover { background: #2a2a4e; padding-left: 4px; }
    .legend-item.dimmed { opacity: 0.35; }
    .legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
    .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .legend-count { color: #777; font-size: 11px; font-variant-numeric: tabular-nums; }
    #stats {
      padding: 10px 14px;
      border-top: 1px solid #2a2a4e;
      font-size: 11px;
      color: #666;
    }
    @media (max-width: 860px) {
      body { flex-direction: column; overflow: auto; height: auto; min-height: 100vh; }
      #graph { min-height: 62vh; flex: none; }
      #sidebar { width: 100%; flex: none; max-height: none; border-right: 0; border-bottom: 1px solid #2a2a4e; }
    }
  </style>
</head>
<body>
  <div id="sidebar">
    <div id="search-wrap">
      <input id="search" type="search" placeholder="Search nodes..." autocomplete="off">
    </div>
    <div id="search-results"></div>
    <div id="legend-wrap">
      <h3>Clusters</h3>
      <div id="legend-controls">
        <label><input type="checkbox" id="select-all-cb" checked>Select All</label>
      </div>
      <div id="legend"></div>
    </div>
    <div id="stats">__NODE_COUNT__ nodes &middot; __EDGE_COUNT__ edges &middot; <span id="visible-stats">0 visible</span></div>
  </div>
  <div id="graph" role="img" aria-label="REQL memory graph visualization"></div>
  <div id="node-menu" role="dialog" aria-label="Node context menu"></div>
  <script id="graph-data" type="application/json">__GRAPH_DATA__</script>
  <script>
(() => {
  const payload = JSON.parse(document.getElementById("graph-data").textContent);
  const container = document.getElementById("graph");
  const search = document.getElementById("search");
  const searchResults = document.getElementById("search-results");
  const legend = document.getElementById("legend");
  const selectAll = document.getElementById("select-all-cb");
  const visibleStats = document.getElementById("visible-stats");
  const nodeMenu = document.getElementById("node-menu");
  const palette = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC949", "#AF7AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
    "#86BCB6", "#D37295", "#A0CBE8", "#FFBE7D", "#8CD17D", "#B6992D", "#B07AA1", "#FABFD2", "#D4A6C8", "#9D7660"
  ];
  const typeFallback = new Map([
    ["Module", "code"], ["Class", "code"], ["Interface", "code"], ["Function", "code"], ["Method", "code"],
    ["Variable", "code"], ["Import", "code"], ["Dependency", "code"],
    ["Endpoint", "code"], ["Schema", "code"], ["StaticAnalysisFinding", "code"], ["Comment", "rationale"], ["Docstring", "rationale"],
    ["SourceArtifact", "document"], ["SourceFragment", "document"], ["File", "document"], ["Directory", "document"],
    ["Project", "document"], ["Concept", "concept"]
  ]);
  if (!window.vis || !window.vis.Network) {
    container.innerHTML = '<div style="display:flex;height:100%;align-items:center;justify-content:center;color:#aaa;font-size:13px;">vis-network failed to load. Check your connection to unpkg.com.</div>';
    return;
  }
  const nodesRaw = Array.isArray(payload.nodes) ? payload.nodes : [];
  const edgesRaw = Array.isArray(payload.edges) ? payload.edges : [];
  const communityNodes = new Map(nodesRaw.filter((node) => node.type === "Community").map((node) => [node.id, node]));
  const memberships = new Map();
  for (const edge of edgesRaw) {
    if (edge.type === "BELONGS_TO_COMMUNITY" && communityNodes.has(edge.to_id)) {
      memberships.set(edge.from_id, edge.to_id);
    }
  }
  const graphNodes = nodesRaw
    .filter((node) => node.type !== "Community")
    .map((node, index) => {
      const props = node.properties || {};
      const community = communityFor(node);
      return {
        ...node,
        index,
        degree: 0,
        community: community.id,
        community_name: community.label,
        file_type: fileTypeFor(node),
        source_file: props.relative_path || props.source_file || props.path || ""
      };
    });
  const byId = new Map(graphNodes.map((node) => [node.id, node]));
  const graphEdges = edgesRaw.filter((edge) => byId.has(edge.from_id) && byId.has(edge.to_id) && edge.type !== "BELONGS_TO_COMMUNITY" && edge.type !== "BRIDGES_COMMUNITY");
  for (const edge of graphEdges) {
    byId.get(edge.from_id).degree += 1;
    byId.get(edge.to_id).degree += 1;
  }
  const renderableNodes = graphNodes;
  const renderableIds = new Set(renderableNodes.map((node) => node.id));
  const renderableEdges = graphEdges.filter((edge) => renderableIds.has(edge.from_id) && renderableIds.has(edge.to_id));
  const communities = buildCommunities();
  const colorByCommunity = new Map(communities.map((item, index) => [item.id, palette[index % palette.length]]));
  assignStaticLayout(renderableNodes);
  let selectedCommunities = new Set(communities.map((item) => item.id));
  let activeNodes = [];
  let activeEdges = [];
  let nodeItems = [];
  let edgeItems = [];
  let network = null;
  let nodesDS = new vis.DataSet();
  let edgesDS = new vis.DataSet();
  let selectedNodeId = null;
  let hoveredNodeId = null;
  let searchTimer = null;

  function labelFor(node) {
    return node.label || node.text || node.canonical_key || node.id;
  }

  function fileTypeFor(node) {
    const props = node.properties || {};
    if (props.file_type) return String(props.file_type);
    if (typeFallback.has(node.type)) return typeFallback.get(node.type);
    if (props.is_technical) return "code";
    if (props.is_semantic) return "concept";
    return String(node.type || "node").toLowerCase();
  }

  function communityFor(node) {
    const persistedId = memberships.get(node.id);
    if (persistedId && communityNodes.has(persistedId)) {
      return { id: persistedId, label: labelFor(communityNodes.get(persistedId)) };
    }
    const props = node.properties || {};
    const path = String(props.relative_path || props.source_file || props.path || "");
    const pathGroup = pathCommunity(path);
    if (pathGroup) return pathGroup;
    const qualified = String(props.qualified_name || props.module || props.name || "");
    const moduleGroup = moduleCommunity(qualified);
    if (moduleGroup) return moduleGroup;
    const family = fileTypeFor(node);
    return { id: `type:${family}`, label: family.charAt(0).toUpperCase() + family.slice(1) };
  }

  function pathCommunity(path) {
    const clean = path.replaceAll("\\\\", "/").replace(/^\\.\\//, "");
    if (!clean) return null;
    const parts = clean.split("/").filter(Boolean);
    if (!parts.length) return null;
    if (parts[0] === "src" && parts.length >= 3) {
      return { id: `path:${parts.slice(0, 3).join("/")}`, label: parts.slice(0, 3).join("/") };
    }
    if (parts[0] === "tests") return { id: "path:tests", label: "tests" };
    if (parts[0] === "docs") return { id: "path:docs", label: "docs" };
    if (parts.length >= 2 && !parts[parts.length - 1].includes(".")) {
      return { id: `path:${parts.slice(0, 2).join("/")}`, label: parts.slice(0, 2).join("/") };
    }
    return { id: `path:${parts[0]}`, label: parts[0] };
  }

  function moduleCommunity(value) {
    if (!value || value.length < 2) return null;
    const clean = value.replaceAll(":", ".").replaceAll("/", ".");
    const parts = clean.split(".").filter(Boolean);
    if (parts.length >= 2) return { id: `module:${parts.slice(0, 2).join(".")}`, label: parts.slice(0, 2).join(".") };
    return null;
  }

  function displayEdgeType(type) {
    return String(type || "related_to").toLowerCase();
  }

  function colorForCommunity(community) {
    const base = colorByCommunity.get(community) || "#777";
    return {
      background: base,
      border: base,
      highlight: { background: "#ffffff", border: base },
      hover: { background: "#ffffff", border: base }
    };
  }

  function visNodeFor(node) {
    const size = Math.max(2.2, Math.min(9, 2.1 + Math.sqrt(node.degree || 0) * 0.75));
    return {
      id: node.id,
      label: "",
      x: Math.round(node.x || 0),
      y: Math.round(node.y || 0),
      fixed: { x: true, y: true },
      physics: false,
      color: colorForCommunity(node.community),
      size,
      font: {
        size: node.degree >= 18 ? 10 : 0,
        color: "#ffffff",
        strokeWidth: 4,
        strokeColor: "#0f0f1a"
      },
      _community: node.community,
      _community_name: node.community_name,
      _source_file: node.source_file,
      _file_type: node.file_type,
      _degree: node.degree
    };
  }

  function visEdgeFor(edge, index) {
    const source = byId.get(edge.from_id);
    const edgeColor = source ? (colorByCommunity.get(source.community) || "#8b91a8") : "#8b91a8";
    return {
      id: index,
      from: edge.from_id,
      to: edge.to_id,
      label: "",
      title: `${escapeHtml(displayEdgeType(edge.type))} [${escapeHtml(edge.origin || "deterministic")}]`,
      width: Math.max(0.35, Math.min(1.6, Number(edge.weight || 1) * 0.7)),
      color: { color: edgeColor, highlight: "#cfd3e6", hover: "#cfd3e6", opacity: 0.24 },
      arrows: { to: { enabled: true, scaleFactor: 0.45, type: "arrow" } },
      smooth: false,
      _type: displayEdgeType(edge.type),
      _origin: edge.origin || "deterministic",
      _direction: "out"
    };
  }

  function edgeDirectionFor(edge, nodeId) {
    if (edge.from_id === nodeId) {
      return { kind: "outgoing", symbol: "->", otherId: edge.to_id };
    }
    if (edge.to_id === nodeId) {
      return { kind: "incoming", symbol: "<-", otherId: edge.from_id };
    }
    return { kind: "adjacent", symbol: "--", otherId: edge.from_id };
  }

  function assignStaticLayout(nodes) {
    const groups = new Map();
    for (const node of nodes) {
      if (!groups.has(node.community)) groups.set(node.community, []);
      groups.get(node.community).push(node);
    }
    const orderedGroups = Array.from(groups.entries())
      .map(([community, items]) => ({
        community,
        nodes: items.sort((a, b) => b.degree - a.degree || labelFor(a).localeCompare(labelFor(b))),
        radius: groupRadiusFor(items.length),
        x: 0,
        y: 0
      }))
      .sort((a, b) => b.nodes.length - a.nodes.length);
    const groupLookup = new Map(orderedGroups.map((group) => [group.community, group]));
    const groupLinks = buildCommunityLinks(groupLookup);
    const cloudRadius = Math.max(480, Math.min(2300, Math.sqrt(Math.max(nodes.length, 1)) * 20 + Math.sqrt(Math.max(orderedGroups.length, 1)) * 100));
    orderedGroups.forEach((group, groupIndex) => {
      const angle = groupIndex * 2.399963229728653 - Math.PI / 2;
      const radius = orderedGroups.length <= 1 ? 0 : cloudRadius * Math.sqrt((groupIndex + 0.45) / orderedGroups.length);
      group.x = Math.cos(angle) * radius * 1.16;
      group.y = Math.sin(angle) * radius * 0.82;
    });
    relaxGroupCenters(orderedGroups, groupLinks);
    orderedGroups.forEach((group, groupIndex) => {
      group.nodes.forEach((node, index) => {
        const theta = index * 2.399963229728653 + groupIndex * 0.73;
        const radius = group.radius * Math.sqrt((index + 0.5) / Math.max(group.nodes.length, 1));
        const wobble = deterministicJitter(index, groupIndex) * 0.14;
        node.x = group.x + Math.cos(theta + wobble) * radius;
        node.y = group.y + Math.sin(theta - wobble) * radius * 0.9;
      });
    });
  }

  function groupRadiusFor(size) {
    return Math.max(62, Math.min(500, 26 + Math.sqrt(size || 1) * 15));
  }

  function buildCommunityLinks(groupLookup) {
    const links = new Map();
    for (const edge of graphEdges) {
      const source = byId.get(edge.from_id);
      const target = byId.get(edge.to_id);
      if (!source || !target || source.community === target.community) continue;
      if (!groupLookup.has(source.community) || !groupLookup.has(target.community)) continue;
      const key = source.community < target.community ? `${source.community}\t${target.community}` : `${target.community}\t${source.community}`;
      links.set(key, (links.get(key) || 0) + Math.max(0.25, Math.min(2.5, Number(edge.weight || 1))));
    }
    return Array.from(links.entries()).map(([key, weight]) => {
      const [a, b] = key.split("\t");
      return { a: groupLookup.get(a), b: groupLookup.get(b), weight };
    }).filter((link) => link.a && link.b);
  }

  function relaxGroupCenters(groups, links) {
    if (groups.length <= 1) return;
    for (let iteration = 0; iteration < 96; iteration += 1) {
      for (let i = 0; i < groups.length; i += 1) {
        const a = groups[i];
        for (let j = i + 1; j < groups.length; j += 1) {
          const b = groups[j];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const distance = Math.max(0.01, Math.hypot(dx, dy));
          const minimum = (a.radius + b.radius) * 1.05 + 70;
          if (distance >= minimum) continue;
          const push = (minimum - distance) * 0.035;
          const fx = (dx / distance) * push;
          const fy = (dy / distance) * push;
          a.x -= fx;
          a.y -= fy;
          b.x += fx;
          b.y += fy;
        }
      }
      for (const link of links) {
        const a = link.a;
        const b = link.b;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const target = (a.radius + b.radius) * 0.78 + 220;
        const pull = (distance - target) * Math.min(0.005, 0.0014 * Math.sqrt(link.weight));
        const fx = (dx / distance) * pull;
        const fy = (dy / distance) * pull;
        a.x += fx;
        a.y += fy;
        b.x -= fx;
        b.y -= fy;
      }
    }
  }

  function deterministicJitter(index, groupIndex) {
    return Math.sin((index + 1) * 12.9898 + (groupIndex + 1) * 78.233) * 0.5;
  }

  function searchText(node) {
    return [node.id, node.type, node.label, node.text, node.canonical_key, node.community_name, node.file_type, node.source_file, JSON.stringify(node.properties || {})]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function buildCommunities() {
    const counts = new Map();
    const labels = new Map();
    for (const node of renderableNodes) {
      counts.set(node.community, (counts.get(node.community) || 0) + 1);
      labels.set(node.community, node.community_name);
    }
    return Array.from(counts.entries())
      .map(([id, count]) => ({ id, count, label: labels.get(id) || id }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  }

  function renderLegend() {
    legend.innerHTML = "";
    for (const item of communities) {
      const row = document.createElement("label");
      row.className = "legend-item";
      if (!selectedCommunities.has(item.id)) row.classList.add("dimmed");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.className = "legend-cb";
      input.checked = selectedCommunities.has(item.id);
      input.addEventListener("change", () => {
        input.checked ? selectedCommunities.add(item.id) : selectedCommunities.delete(item.id);
        syncSelectAll();
        renderLegend();
        updateGraph();
      });
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = colorByCommunity.get(item.id) || "#777";
      const name = document.createElement("span");
      name.className = "legend-label";
      name.textContent = item.label;
      const count = document.createElement("span");
      count.className = "legend-count";
      count.textContent = item.count;
      row.append(input, dot, name, count);
      legend.appendChild(row);
    }
  }

  function syncSelectAll() {
    selectAll.checked = selectedCommunities.size === communities.length;
    selectAll.indeterminate = selectedCommunities.size > 0 && selectedCommunities.size < communities.length;
  }

  function applyFilters() {
    const query = search.value.trim().toLowerCase();
    activeNodes = renderableNodes.filter((node) => selectedCommunities.has(node.community) && (!query || searchText(node).includes(query)));
    const activeIds = new Set(activeNodes.map((node) => node.id));
    activeEdges = renderableEdges.filter((edge) => activeIds.has(edge.from_id) && activeIds.has(edge.to_id));
    nodeItems = activeNodes.map(visNodeFor);
    edgeItems = activeEdges.map(visEdgeFor);
    nodesDS.clear();
    edgesDS.clear();
    nodesDS.add(nodeItems);
    edgesDS.add(edgeItems);
    if (network) {
      network.setData({ nodes: nodesDS, edges: edgesDS });
    }
    hideNodeMenu();
    renderSearchResults(query);
    visibleStats.textContent = `${activeNodes.length} visible`;
  }

  function renderSearchResults(query) {
    if (!query) {
      searchResults.style.display = "none";
      searchResults.innerHTML = "";
      return;
    }
    const matches = activeNodes.slice(0, 20);
    searchResults.innerHTML = "";
    for (const node of matches) {
      const item = document.createElement("div");
      item.className = "search-item";
      item.textContent = `${labelFor(node)} - ${node.type}`;
      item.addEventListener("click", () => {
        selectNode(node.id, true);
        showNodeMenu(node.id, null);
      });
      searchResults.appendChild(item);
    }
    searchResults.style.display = matches.length ? "block" : "none";
  }

  function selectNode(nodeId, focus) {
    selectedNodeId = nodeId;
    const node = byId.get(nodeId);
    if (!node) return;
    if (focus) {
      network.focus(nodeId, { scale: 1.35, animation: false });
      network.selectNodes([nodeId]);
    }
  }

  function showNodeMenu(nodeId, pointer) {
    const node = byId.get(nodeId);
    if (!node) return;
    const adjacent = graphEdges.filter((edge) => edge.from_id === nodeId || edge.to_id === nodeId);
    const outgoingCount = adjacent.filter((edge) => edge.from_id === nodeId).length;
    const incomingCount = adjacent.filter((edge) => edge.to_id === nodeId).length;
    nodeMenu.innerHTML = "";

    const title = document.createElement("div");
    title.className = "node-menu-title";
    const dot = document.createElement("span");
    dot.className = "node-menu-dot";
    dot.style.background = colorByCommunity.get(node.community) || "#777";
    const titleText = document.createElement("div");
    const name = document.createElement("div");
    name.className = "node-menu-name";
    name.textContent = labelFor(node);
    const type = document.createElement("div");
    type.className = "node-menu-type";
    type.textContent = `${node.type || "Node"} - ${node.community_name || "cluster"}`;
    titleText.append(name, type);
    title.append(dot, titleText);
    nodeMenu.appendChild(title);

    const fields = [
      ["Label", labelFor(node)],
      ["Type", node.type || ""],
      ["Community", node.community_name || ""],
      ["ID", node.id],
      ["Source", node.source_file || ""],
      ["File Type", node.file_type || ""],
      ["Degree", String(node.degree || 0)],
      ["Outgoing", String(outgoingCount)],
      ["Incoming", String(incomingCount)]
    ].filter(([, value]) => String(value || "").length > 0);
    const grid = document.createElement("div");
    grid.className = "node-menu-grid";
    for (const [key, value] of fields) {
      const keyEl = document.createElement("div");
      keyEl.className = "node-menu-key";
      keyEl.textContent = key;
      const valueEl = document.createElement("div");
      valueEl.className = "node-menu-value";
      valueEl.textContent = value;
      grid.append(keyEl, valueEl);
    }
    nodeMenu.appendChild(grid);

    const text = node.text || node.canonical_key || "";
    if (text && text !== labelFor(node)) {
      const section = menuSection("Text");
      const textEl = document.createElement("div");
      textEl.className = "node-menu-text";
      textEl.textContent = text;
      section.appendChild(textEl);
      nodeMenu.appendChild(section);
    }

    const props = compactProperties(node.properties || {});
    if (props.length) {
      const section = menuSection("Properties");
      const propGrid = document.createElement("div");
      propGrid.className = "node-menu-grid";
      for (const [key, value] of props) {
        const keyEl = document.createElement("div");
        keyEl.className = "node-menu-key";
        keyEl.textContent = key;
        const valueEl = document.createElement("div");
        valueEl.className = "node-menu-value";
        valueEl.textContent = value;
        propGrid.append(keyEl, valueEl);
      }
      section.appendChild(propGrid);
      nodeMenu.appendChild(section);
    }

    if (adjacent.length) {
      const section = menuSection("Links");
      for (const edge of adjacent.slice(0, 40)) {
        const direction = edgeDirectionFor(edge, nodeId);
        const other = byId.get(direction.otherId);
        if (!other) continue;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "node-menu-link";
        item.style.borderLeftColor = colorByCommunity.get(other.community) || "#4E79A7";
        item.textContent = `${direction.symbol} ${displayEdgeType(edge.type)} ${labelFor(other)}`;
        item.title = `${direction.kind}: ${edge.from_id} -> ${edge.to_id}`;
        item.addEventListener("click", () => {
          selectNode(other.id, true);
          showNodeMenu(other.id, null);
        });
        section.appendChild(item);
      }
      nodeMenu.appendChild(section);
    }

    nodeMenu.classList.add("open");
    positionNodeMenu(pointer);
  }

  function menuSection(title) {
    const section = document.createElement("div");
    section.className = "node-menu-section";
    const heading = document.createElement("h4");
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  function compactProperties(properties) {
    return Object.entries(properties)
      .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
      .slice(0, 8)
      .map(([key, value]) => [key, String(value)]);
  }

  function positionNodeMenu(pointer) {
    const rect = container.getBoundingClientRect();
    const x = rect.left + (pointer?.x ?? rect.width / 2) + 14;
    const y = rect.top + (pointer?.y ?? rect.height / 2) + 14;
    nodeMenu.style.left = "0px";
    nodeMenu.style.top = "0px";
    const menuRect = nodeMenu.getBoundingClientRect();
    const left = Math.min(Math.max(12, x), window.innerWidth - menuRect.width - 12);
    const top = Math.min(Math.max(12, y), window.innerHeight - menuRect.height - 12);
    nodeMenu.style.left = `${left}px`;
    nodeMenu.style.top = `${top}px`;
  }

  function hideNodeMenu() {
    nodeMenu.classList.remove("open");
  }

  function escapeHtml(value) {
    const entities = new Map([
      ["&", "&amp;"],
      ["<", "&lt;"],
      [">", "&gt;"],
      [String.fromCharCode(34), "&quot;"],
      ["'", "&#39;"]
    ]);
    return String(value ?? "").replace(/[&<>"']/g, (char) => entities.get(char) || char);
  }

  function updateGraph() {
    applyFilters();
  }

  function initNetwork() {
    network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, {
      autoResize: true,
      nodes: {
        shape: "dot",
        borderWidth: 0,
        borderWidthSelected: 2,
        scaling: { min: 2, max: 10 }
      },
      edges: {
        selectionWidth: 1,
        hoverWidth: 1,
        smooth: false
      },
      interaction: {
        hover: true,
        tooltipDelay: 120,
        hideEdgesOnDrag: true,
        hideEdgesOnZoom: true,
        multiselect: false,
        navigationButtons: false
      },
      physics: { enabled: false },
      layout: { improvedLayout: false }
    });
    network.on("hoverNode", (params) => {
      hoveredNodeId = params.node;
      container.style.cursor = "pointer";
    });
    network.on("blurNode", () => {
      hoveredNodeId = null;
      container.style.cursor = "default";
    });
    network.on("click", (params) => {
      if (params.nodes.length > 0) {
        selectNode(params.nodes[0], false);
        showNodeMenu(params.nodes[0], params.pointer.DOM);
      } else if (hoveredNodeId === null) {
        selectedNodeId = null;
        hideNodeMenu();
      }
    });
    network.fit({ animation: false });
  }

  selectAll.addEventListener("change", () => {
    selectedCommunities = selectAll.checked ? new Set(communities.map((item) => item.id)) : new Set();
    syncSelectAll();
    renderLegend();
    updateGraph();
  });
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(updateGraph, 80);
  });
  renderLegend();
  syncSelectAll();
  applyFilters();
  initNetwork();
})();
  </script>
</body>
</html>
"""
    return (
        template.replace("__GRAPH_DATA__", data)
        .replace("__NODE_COUNT__", str(node_count))
        .replace("__EDGE_COUNT__", str(edge_count))
    )


def _safe_json(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _safe_graph_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    nodes: list[dict[str, Any]] = []
    for raw in payload.get("nodes", []) or []:
        if not isinstance(raw, dict):
            continue
        node = dict(raw)
        if "label" in node:
            node["label"] = sanitize_label(node.get("label"))
        if "type" in node:
            node["type"] = sanitize_label(node.get("type"), max_chars=80)
        nodes.append(node)
    edges: list[dict[str, Any]] = []
    for raw in payload.get("edges", []) or []:
        if not isinstance(raw, dict):
            continue
        edge = dict(raw)
        if "type" in edge:
            edge["type"] = sanitize_label(edge.get("type"), max_chars=80)
        edges.append(edge)
    safe["nodes"] = nodes
    safe["edges"] = edges
    return safe


_NODE_PROPERTY_KEYS = {
    "alias",
    "artifact_type",
    "confidence",
    "end_line",
    "file_type",
    "finding_type",
    "handler",
    "is_semantic",
    "is_technical",
    "language",
    "line_end",
    "line_start",
    "method",
    "mode",
    "name",
    "path",
    "project_id",
    "qualified_name",
    "relative_path",
    "route",
    "severity",
    "source_file",
    "start_line",
    "status",
    "symbol_name",
}
_NODE_TEXT_MAX_CHARS = 420
_LABEL_MAX_CHARS = 180
_PROPERTY_VALUE_MAX_CHARS = 180


def _visual_graph_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the full graph payload with compact fields for the HTML viewer."""

    nodes = [node for node in payload.get("nodes", []) or [] if isinstance(node, dict)]
    edges = [edge for edge in payload.get("edges", []) or [] if isinstance(edge, dict)]
    return {
        "format": "reql-memory-visual-export-v1",
        "source_format": payload.get("format"),
        "created_at": payload.get("created_at"),
        "source_counts": {
            "nodes": len([node for node in payload.get("nodes", []) or [] if isinstance(node, dict)]),
            "edges": len([edge for edge in payload.get("edges", []) or [] if isinstance(edge, dict)]),
        },
        "visual_limits": {
            "nodes": None,
            "edges": None,
        },
        "nodes": [_visual_node(node) for node in nodes],
        "edges": [_visual_edge(edge) for edge in edges],
    }


def _visual_node(node: dict[str, Any]) -> dict[str, Any]:
    properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    compact = {
        "id": _compact_text(node.get("id"), max_chars=220),
        "type": _compact_text(node.get("type"), max_chars=80),
        "label": _compact_text(node.get("label") or properties.get("name") or properties.get("qualified_name") or node.get("id"), max_chars=_LABEL_MAX_CHARS),
        "text": _compact_text(node.get("text"), max_chars=_NODE_TEXT_MAX_CHARS),
        "canonical_key": _compact_text(node.get("canonical_key"), max_chars=_LABEL_MAX_CHARS),
        "status": _compact_text(node.get("status"), max_chars=40),
        "salience": _round_float(node.get("salience")),
        "confidence": _round_float(node.get("confidence")),
        "activation": _round_float(node.get("activation")),
        "properties": _visual_properties(properties),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", {})}


def _visual_edge(edge: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "id": _compact_text(edge.get("id"), max_chars=220),
        "from_id": _compact_text(edge.get("from_id"), max_chars=220),
        "to_id": _compact_text(edge.get("to_id"), max_chars=220),
        "type": _compact_text(edge.get("type"), max_chars=80),
        "weight": _round_float(edge.get("weight")),
        "confidence": _round_float(edge.get("confidence")),
        "origin": _compact_text(edge.get("origin"), max_chars=40),
    }
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _visual_properties(properties: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in sorted(_NODE_PROPERTY_KEYS):
        if key not in properties:
            continue
        value = _compact_property_value(properties.get(key))
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def _compact_property_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return _round_float(value)
    if isinstance(value, list):
        return [_compact_property_value(item) for item in value[:8]]
    if isinstance(value, tuple):
        return [_compact_property_value(item) for item in list(value)[:8]]
    if isinstance(value, dict):
        return {
            _compact_text(key, max_chars=80): _compact_property_value(item)
            for key, item in list(value.items())[:8]
            if _compact_text(key, max_chars=80)
        }
    return _compact_text(value, max_chars=_PROPERTY_VALUE_MAX_CHARS)


def _compact_text(value: Any, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _round_float(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 4)
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None
