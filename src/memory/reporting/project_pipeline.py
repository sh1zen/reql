"""Mermaid and interactive HTML renderers for project pipelines."""
from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
import re

from ..pipeline import ProjectPipeline


def write_pipeline_mermaid(pipeline: ProjectPipeline, output_path: str | Path) -> Path:
    """Atomically write Mermaid source for a project pipeline."""

    return _write_text_atomic(Path(output_path), render_pipeline_mermaid(pipeline))


def write_pipeline_html(pipeline: ProjectPipeline, output_path: str | Path) -> Path:
    """Atomically write an interactive HTML project-pipeline view."""

    return _write_text_atomic(Path(output_path), render_pipeline_html(pipeline))


def render_pipeline_mermaid(pipeline: ProjectPipeline) -> str:
    """Render a deterministic left-to-right Mermaid flowchart."""

    project_name = str(pipeline.project.get("name") or "Project")
    lines = [
        f"%% REQL project pipeline: {_comment_text(project_name)}",
        f"%% Schema: {pipeline.schema_version}; deterministic; LLM required: false",
    ]
    for component in pipeline.components:
        symbols = "; ".join(
            f"{symbol.label} @ {symbol.location or '-'}"
            for symbol in component.symbols
        )
        lines.append(f"%% component {component.id}: {_comment_text(symbols)}")
    lines.extend(["flowchart LR"])

    if not pipeline.workflows:
        lines.extend(
            [
                '  empty["No deterministic project pipeline detected"]',
                "  classDef neutral fill:#374151,stroke:#9ca3af,color:#f9fafb;",
                "  class empty neutral;",
            ]
        )
        return "\n".join(lines) + "\n"

    workflow_ids = {item.id: _mermaid_id("wf", item.id) for item in pipeline.workflows}
    component_ids = {item.id: _mermaid_id("cmp", item.id) for item in pipeline.components}
    outcome_ids = {item.id: _mermaid_id("out", item.id) for item in pipeline.outcomes}

    lines.append('  subgraph entrypoints["Entrypoints"]')
    for workflow in pipeline.workflows:
        qualifier = "inferred" if workflow.inferred else "detected"
        label = _mermaid_label(f"{workflow.name}\n{qualifier}")
        lines.append(f'    {workflow_ids[workflow.id]}(["{label}"])')
    lines.append("  end")

    lines.append('  subgraph components["Shared components"]')
    for component in pipeline.components:
        cycle = " ↻" if component.cyclic else ""
        label = _mermaid_label(f"{component.name}{cycle}\n{component.layer}")
        lines.append(f'    {component_ids[component.id]}["{label}"]')
    lines.append("  end")

    if pipeline.outcomes:
        lines.append('  subgraph outcomes["Observed outcomes"]')
        for outcome in pipeline.outcomes:
            prefix = "Observed end" if outcome.observed_terminal else outcome.kind.title()
            label = _mermaid_label(f"{prefix}: {outcome.label}")
            lines.append(f'    {outcome_ids[outcome.id]}(["{label}"])')
        lines.append("  end")

    link_index = 0
    cyclic_link_indexes: list[int] = []
    for workflow in pipeline.workflows:
        target = component_ids.get(workflow.trigger_component_id)
        if target:
            lines.append(f"  {workflow_ids[workflow.id]} --> {target}")
            link_index += 1
    for edge in pipeline.edges:
        source = component_ids.get(edge.from_component_id)
        target = component_ids.get(edge.to_component_id)
        if not source or not target:
            continue
        relation = _mermaid_edge_label(" / ".join(edge.relation_types))
        arrow = "-.->" if edge.cyclic else "-->"
        lines.append(f"  {source} {arrow}|{relation}| {target}")
        if edge.cyclic:
            cyclic_link_indexes.append(link_index)
        link_index += 1
    for outcome in pipeline.outcomes:
        source = component_ids.get(outcome.component_id)
        target = outcome_ids.get(outcome.id)
        if source and target:
            lines.append(f"  {source} --> {target}")
            link_index += 1

    lines.extend(
        [
            "  classDef entrypoint fill:#6d28d9,stroke:#c4b5fd,color:#ffffff;",
            "  classDef interface fill:#1d4ed8,stroke:#93c5fd,color:#ffffff;",
            "  classDef application fill:#0f766e,stroke:#5eead4,color:#ffffff;",
            "  classDef domain fill:#a16207,stroke:#fde68a,color:#ffffff;",
            "  classDef core fill:#374151,stroke:#d1d5db,color:#ffffff;",
            "  classDef infrastructure fill:#9f1239,stroke:#fda4af,color:#ffffff;",
            "  classDef outcome fill:#166534,stroke:#86efac,color:#ffffff;",
        ]
    )
    if workflow_ids:
        lines.append(f"  class {','.join(workflow_ids.values())} entrypoint;")
    for layer in ("interface", "application", "domain", "core", "infrastructure"):
        ids = [component_ids[item.id] for item in pipeline.components if item.layer == layer]
        if ids:
            lines.append(f"  class {','.join(ids)} {layer};")
    if outcome_ids:
        lines.append(f"  class {','.join(outcome_ids.values())} outcome;")
    if cyclic_link_indexes:
        lines.append(
            f"  linkStyle {','.join(str(index) for index in cyclic_link_indexes)} "
            "stroke:#f59e0b,stroke-width:2px,stroke-dasharray:6 4;"
        )
    return "\n".join(lines) + "\n"


def render_pipeline_html(pipeline: ProjectPipeline) -> str:
    """Render an interactive browser view with an embedded pipeline payload."""

    payload = _safe_json(pipeline.to_dict())
    project_name = escape(str(pipeline.project.get("name") or "Project"), quote=True)
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>REQL Project Pipeline — __PROJECT_NAME__</title>
  <script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
          integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
          crossorigin="anonymous"></script>
  <style>
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; }
    body { display: grid; grid-template-columns: 320px minmax(0, 1fr) 360px; overflow: hidden;
      background: #0b1020; color: #e5e7eb; font: 13px/1.45 Inter, ui-sans-serif, system-ui, sans-serif; }
    aside { min-width: 0; background: #111827; overflow: auto; border-color: #293548; }
    #controls { border-right: 1px solid #293548; }
    #details { border-left: 1px solid #293548; }
    .panel { padding: 16px; border-bottom: 1px solid #293548; }
    h1 { margin: 0 0 5px; font-size: 18px; color: #fff; }
    h2 { margin: 0 0 10px; font-size: 12px; color: #a5b4fc; text-transform: uppercase; letter-spacing: .08em; }
    p { margin: 5px 0; color: #aab4c5; }
    input, select, button { width: 100%; border: 1px solid #39475d; border-radius: 7px; background: #0b1220;
      color: #e5e7eb; padding: 8px 10px; font: inherit; }
    button { cursor: pointer; background: #24324a; }
    button:hover, button:focus { border-color: #818cf8; outline: none; }
    .button-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }
    .check { display: flex; align-items: center; gap: 8px; margin: 7px 0; color: #cbd5e1; }
    .check input { width: auto; accent-color: #818cf8; }
    .legend-dot { width: 10px; height: 10px; flex: 0 0 10px; border-radius: 50%; }
    #graph-wrap { position: relative; min-width: 0; height: 100vh; overflow: hidden;
      background-color: #0b1020;
      background-image: linear-gradient(rgba(148,163,184,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148,163,184,.035) 1px, transparent 1px),
        radial-gradient(circle at 50% 40%, #17213a, #0b1020 62%);
      background-size: 36px 36px, 36px 36px, 100% 100%; }
    #graph { width: 100%; height: 100%; }
    #graph canvas { cursor: grab; }
    #graph canvas:active { cursor: grabbing; }
    #graph .vis-navigation .vis-button { width: 36px !important; height: 36px !important;
      border: 1px solid #3b4a61; border-radius: 10px; background-color: rgba(17,24,39,.94) !important;
      background-image: none !important; box-shadow: 0 5px 16px rgba(0,0,0,.28); color: #dbeafe; }
    #graph .vis-navigation .vis-button:hover { border-color: #818cf8; background-color: #202c42 !important; }
    #graph .vis-navigation .vis-button::after { position: absolute; inset: 0; display: grid; place-items: center;
      font: 500 20px/1 ui-sans-serif, system-ui, sans-serif; }
    #graph .vis-navigation .vis-up::after { content: '↑'; }
    #graph .vis-navigation .vis-down::after { content: '↓'; }
    #graph .vis-navigation .vis-left::after { content: '←'; }
    #graph .vis-navigation .vis-right::after { content: '→'; }
    #graph .vis-navigation .vis-zoomIn::after { content: '+'; font-size: 24px; }
    #graph .vis-navigation .vis-zoomOut::after { content: '−'; font-size: 24px; }
    #graph .vis-navigation .vis-zoomExtends::after { content: '⌗'; font-size: 18px; }
    #empty { position: absolute; inset: 0; z-index: 5; display: none; place-items: center; color: #9ca3af;
      padding: 24px; text-align: center; pointer-events: none; background: rgba(11,16,32,.82); }
    #search-results { display: none; margin-top: 6px; border: 1px solid #39475d; border-radius: 7px; overflow: hidden; }
    .search-result { border: 0; border-radius: 0; text-align: left; background: #111a2c; }
    .search-result + .search-result { border-top: 1px solid #293548; }
    .warning { padding: 8px; margin: 7px 0; border-left: 3px solid #f59e0b; background: #271c0d; color: #fcd34d; }
    .detail-title { color: #fff; font-size: 17px; font-weight: 700; overflow-wrap: anywhere; }
    .badge { display: inline-block; margin: 4px 5px 4px 0; padding: 2px 7px; border-radius: 999px;
      background: #25314a; color: #c7d2fe; font-size: 11px; }
    .row { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 8px; padding: 5px 0; }
    .key { color: #8fa0b8; font-weight: 600; }
    .value { color: #e5e7eb; overflow-wrap: anywhere; }
    .symbol { padding: 7px 0; border-top: 1px solid #293548; }
    .symbol:first-child { border-top: 0; }
    .symbol-name { color: #fff; font-weight: 600; }
    .symbol-location { color: #93c5fd; font-size: 12px; overflow-wrap: anywhere; }
    @media (max-width: 1050px) { body { grid-template-columns: 280px minmax(0,1fr); } #details { display: none; } }
    @media (max-width: 720px) { body { grid-template-columns: 1fr; grid-template-rows: auto minmax(0,1fr); }
      #controls { max-height: 42vh; } #graph-wrap { height: 58vh; } }
  </style>
</head>
<body>
  <aside id="controls">
    <section class="panel"><h1>__PROJECT_NAME__</h1><p id="summary"></p><p id="counts"></p></section>
    <section class="panel"><h2>Search</h2><input id="search" type="search" placeholder="Component, workflow, symbol…">
      <div id="search-results"></div></section>
    <section class="panel"><h2>Workflow</h2><select id="workflow-filter"><option value="">All workflows</option></select></section>
    <section class="panel"><h2>Layers</h2><div id="layer-filters"></div></section>
    <section class="panel"><h2>View</h2><div class="button-row"><button id="fit">Fit graph</button><button id="reset">Reset</button></div></section>
    <section class="panel"><h2>Analysis notes</h2><div id="warnings"></div></section>
  </aside>
  <main id="graph-wrap"><div id="graph"></div><div id="empty"></div></main>
  <aside id="details"><section class="panel"><h2>Details</h2><div id="detail-content"><p>Select a node to inspect its evidence.</p></div></section></aside>
  <script>
    const pipeline = __PIPELINE_DATA__;
    const layerColors = { interface:'#2563eb', application:'#0f766e', domain:'#a16207', core:'#4b5563', infrastructure:'#be123c' };
    const workflowPalette = ['#8b5cf6','#06b6d4','#f97316','#84cc16','#ec4899','#14b8a6','#eab308','#60a5fa'];
    const workflowById = new Map(pipeline.workflows.map(item => [item.id, item]));
    const componentById = new Map(pipeline.components.map(item => [item.id, item]));
    const workflowColors = new Map(pipeline.workflows.map((item, index) => [item.id, workflowPalette[index % workflowPalette.length]]));
    const allNodeRecords = [];
    const allEdgeRecords = [];
    const visualOutcomes = groupPipelineOutcomes(pipeline.outcomes);

    pipeline.workflows.forEach(item => allNodeRecords.push({ id:item.id, kind:'workflow', label:item.name,
      title:item.trigger_reason, shape:'box', shapeProperties:{borderRadius:18}, margin:{top:10,right:14,bottom:10,left:14},
      widthConstraint:{maximum:190}, color:nodeColor(workflowColors.get(item.id),'#ddd6fe'),
      font:{color:'#fff'}, workflows:[item.id], layer:'entrypoint', raw:item }));
    pipeline.components.forEach(item => allNodeRecords.push({ id:item.id, kind:'component', label:item.name + (item.cyclic ? ' ↻' : ''),
      title:item.layer, shape:'box', shapeProperties:{borderRadius:9}, margin:{top:10,right:14,bottom:10,left:14},
      widthConstraint:{maximum:210}, color:nodeColor(layerColors[item.layer] || '#4b5563','#cbd5e1'),
      font:{color:'#fff'}, workflows:item.workflow_ids, layer:item.layer, raw:item }));
    visualOutcomes.forEach(item => allNodeRecords.push({ id:item.id, kind:'outcome', label:outcomeGroupLabel(item),
      shape:'box', shapeProperties:{borderRadius:18}, margin:{top:9,right:13,bottom:9,left:13}, widthConstraint:{maximum:200},
      color:nodeColor('#166534','#86efac'), font:{color:'#fff'}, workflows:item.workflow_ids, layer:'outcome', raw:item }));
    pipeline.workflows.forEach(item => allEdgeRecords.push({ id:'start:'+item.id, from:item.id, to:item.trigger_component_id,
      arrows:'to', label:'starts', color:{color:workflowColors.get(item.id)}, workflows:[item.id], dashes:false }));
    pipeline.edges.forEach(item => allEdgeRecords.push({ id:item.id, from:item.from_component_id, to:item.to_component_id,
      arrows:'to', label:item.relation_types.join(' / '), color:{color:item.workflow_ids.length === 1 ? workflowColors.get(item.workflow_ids[0]) : '#d6ad60'},
      workflows:item.workflow_ids, dashes:item.cyclic, width:item.cyclic ? 2 : 1.4 }));
    visualOutcomes.forEach(item => allEdgeRecords.push({ id:'edge:'+item.id, from:item.component_id, to:item.id,
      arrows:'to', color:{color:item.workflow_ids.length===1 ? workflowColors.get(item.workflow_ids[0]) : '#d6ad60'},
      workflows:item.workflow_ids, dashes:item.observed_terminal }));

    document.getElementById('summary').textContent = pipeline.summary;
    document.getElementById('counts').textContent = `${pipeline.workflows.length} workflows · ${pipeline.components.length} components · ${pipeline.edges.length} links`;
    const warningBox = document.getElementById('warnings');
    pipeline.warnings.forEach(text => { const item=document.createElement('div'); item.className='warning'; item.textContent=text; warningBox.appendChild(item); });
    const workflowFilter = document.getElementById('workflow-filter');
    pipeline.workflows.forEach(item => { const option=document.createElement('option'); option.value=item.id; option.textContent=item.name; workflowFilter.appendChild(option); });
    const layers = [...new Set(pipeline.components.map(item => item.layer))].sort();
    const layerFilters = document.getElementById('layer-filters');
    layers.forEach(layer => { const label=document.createElement('label'); label.className='check'; const input=document.createElement('input');
      input.type='checkbox'; input.checked=true; input.value=layer; input.addEventListener('change', applyFilters);
      const dot=document.createElement('span'); dot.className='legend-dot'; dot.style.background=layerColors[layer] || '#4b5563';
      label.append(input,dot,document.createTextNode(layer)); layerFilters.appendChild(label); });

    const graph = document.getElementById('graph');
    let network = null;
    let visibleNodeById = new Map();
    if (typeof vis === 'undefined' || !vis.Network) {
      graph.innerHTML = '<div style="display:grid;height:100%;place-items:center;color:#fca5a5;padding:24px;text-align:center">vis-network failed to load. Check your connection to unpkg.com.</div>';
    } else {
      network = new vis.Network(graph, {nodes:new vis.DataSet(),edges:new vis.DataSet()}, {
        layout:{randomSeed:1729,improvedLayout:false}, physics:{enabled:false},
        interaction:{hover:true,navigationButtons:true,keyboard:true,zoomView:true,dragView:true,dragNodes:true},
        edges:{smooth:{enabled:true,type:'cubicBezier',forceDirection:'horizontal',roundness:.28},
          arrows:{to:{enabled:true,scaleFactor:.72}},arrowStrikethrough:false,selectionWidth:2,hoverWidth:1.5,
          font:{color:'#d1d5db',size:10,strokeWidth:4,strokeColor:'#0b1020',vadjust:-2}},
        nodes:{borderWidth:1.5,borderWidthSelected:3,font:{face:'Inter, system-ui, sans-serif',size:13},
          shadow:{enabled:true,color:'rgba(0,0,0,.3)',size:9,x:0,y:4}}
      });
      network.on('click', event => { if (event.nodes.length) showDetails(event.nodes[0]); });
      network.on('dragEnd', event => { if (event.nodes.length) network.storePositions(); });
      applyFilters();
    }

    function selectedLayers() { return new Set([...layerFilters.querySelectorAll('input:checked')].map(input => input.value)); }
    function applyFilters() {
      if (!network) return;
      const workflowId = workflowFilter.value;
      const enabledLayers = selectedLayers();
      let visibleNodes = allNodeRecords.filter(node => {
        if (node.kind === 'component' && !enabledLayers.has(node.layer)) return false;
        return !workflowId || node.workflows.includes(workflowId);
      });
      if (workflowId) visibleNodes = visibleNodes.map(node => filteredOutcomeNode(node,workflowId));
      const visibleIds = new Set(visibleNodes.map(node => node.id));
      const visibleEdges = allEdgeRecords.filter(edge => (!workflowId || edge.workflows.includes(workflowId)) && visibleIds.has(edge.from) && visibleIds.has(edge.to));
      visibleNodes = positionPipelineNodes(visibleNodes,visibleEdges);
      visibleNodeById = new Map(visibleNodes.map(node => [node.id,node]));
      network.setData({nodes:new vis.DataSet(visibleNodes),edges:new vis.DataSet(visibleEdges)});
      const empty = document.getElementById('empty');
      empty.style.display = visibleNodes.length ? 'none' : 'grid';
      empty.textContent = 'No pipeline nodes match the selected filters.';
      if (visibleNodes.length) requestAnimationFrame(() => frameReadableView(visibleNodes,visibleEdges,workflowId));
    }
    workflowFilter.addEventListener('change', applyFilters);
    document.getElementById('fit').addEventListener('click', () => network && network.fit({animation:{duration:300}}));
    document.getElementById('reset').addEventListener('click', () => { workflowFilter.value=''; layerFilters.querySelectorAll('input').forEach(input => input.checked=true); applyFilters(); });

    const DENSE_GRAPH_THRESHOLD = 40;
    function nodeColor(background,border) {
      return {background,border,highlight:{background,border:'#f8fafc'},hover:{background,border:'#e0e7ff'}};
    }
    function frameReadableView(visibleNodes,visibleEdges,workflowId) {
        if (!network) return;
        if (workflowId || visibleNodes.length<=DENSE_GRAPH_THRESHOLD) {
          network.fit({animation:false});
          return;
        }
        const focus=denseGraphFocus(visibleNodes,visibleEdges);
        if (focus) network.focus(focus.id,{scale:.68,animation:false});
    }
    function positionPipelineNodes(visibleNodes,visibleEdges) {
      const nodeById=new Map(visibleNodes.map(node => [node.id,node]));
      const outgoing=new Map(visibleNodes.map(node => [node.id,[]]));
      visibleEdges.forEach(edge => {
        const source=nodeById.get(edge.from); const target=nodeById.get(edge.to);
        if (!source || !target) return;
        if (edge.dashes && source.kind==='component' && target.kind==='component') return;
        outgoing.get(edge.from).push(edge.to);
      });
      outgoing.forEach(targets => targets.sort((left,right) => left.localeCompare(right)));
      const rank=new Map();
      const queue=[];
      visibleNodes.filter(node => node.kind==='workflow').sort((left,right) => left.id.localeCompare(right.id)).forEach(node => {
        rank.set(node.id,0); queue.push(node.id);
      });
      for (let index=0;index<queue.length;index+=1) {
        const sourceId=queue[index]; const nextRank=(rank.get(sourceId)||0)+1;
        (outgoing.get(sourceId)||[]).forEach(targetId => {
          if (rank.has(targetId)) return;
          rank.set(targetId,nextRank); queue.push(targetId);
        });
      }
      visibleNodes.filter(node => !rank.has(node.id)).sort((left,right) => left.id.localeCompare(right.id)).forEach(node => {
        rank.set(node.id,node.kind==='outcome' ? 2 : 1);
      });
      const stages=new Map();
      visibleNodes.forEach(node => {
        const stage=rank.get(node.id)||0;
        if (!stages.has(stage)) stages.set(stage,[]);
        stages.get(stage).push(node);
      });
      const positions=new Map();
      let cursorX=0;
      [...stages.keys()].sort((left,right) => left-right).forEach(stage => {
        const nodes=stages.get(stage).sort((left,right) =>
          left.kind.localeCompare(right.kind) || left.label.localeCompare(right.label) || left.id.localeCompare(right.id));
        const rowsPerColumn=10;
        const columnCount=Math.max(1,Math.ceil(nodes.length/rowsPerColumn));
        nodes.forEach((node,index) => {
          const column=Math.floor(index/rowsPerColumn);
          const row=index%rowsPerColumn;
          const rowsInColumn=Math.min(rowsPerColumn,nodes.length-column*rowsPerColumn);
          positions.set(node.id,{x:cursorX+column*250,y:(row-(rowsInColumn-1)/2)*88});
        });
        cursorX+=columnCount*250+150;
      });
      return visibleNodes.map(node => ({...node,...positions.get(node.id),physics:false}));
    }
    function denseGraphFocus(visibleNodes,visibleEdges) {
      const degree=new Map(visibleNodes.map(node => [node.id,0]));
      visibleEdges.forEach(edge => {
        degree.set(edge.from,(degree.get(edge.from)||0)+1);
        degree.set(edge.to,(degree.get(edge.to)||0)+1);
      });
      return visibleNodes.filter(node => node.kind==='component').sort((left,right) =>
        (right.workflows.length-left.workflows.length) ||
        ((degree.get(right.id)||0)-(degree.get(left.id)||0)) ||
        left.label.localeCompare(right.label)
      )[0] || visibleNodes[0];
    }
    function groupPipelineOutcomes(outcomes) {
      const groups=new Map();
      outcomes.forEach(item => {
        const key=JSON.stringify([item.component_id,item.kind,item.observed_terminal]);
        if (!groups.has(key)) groups.set(key,{id:'outcome-group:'+item.component_id+':'+item.kind+':'+(item.observed_terminal?'terminal':'effect'),
          component_id:item.component_id,kind:item.kind,observed_terminal:item.observed_terminal,workflow_ids:[],items:[]});
        const group=groups.get(key); group.items.push(item);
        if (!group.workflow_ids.includes(item.workflow_id)) group.workflow_ids.push(item.workflow_id);
      });
      return [...groups.values()].map(group => {
        group.workflow_ids.sort((left,right) => left.localeCompare(right));
        group.items.sort((left,right) => left.id.localeCompare(right.id));
        return group;
      }).sort((left,right) => left.id.localeCompare(right.id));
    }
    function outcomeGroupLabel(group) {
      if (group.items.length===1) {
        const item=group.items[0];
        return (item.observed_terminal ? 'Observed end: ' : item.kind+': ')+item.label;
      }
      return group.observed_terminal ? `Observed ends (${group.items.length})` : `${group.kind} (${group.items.length})`;
    }
    function filteredOutcomeNode(node,workflowId) {
      if (node.kind!=='outcome') return node;
      const items=node.raw.items.filter(item => item.workflow_id===workflowId);
      const raw={...node.raw,workflow_ids:[workflowId],items};
      return {...node,label:outcomeGroupLabel(raw),workflows:[workflowId],raw};
    }

    const search = document.getElementById('search');
    const searchResults = document.getElementById('search-results');
    search.addEventListener('input', () => {
      const query=search.value.trim().toLowerCase(); searchResults.replaceChildren();
      if (!query) { searchResults.style.display='none'; return; }
      const matches=allNodeRecords.filter(node => searchable(node).includes(query)).slice(0,8);
      matches.forEach(node => { const button=document.createElement('button'); button.className='search-result'; button.textContent=node.label;
        button.addEventListener('click', () => focusNode(node.id)); searchResults.appendChild(button); });
      searchResults.style.display=matches.length ? 'block' : 'none';
    });
    function searchable(node) { const raw=node.raw; return [node.label,node.layer,JSON.stringify(raw)].join(' ').toLowerCase(); }
    function focusNode(id) { if (!network) return; const node=allNodeRecords.find(item => item.id===id); if (!node) return;
      const workflowId=node.kind==='workflow' ? node.id : ''; if (workflowId) workflowFilter.value=workflowId; applyFilters();
      setTimeout(() => { network.selectNodes([id]); network.focus(id,{scale:1.35,animation:{duration:350}}); showDetails(id); },20);
      searchResults.style.display='none'; }

    function showDetails(id) {
      const node=visibleNodeById.get(id) || allNodeRecords.find(item => item.id===id); if (!node) return;
      const box=document.getElementById('detail-content'); box.replaceChildren();
      const title=document.createElement('div'); title.className='detail-title'; title.textContent=node.label; box.appendChild(title);
      addBadge(box,node.kind); addBadge(box,node.layer);
      if (node.kind==='workflow') {
        addRow(box,'Trigger',node.raw.trigger.label); addRow(box,'Location',node.raw.trigger.location || 'unknown');
        addRow(box,'Evidence',node.raw.trigger_reason); addRow(box,'Admission',node.raw.inferred ? 'inferred' : 'explicit');
      } else if (node.kind==='component') {
        addRow(box,'Layer',node.raw.layer); addRow(box,'Paths',node.raw.paths.join(', ') || 'unknown');
        addRow(box,'Workflows',node.raw.workflow_ids.map(id => workflowById.get(id)?.name || id).join(', '));
        const heading=document.createElement('h2'); heading.style.marginTop='18px'; heading.textContent=`Symbols (${node.raw.symbols.length})`; box.appendChild(heading);
        node.raw.symbols.forEach(symbol => { const item=document.createElement('div'); item.className='symbol';
          const name=document.createElement('div'); name.className='symbol-name'; name.textContent=symbol.label + (symbol.private ? ' (private)' : '');
          const location=document.createElement('div'); location.className='symbol-location'; location.textContent=symbol.location || symbol.node_type;
          item.append(name,location); box.appendChild(item); });
      } else {
        addRow(box,'Kind',node.raw.kind); addRow(box,'Results',String(node.raw.items.length));
        addRow(box,'Workflows',node.raw.workflow_ids.map(id => workflowById.get(id)?.name || id).join(', '));
        addRow(box,'Meaning',node.raw.observed_terminal ? 'End of available static evidence' : 'Observed graph effect');
        const heading=document.createElement('h2'); heading.style.marginTop='18px'; heading.textContent='Representative results'; box.appendChild(heading);
        const unique=[...new Map(node.raw.items.map(item => [item.label,item])).values()].slice(0,20);
        unique.forEach(result => { const item=document.createElement('div'); item.className='symbol';
          const name=document.createElement('div'); name.className='symbol-name'; name.textContent=result.label;
          const relation=document.createElement('div'); relation.className='symbol-location'; relation.textContent=result.kind;
          item.append(name,relation); box.appendChild(item); });
      }
    }
    function addBadge(parent,text) { const badge=document.createElement('span'); badge.className='badge'; badge.textContent=text; parent.appendChild(badge); }
    function addRow(parent,key,value) { const row=document.createElement('div'); row.className='row'; const left=document.createElement('div'); left.className='key'; left.textContent=key;
      const right=document.createElement('div'); right.className='value'; right.textContent=value || '—'; row.append(left,right); parent.appendChild(row); }
  </script>
</body>
</html>
"""
    return template.replace("__PROJECT_NAME__", project_name).replace("__PIPELINE_DATA__", payload)


def _write_text_atomic(path: Path, text: str) -> Path:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def _safe_json(payload: dict[str, object]) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _mermaid_id(prefix: str, value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"{prefix}_{normalized}"


def _mermaid_label(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", " ")
        .replace("\n", "<br/>")
    )


def _mermaid_edge_label(value: str) -> str:
    return _mermaid_label(value).replace("|", "/")


def _comment_text(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())
