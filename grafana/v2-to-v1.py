#!/usr/bin/env python3
"""Convert Grafana schema-v2 dashboard exports to the v1 provisioning form.

Grafana 13 exports dashboards as a Kubernetes-style resource
(apiVersion: dashboard.grafana.app/v2) with panels in `spec.elements`,
their positions in a separate `spec.layout`, and variables in
`spec.variables`. Classic file provisioning has been unreliable with that
shape - see the note in the README - so this rewrites one or more v2 files
into the v1 `panels[]` + `gridPos` form that provisioning has always
accepted, keeping every title, size, position and panel option.

    python3 grafana/v2-to-v1.py grafana/dashboards/*.json -o grafana/dashboards-v1

Nothing here is specific to this project; it is plain stdlib.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# v2 spells these as words; v1 wants the numbers it always used.
REFRESH = {"onDashboardLoad": 1, "onTimeRangeChanged": 2, "never": 0}
HIDE = {"dontHide": 0, "hideLabel": 1, "hideVariable": 2}


def datasource(query_spec: dict) -> dict | None:
    ds = query_spec.get("datasource") or {}
    if not ds.get("name"):
        return None
    return {"type": query_spec.get("group", ""), "uid": ds["name"]}


def targets(element_spec: dict) -> list[dict]:
    out = []
    for query in element_spec.get("data", {}).get("spec", {}).get("queries", []):
        qs = query["spec"]
        inner = qs["query"]
        spec = inner.get("spec", {})
        target = {"refId": qs.get("refId", "A")}
        ds = datasource(inner)
        if ds:
            target["datasource"] = ds
        # Carry the query body through verbatim: format, rawSql, rawQuery,
        # editorMode and anything else a panel type happens to store.
        target.update(spec)
        if qs.get("hidden"):
            target["hide"] = True
        out.append(target)
    return out


def variables(spec: dict) -> list[dict]:
    out = []
    for var in spec.get("variables", []):
        kind, vs = var.get("kind"), var["spec"]
        common = {
            "name": vs["name"],
            "label": vs.get("label"),
            "hide": HIDE.get(vs.get("hide"), 0),
            "skipUrlSync": vs.get("skipUrlSync", False),
            "multi": vs.get("multi", False),
            "includeAll": vs.get("includeAll", False),
            "current": vs.get("current", {}),
            "options": vs.get("options", []),
        }
        if kind == "QueryVariable":
            q = vs.get("query", {}) or {}
            qspec = q.get("spec", {}) or {}
            out.append({**common, "type": "query",
                        # A v1 string variable carried into v2 keeps its SQL
                        # in __legacyStringValue; one authored in v2 uses
                        # rawSql.
                        "query": qspec.get("__legacyStringValue")
                                 or qspec.get("rawSql", ""),
                        "datasource": datasource(q),
                        "refresh": REFRESH.get(vs.get("refresh"), 1),
                        "sort": 0,
                        "regex": vs.get("regex", "")})
        elif kind == "CustomVariable":
            out.append({**common, "type": "custom",
                        "query": vs.get("query", "")})
        elif kind == "ConstantVariable":
            out.append({**common, "type": "constant",
                        "query": vs.get("query", "")})
        else:
            print(f"  warning: variable {vs.get('name')!r} is a "
                  f"{kind} and was copied as-is", file=sys.stderr)
            out.append({**common, "type": (kind or "custom").replace(
                "Variable", "").lower()})
    return out


def panels(spec: dict) -> list[dict]:
    where = {}
    for item in spec.get("layout", {}).get("spec", {}).get("items", []):
        s = item["spec"]
        where[s["element"]["name"]] = {"h": s.get("height", 8),
                                       "w": s.get("width", 12),
                                       "x": s.get("x", 0),
                                       "y": s.get("y", 0)}
    out = []
    for name, element in spec.get("elements", {}).items():
        es = element["spec"]
        viz = es.get("vizConfig", {})
        vspec = viz.get("spec", {})
        panel = {
            "id": es.get("id"),
            "type": viz.get("group", "timeseries"),
            "title": es.get("title", name),
            "description": es.get("description", ""),
            "gridPos": where.get(name, {"h": 8, "w": 12, "x": 0, "y": 0}),
            "fieldConfig": vspec.get("fieldConfig",
                                     {"defaults": {}, "overrides": []}),
            "options": vspec.get("options", {}),
            "targets": targets(es),
            "links": es.get("links", []),
        }
        tf = es.get("data", {}).get("spec", {}).get("transformations")
        if tf:
            panel["transformations"] = tf
        # Panels share one datasource here; v1 wants it on the panel too.
        if panel["targets"] and "datasource" in panel["targets"][0]:
            panel["datasource"] = panel["targets"][0]["datasource"]
        out.append(panel)
    return sorted(out, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))


def convert(raw: dict) -> dict:
    spec = raw["spec"]
    ts = spec.get("timeSettings", {})
    picker = {"hidden": True} if ts.get("hideTimepicker") else {}
    return {
        "uid": raw.get("metadata", {}).get("name"),
        "title": spec.get("title", ""),
        "description": spec.get("description", ""),
        "tags": spec.get("tags", []),
        "editable": spec.get("editable", True),
        "timezone": ts.get("timezone", "browser"),
        "time": {"from": ts.get("from", "now-6h"), "to": ts.get("to", "now")},
        "timepicker": picker,
        "refresh": ts.get("autoRefresh", ""),
        "fiscalYearStartMonth": ts.get("fiscalYearStartMonth", 0),
        "schemaVersion": 39,
        "version": 1,
        "annotations": {"list": [
            {**a["spec"], "datasource": (a["spec"].get("query") or {})
                                        .get("datasource")}
            for a in spec.get("annotations", [])]},
        "templating": {"list": variables(spec)},
        "panels": panels(spec),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out", required=True,
                    help="directory to write the v1 files into")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        raw = json.loads(pathlib.Path(path).read_text())
        if raw.get("apiVersion", "").startswith("dashboard.grafana.app/v1") \
                or "panels" in raw:
            print(f"  {pathlib.Path(path).name}: already v1, skipped")
            continue
        v1 = convert(raw)
        target = out / pathlib.Path(path).name
        target.write_text(json.dumps(v1, indent=2) + "\n")
        print(f"  {pathlib.Path(path).name}: {len(v1['panels'])} panels, "
              f"{len(v1['templating']['list'])} variables -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
