"""
Interactive Wikidata knowledge-graph explorer, built around Q52 (Wikipedia).

Data source: live SPARQL queries against the public Wikidata Query Service
(https://query.wikidata.org/sparql). The app renders an entity's outgoing
direct claims (statements) as a force-directed graph:

  - Q-item values        -> "item" nodes (the semantic knowledge graph)
  - External-id values   -> one "identifier" node per property (values in tooltip)
  - URL / media / string -> "url" / "media" / "literal" nodes, grouped per property

Run with:  streamlit run app.py
"""

import json

import streamlit as st
import requests

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "KOF2026wikidatamcp-StreamlitDemo/0.1 (local knowledge-graph demo)"
DEFAULT_QID = "Q52"

# How Wikidata property datatypes map to visual node types.
PTYPE_TO_TYPE = {
    "ExternalId": "identifier",
    "Url": "url",
    "CommonsMedia": "media",
    "GeoCoordinates": "literal",
    "GlobeCoordinates": "literal",
    "Time": "literal",
    "Quantity": "literal",
    "Monolingualtext": "literal",
    "String": "literal",
}

TYPE_META = {
    "center": {"name": "Focus entity", "color": "#4f46e5"},
    "item": {"name": "Linked items (Q…)", "color": "#059669"},
    "url": {"name": "URLs", "color": "#d97706"},
    "media": {"name": "Media files", "color": "#db2777"},
    "identifier": {"name": "External IDs", "color": "#64748b"},
    "literal": {"name": "Literal values", "color": "#7c3aed"},
}

EDGE_COLOR = "#94a3b8"
EDGE_HIGHLIGHT = "#f59e0b"
GRAPH_HEIGHT = 720

st.set_page_config(page_title="Wikidata KG — Q52", page_icon="🕸️", layout="wide")


# ---------------------------------------------------------------- data access

@st.cache_data(ttl=3600, show_spinner=False)
def _sparql(query: str):
    """Run one SPARQL query against the Wikidata Query Service."""
    response = requests.get(
        SPARQL_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()["results"]["bindings"]


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_entity_graph(qid: str):
    """Fetch label, sitelink count and all direct claims for one entity."""
    qid = qid.strip().upper()
    if not _valid_qid(qid):
        raise ValueError(f"'{qid}' does not look like a Wikidata entity ID (Q followed by digits).")

    # Entity label (English).
    label_rows = _sparql(
        f'SELECT ?l WHERE {{ wd:{qid} rdfs:label ?l. FILTER(LANG(?l) = "en") }} LIMIT 1'
    )
    entity_label = label_rows[0]["l"]["value"] if label_rows else qid

    # Sitelink count (the "how many links" number).
    sitelink_rows = _sparql(f"SELECT ?s WHERE {{ wd:{qid} wikibase:sitelinks ?s }}")
    sitelinks = int(sitelink_rows[0]["s"]["value"]) if sitelink_rows else None

    # All direct (best-rank) claims with property labels and datatypes.
    rows = _sparql(
        f"""
        SELECT ?pid ?pname ?ptype ?val ?valLabel WHERE {{
          wd:{qid} ?p ?val.
          ?prop wikibase:directClaim ?p ;
                wikibase:propertyType ?ptype .
          OPTIONAL {{ ?prop rdfs:label ?propLabel . FILTER(LANG(?propLabel) = "en") }}
          BIND(STRAFTER(STR(?prop), "http://www.wikidata.org/entity/") AS ?pid)
          BIND(COALESCE(?propLabel, ?pid) AS ?pname)
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}
        """
    )

    center = {"id": qid, "label": entity_label, "type": "center", "qid": qid}
    nodes = {qid: center}
    literal_groups = {}
    edges = []

    for row in rows:
        pid = row["pid"]["value"]
        pname = row.get("pname", {}).get("value", pid)
        ptype = row["ptype"]["value"].rsplit("#", 1)[-1]
        val = row["val"]["value"]
        edge_label = f"{pname} ({pid})"

        if val.startswith("http://www.wikidata.org/entity/"):
            target = val.rsplit("/", 1)[-1]
            if target not in nodes:
                vlabel = row.get("valLabel", {}).get("value", target)
                nodes[target] = {"id": target, "label": vlabel, "type": "item", "qid": target}
            edges.append({"source": qid, "target": target, "label": edge_label})
        else:
            # Group literal-ish values (IDs, URLs, files, strings) per property
            # so hundreds of external IDs stay readable.
            ntype = PTYPE_TO_TYPE.get(ptype, "literal")
            key = f"lit:{pid}"
            group = literal_groups.get(key)
            if group is None:
                group = {"id": key, "label": pname, "type": ntype, "pid": pid, "values": []}
                literal_groups[key] = group
            if len(group["values"]) < 40:
                group["values"].append(row.get("valLabel", {}).get("value", val))
            edges.append({"source": qid, "target": key, "label": edge_label})

    all_nodes = list(nodes.values()) + list(literal_groups.values())
    stats = {
        "statements": len(edges),
        "distinct properties": len({e["label"] for e in edges}),
        "linked items": sum(1 for n in all_nodes if n["type"] == "item"),
        "sitelinks": sitelinks,
    }
    return qid, entity_label, all_nodes, edges, stats


def _valid_qid(qid: str) -> bool:
    return len(qid) >= 2 and qid[0] == "Q" and qid[1:].isdigit()


def build_graph_edges(edges, visible_ids):
    """Build plain vis-network edge dicts with fixed, selection-independent geometry.

    Several properties can link the same two items, so parallel edges are fanned
    out with a constant curvature. Click handling in the browser only changes
    labels and colours (never curvature or node positions), which keeps the
    layout and the zoom/pan state untouched when a node is selected.
    """
    pair_slot = {}
    result = []
    for edge in edges:
        if edge["source"] not in visible_ids or edge["target"] not in visible_ids:
            continue
        key = tuple(sorted((edge["source"], edge["target"])))
        slot = pair_slot.get(key, 0)
        pair_slot[key] = slot + 1
        item = {
            "id": f"e{len(result)}",
            "from": edge["source"],
            "to": edge["target"],
            "prop": edge["label"],
            "label": "",
            "title": edge["label"],
            "color": EDGE_COLOR,
            "width": 1,
            "font": {
                "size": 12,
                "color": "#92400e",
                "strokeWidth": 3,
                "strokeColor": "#ffffff",
            },
        }
        if slot:
            # Alternate sides and grow the curvature so the property labels of
            # parallel edges stay apart and readable.
            item["smooth"] = {
                "enabled": True,
                "type": "curvedCW" if slot % 2 == 1 else "curvedCCW",
                "roundness": min(1.6, 0.8 * ((slot + 1) // 2)),
            }
        result.append(item)
    return result


# Self-contained vis-network view. Node clicks are handled entirely inside the
# iframe: edge labels/highlights are toggled through the vis DataSet, which
# redraws without re-running physics, re-fitting the view, or re-rendering the
# Streamlit component. That is what keeps the layout and zoom stable on click.
GRAPH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  html, body {
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: "Source Sans Pro", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  #panel {
    padding: 8px 12px 12px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
  }
  #status {
    height: 22px;
    padding: 0 0 8px;
    font-size: 14px;
    color: #334155;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  #status b { color: #0f172a; }
  #graph {
    width: 100%;
    height: __GRAPH_HEIGHT__px;
    background: #ffffff;
  }
  div.vis-tooltip {
    position: absolute;
    padding: 6px 9px;
    font: 12px/1.45 "Source Sans Pro", -apple-system, BlinkMacSystemFont, sans-serif;
    color: #1f2937;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(15, 23, 42, 0.12);
    white-space: nowrap;
    max-width: 420px;
  }
</style>
</head>
<body>
<div id="panel">
  <div id="status">Click a node to show the property names of its edges.</div>
  <div id="graph"></div>
</div>
<script>
const GRAPH = __GRAPH_DATA__;

function initGraph() {
  const container = document.getElementById("graph");
  const status = document.getElementById("status");
  const nodes = new vis.DataSet(GRAPH.nodes);
  const edges = new vis.DataSet(GRAPH.edges);
  const network = new vis.Network(
    container,
    { nodes: nodes, edges: edges },
    GRAPH.options
  );
  // Exposed for debugging/automated checks; not used by the app itself.
  window.__visNetwork = network;
  window.__visEdges = edges;

  // node id -> ids of its incident edges; edge id -> property label.
  const incident = new Map();
  for (const edge of GRAPH.edges) {
    for (const end of [edge.from, edge.to]) {
      if (!incident.has(end)) incident.set(end, []);
      incident.get(end).push(edge.id);
    }
  }
  const edgeProps = new Map(GRAPH.edges.map((e) => [e.id, e.prop]));
  const nodeNames = new Map(GRAPH.nodes.map((n) => [n.id, n.label]));
  let selectedId = null;

  // Keep the property names legible at any zoom: vis scales label fonts with
  // the viewport, so compensate with the inverse of the current zoom.
  function edgeFont() {
    const size = Math.min(60, Math.max(6, 13 / network.getScale()));
    return {
      size: size,
      color: "#92400e",
      strokeWidth: Math.max(2, Math.round(size / 4)),
      strokeColor: "#ffffff"
    };
  }

  function setStatus(nodeId, count) {
    status.textContent = "";
    if (nodeId === null) {
      status.textContent = "Click a node to show the property names of its edges.";
      return;
    }
    status.append("Selected: ");
    const name = document.createElement("b");
    name.textContent = nodeNames.get(nodeId) || nodeId;
    status.append(name);
    status.append(
      " — showing property names on its " + count +
      " edge" + (count === 1 ? "" : "s") + "."
    );
  }

  function clearSelection() {
    selectedId = null;
    for (const edge of GRAPH.edges) {
      // vis ignores an empty-string label update (the old text would linger),
      // so a single space is used to clear labels.
      edges.update({ id: edge.id, label: " ", color: GRAPH.baseColor, width: 1 });
    }
    network.unselectAll();
    setStatus(null);
  }

  function selectNode(nodeId) {
    selectedId = nodeId;
    const connected = new Set(incident.get(nodeId) || []);
    const font = edgeFont();
    for (const edge of GRAPH.edges) {
      const on = connected.has(edge.id);
      edges.update({
        id: edge.id,
        label: on ? edgeProps.get(edge.id) : " ",
        color: on ? GRAPH.highlightColor : GRAPH.baseColor,
        width: on ? 2 : 1,
        font: { ...font }
      });
    }
    network.selectNodes([nodeId]);
    setStatus(nodeId, connected.size);
  }

  network.on("click", function (params) {
    if (params.nodes.length) {
      selectNode(params.nodes[0]);
    } else {
      clearSelection();
    }
  });

  network.on("zoom", function () {
    if (selectedId === null) return;
    const font = edgeFont();
    edges.update((incident.get(selectedId) || []).map((id) => ({ id: id, font: { ...font } })));
  });

  // Freeze the settled layout once: later label updates then cannot nudge any
  // node or reset the viewport.
  network.once("stabilizationIterationsDone", function () {
    network.setOptions({ physics: { enabled: false } });
    network.fit({ animation: false });
  });
}

const VIS_URLS = [
  "https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js",
  "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
];

(function loadVis(index) {
  if (index >= VIS_URLS.length) {
    document.getElementById("status").textContent =
      "Could not load the graph library (vis-network). Check the network connection.";
    return;
  }
  const script = document.createElement("script");
  script.src = VIS_URLS[index];
  script.onload = initGraph;
  script.onerror = function () { loadVis(index + 1); };
  document.head.appendChild(script);
})(0);
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ sidebar

st.sidebar.header("Graph controls")
qid_input = st.sidebar.text_input("Wikidata entity", value=DEFAULT_QID, help="e.g. Q52 (Wikipedia)")
show_types = st.sidebar.multiselect(
    "Node types to show",
    options=[t for t in TYPE_META if t != "center"],
    default=["item"],
    format_func=lambda t: TYPE_META[t]["name"],
)
search = st.sidebar.text_input("Filter nodes by label (substring)", value="")
search = search.strip().lower()

st.sidebar.caption(
    "Click a node in the graph to reveal the property names of its edges."
)

st.sidebar.caption(
    "Data is fetched live from the [Wikidata Query Service]"
    "(https://query.wikidata.org) via SPARQL and cached for one hour. "
    "Literal values (IDs, filenames, strings) are grouped into one node per "
    "property; hover a node to see its values."
)

# --------------------------------------------------------------- data load

try:
    qid, entity_label, all_nodes, edges, stats = fetch_entity_graph(qid_input)
except ValueError as exc:
    st.error(str(exc))
    st.stop()
except requests.RequestException as exc:
    st.error(f"Could not reach the Wikidata Query Service: {exc}")
    st.stop()

# ------------------------------------------------------------ page header

st.title("🕸️ Wikidata knowledge graph")
st.markdown(
    f"**{entity_label}** ({qid}) — outgoing direct claims rendered as an "
    "interactive force-directed graph. Drag nodes to rearrange them and "
    "pan/zoom with the mouse; hover a node for its details."
)

metric_cols = st.columns(len(stats))
for col, (name, value) in zip(metric_cols, stats.items()):
    col.metric(name.replace("_", " ").title(), f"{value:,}" if value is not None else "—")

# -------------------------------------------------------------- filtering

visible_nodes = []
for node in all_nodes:
    ntype = node["type"]
    if ntype != "center" and ntype not in show_types:
        continue
    if search and ntype != "center" and search not in node["label"].lower():
        continue
    visible_nodes.append(node)
visible_ids = {n["id"] for n in visible_nodes}

degree = {}
for edge in edges:
    if edge["source"] in visible_ids and edge["target"] in visible_ids:
        degree[edge["source"]] = degree.get(edge["source"], 0) + 1
        degree[edge["target"]] = degree.get(edge["target"], 0) + 1

# Disambiguate duplicate item labels (e.g. several items named "Wikipedia").
label_counts = {}
for node in visible_nodes:
    if node["type"] == "item":
        label_counts[node["label"]] = label_counts.get(node["label"], 0) + 1


def display_label(node):
    label = node["label"]
    if node["type"] == "item" and label_counts.get(label, 0) > 1:
        label = f"{label} ({node['qid']})"
    return label if len(label) <= 32 else label[:29] + "…"


def tooltip(node):
    if node["type"] == "center":
        return f"{entity_label} ({qid}) — {stats['statements']} statements"
    if node["type"] == "item":
        return f"{node['label']} ({node['qid']})\nhttps://www.wikidata.org/wiki/{node['qid']}"
    values = node.get("values", [])
    head = "\n".join(values[:15])
    more = f"\n… and {len(values) - 15} more" if len(values) > 15 else ""
    return f"{node['label']} ({node.get('pid', '')})\n{head}{more}"


graph_nodes = [
    {
        "id": node["id"],
        "label": display_label(node),
        "shape": "dot",
        "size": 28 if node["type"] == "center" else min(8 + degree.get(node["id"], 0), 24),
        "color": TYPE_META[node["type"]]["color"],
        "title": tooltip(node).replace("\n", "<br>"),
    }
    for node in visible_nodes
]
graph_edges = build_graph_edges(edges, visible_ids)

# ---------------------------------------------------------------- rendering

legend = "  ".join(
    f"<span style='color:{meta['color']}; font-size:18px'>●</span> "
    f"<span style='color:#334155'>{meta['name']}</span>"
    for ntype, meta in TYPE_META.items()
    if ntype == "center" or ntype in show_types
)
st.markdown(legend, unsafe_allow_html=True)

if not graph_nodes:
    st.info("No nodes match the current filters.")
    st.stop()

graph_data = {
    "nodes": graph_nodes,
    "edges": graph_edges,
    "baseColor": EDGE_COLOR,
    "highlightColor": EDGE_HIGHLIGHT,
    "options": {
        "autoResize": True,
        "interaction": {
            "hover": True,
            "tooltipDelay": 120,
            "navigationButtons": False,
            "multiselect": False,
        },
        # Tuned force layout for a star-shaped entity neighbourhood. Physics is
        # switched off as soon as stabilization finishes (see the template).
        "physics": {
            "enabled": True,
            "solver": "barnesHut",
            "barnesHut": {
                "gravitationalConstant": -30000,
                "centralGravity": 0.12,
                "springLength": 320,
                "springConstant": 0.04,
                "damping": 0.45,
                "avoidOverlap": 0.5,
            },
            "minVelocity": 0.6,
            "timestep": 0.4,
            "stabilization": {"enabled": True, "iterations": 250, "fit": True},
        },
    },
}
graph_html = (
    GRAPH_TEMPLATE.replace("__GRAPH_HEIGHT__", str(GRAPH_HEIGHT))
    .replace("__GRAPH_DATA__", json.dumps(graph_data, ensure_ascii=False).replace("</", "<\\/"))
)
st.iframe(graph_html, height=GRAPH_HEIGHT + 60, width="stretch")

graph_json = {"center": qid, "label": entity_label, "nodes": all_nodes, "edges": edges, "stats": stats}
st.download_button(
    "⬇️ Download graph as JSON",
    data=json.dumps(graph_json, ensure_ascii=False, indent=2),
    file_name=f"{qid}_knowledge_graph.json",
    mime="application/json",
)
