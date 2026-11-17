"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlow, Background, Controls, MarkerType, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Ban, Brain, Check, CircleCheck, CircleHelp, ExternalLink, FileText, Link2, LoaderCircle, Moon, Play, Plus,
  RefreshCw, ShieldCheck, Sunrise, TriangleAlert, Upload, X,
} from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

type Any = any;

const STATUS: Record<string, { label: string; text: string; border: string; bg: string; hex: string }> = {
  verified_issue: { label: "Verified issue", text: "text-red-300", border: "border-red-500/60", bg: "bg-red-500/10", hex: "#ef4444" },
  possible_issue: { label: "Possible issue", text: "text-amber-300", border: "border-amber-500/60", bg: "bg-amber-500/10", hex: "#f59e0b" },
  verified_fix: { label: "Verified fix", text: "text-emerald-300", border: "border-emerald-500/60", bg: "bg-emerald-500/10", hex: "#10b981" },
  verified_ok: { label: "Verified OK", text: "text-emerald-300", border: "border-emerald-500/40", bg: "bg-emerald-500/5", hex: "#10b981" },
  pending_approval: { label: "Pending approval", text: "text-sky-300", border: "border-sky-500/50", bg: "bg-sky-500/10", hex: "#38bdf8" },
  approved: { label: "Approved (not sent)", text: "text-emerald-300", border: "border-emerald-500/40", bg: "bg-emerald-500/5", hex: "#10b981" },
  dismissed: { label: "Dismissed", text: "text-slate-400", border: "border-slate-600", bg: "bg-slate-800/40", hex: "#64748b" },
};
const st = (s?: string | null) => STATUS[s || ""] || { label: s || "No status", text: "text-slate-300", border: "border-slate-700", bg: "bg-slate-800/40", hex: "#475569" };

function Pill({ status }: { status?: string | null }) {
  const s = st(status);
  const Icon = status === "verified_issue" ? TriangleAlert : status === "possible_issue" ? CircleHelp : status === "dismissed" ? Ban : CircleCheck;
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${s.text} ${s.border} ${s.bg}`}>
      <Icon size={12} /> {s.label}
    </span>
  );
}

const POS: Record<string, { x: number; y: number }> = {
  event: { x: 420, y: 0 },
  dep_location: { x: 105, y: 130 }, dep_portfolio: { x: 420, y: 130 }, dep_resume: { x: 840, y: 130 },
  src_ics: { x: 0, y: 290 }, src_email: { x: 210, y: 290 }, src_url: { x: 420, y: 290 },
  repair_packet: { x: 630, y: 290 }, src_notes: { x: 840, y: 290 },
};

async function api(path: string, init?: RequestInit) {
  const r = await fetch(API + path, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body?.detail || `${path} failed (HTTP ${r.status})`);
  return body;
}

export default function Home() {
  const [report, setReport] = useState<Any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [stage, setStage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>("event");
  const [url, setUrl] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [openSource, setOpenSource] = useState<Any>(null);
  const urlDirty = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const r = await api("/api/report/latest");
      setReport(r);
      const src = r.sources?.find((s: Any) => s.id === "src_url");
      if (src && !urlDirty.current) setUrl(src.content);
      setError(null);
    } catch (e: Any) {
      setError(`Cannot reach the DAYBREAK backend at ${API}. ${e.message}`);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!busy) { setStage(null); return; }
    const t = setInterval(() => api("/api/status").then((s) => setStage(s.stage)).catch(() => {}), 1000);
    return () => clearInterval(t);
  }, [busy]);

  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setError(null);
    try { await fn(); } catch (e: Any) { setError(e.message); }
    urlDirty.current = false;
    await refresh();
    setBusy(null);
  };
  const run = () => api("/api/run", { method: "POST" }).then((r) => { if (r.status === "failed") throw new Error(`Run #${r.run_id} failed: ${r.error}`); });
  const loadDemo = () => act("Loading demo and running preflight", async () => { await api("/api/seed-demo", { method: "POST" }); urlDirty.current = false; await run(); setSelected("event"); });
  const runNow = () => act("Running tonight's preflight now", run);
  const reverify = () => act("Saving URL and re-verifying", async () => {
    await api("/api/update-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    await run(); setSelected("dep_portfolio");
  });
  const setRepair = (id: number, status: string) => act("Updating repair", async () => {
    await api(`/api/repairs/${id}/status`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
  });

  const findings: Any[] = report?.findings || [];
  const repairs: Any[] = report?.repairs || [];
  const sources: Any[] = report?.sources || [];
  const gNodes: Any[] = report?.graph?.nodes || [];
  const runInfo = report?.run;
  const counts = {
    verified_issue: findings.filter((f) => f.status === "verified_issue").length,
    possible_issue: findings.filter((f) => f.status === "possible_issue").length,
    fixed: findings.filter((f) => f.status === "verified_fix").length + repairs.filter((r) => r.status === "verified_fix").length,
  };

  const { nodes, edges } = useMemo(() => {
    let extra = 0;
    const nodes: Node[] = gNodes.map((n) => {
      const s = st(n.status);
      const pos = POS[n.id] || { x: 1060, y: 130 + 90 * extra++ };
      const sel = selected === n.id;
      return {
        id: n.id, position: pos, draggable: true,
        data: {
          label: (
            <div className="text-left">
              <div className="text-[10px] uppercase tracking-wider text-slate-400">{n.kind}</div>
              <div className="text-[13px] font-semibold text-slate-100">{n.label}</div>
              {n.sublabel && <div className="truncate text-[11px] text-slate-400">{n.sublabel}</div>}
            </div>
          ),
        },
        style: {
          width: 190, padding: "8px 10px", borderRadius: 12, background: "#0f172a", color: "#e2e8f0",
          border: `${sel ? 2 : 1.5}px solid ${s.hex}`,
          boxShadow: sel ? `0 0 0 4px ${s.hex}33, 0 0 24px ${s.hex}55` : `0 0 12px ${s.hex}22`,
        },
      };
    });
    const byId = Object.fromEntries(gNodes.map((n) => [n.id, n]));
    const edges: Edge[] = (report?.graph?.edges || []).map((e: Any) => {
      const hex = st(byId[e.target]?.status).hex;
      return {
        id: e.id, source: e.source, target: e.target, label: e.label, animated: byId[e.target]?.status === "verified_issue",
        style: { stroke: hex, strokeWidth: 1.5 }, markerEnd: { type: MarkerType.ArrowClosed, color: hex },
      };
    });
    return { nodes, edges };
  }, [gNodes, report, selected]);

  const selNode = gNodes.find((n) => n.id === selected) || gNodes[0];
  const selFinding = findings.find((f) => f.key === selNode?.finding_key);
  const selSource = sources.find((s) => s.id === selNode?.id);
  const readiness = runInfo?.readiness;
  const badge = !runInfo
    ? { t: "No run yet", c: "border-slate-600 text-slate-300 bg-slate-800/50" }
    : readiness === "ready" ? { t: "READY", c: "border-emerald-500/60 text-emerald-300 bg-emerald-500/10" }
    : readiness === "at_risk" ? { t: `AT RISK · ${counts.possible_issue} unverified`, c: "border-amber-500/60 text-amber-300 bg-amber-500/10" }
    : readiness === "not_ready" ? { t: `NOT READY · ${counts.verified_issue} verified issue${counts.verified_issue === 1 ? "" : "s"}`, c: "border-red-500/60 text-red-300 bg-red-500/10" }
    : { t: "RUN FAILED", c: "border-red-500/60 text-red-300 bg-red-500/10" };
  const title = report?.extraction?.resolved?.event_title?.value;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-4 gap-y-3 px-6 py-4">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="rounded-xl bg-gradient-to-br from-amber-400 to-rose-500 p-2 text-slate-950"><Sunrise size={22} /></div>
            <div className="min-w-0">
              <h1 className="text-lg font-bold tracking-tight text-white">
                DAYBREAK <span className="font-normal text-slate-400">— Go to sleep with plans. Wake up with plans that have been tested.</span>
              </h1>
              <p className="truncate text-xs text-slate-500">
                Morning Report{title ? ` · ${title}` : ""}
                {runInfo ? ` · run #${runInfo.id} (${runInfo.trigger}) finished ${new Date(runInfo.finished_at).toLocaleTimeString()}` : ""}
                {report?.next_overnight_run ? ` · next overnight run ${report.next_overnight_run.slice(0, 16)}` : ""}
              </p>
            </div>
          </div>
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${badge.c}`}>{badge.t}</span>
          <button onClick={loadDemo} disabled={!!busy} className="inline-flex items-center gap-2 rounded-lg bg-amber-400 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-amber-300 disabled:opacity-50">
            <Play size={15} /> 1. Load Demo &amp; Run
          </button>
          <button onClick={runNow} disabled={!!busy} className="inline-flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-50">
            <Moon size={15} /> Run Tonight Now
          </button>
          <button onClick={() => setShowAdd(true)} disabled={!!busy} className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50">
            <Plus size={15} /> Add Input
          </button>
        </div>
        {busy && (
          <div className="border-t border-slate-800 bg-slate-900/80">
            <div className="mx-auto flex max-w-[1500px] items-center gap-2 px-6 py-2 text-sm text-amber-200">
              <LoaderCircle size={15} className="animate-spin" /> {busy}{stage ? ` — ${stage}…` : "…"}
            </div>
          </div>
        )}
      </header>

      <main className="mx-auto max-w-[1500px] space-y-5 px-6 py-5">
        {error && <div className="rounded-lg border border-red-500/50 bg-red-500/10 px-4 py-3 text-sm text-red-200">{error}</div>}

        {/* Live Twist */}
        <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white"><Link2 size={16} className="text-amber-300" /> Live Twist · Portfolio URL</div>
            <input
              value={url} onChange={(e) => { urlDirty.current = true; setUrl(e.target.value); }}
              onKeyDown={(e) => e.key === "Enter" && !busy && reverify()}
              placeholder="https://…" spellCheck={false}
              className="min-w-[260px] flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-amber-400"
            />
            <button onClick={reverify} disabled={!!busy || !url} className="inline-flex items-center gap-2 rounded-lg bg-emerald-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-50">
              <RefreshCw size={15} className={busy ? "animate-spin" : ""} /> Save &amp; Re-Verify
            </button>
            {(() => { const f = findings.find((x) => x.key === "portfolio"); return f ? <Pill status={f.status} /> : null; })()}
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-500">
            Try:
            {["https://github.com/settings/profile", "https://example.com"].map((u) => (
              <button key={u} onClick={() => { urlDirty.current = true; setUrl(u); }} className="rounded border border-slate-700 px-2 py-0.5 font-mono text-slate-300 hover:border-amber-400">{u}</button>
            ))}
            <span>Checked in a fresh Chromium context with zero cookies. HTTP 200 alone never counts as public.</span>
          </div>
        </section>

        {!runInfo && !busy && (
          <div className="rounded-xl border border-dashed border-slate-700 p-10 text-center text-slate-400">
            No Morning Report yet. Click <span className="font-semibold text-amber-300">1. Load Demo &amp; Run</span> to seed tomorrow&apos;s interview and test it.
          </div>
        )}

        {runInfo && (
          <>
            <div className="grid gap-5 xl:grid-cols-5">
              <section className="rounded-xl border border-slate-800 bg-slate-900/60 xl:col-span-3">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
                  <h2 className="text-sm font-semibold text-white">Dependency Graph</h2>
                  <div className="flex gap-3 text-[11px] text-slate-400">
                    {(["verified_issue", "possible_issue", "verified_fix"] as const).map((k) => (
                      <span key={k} className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS[k].hex }} />{STATUS[k].label}</span>
                    ))}
                  </div>
                </div>
                <div className="h-[470px]">
                  <ReactFlow nodes={nodes} edges={edges} fitView
                    onNodeClick={(_, n) => setSelected(n.id)} nodesConnectable={false} minZoom={0.3}>
                    <Background color="#1e293b" gap={22} />
                    <Controls showInteractive={false} />
                  </ReactFlow>
                </div>
              </section>

              <section className="rounded-xl border border-slate-800 bg-slate-900/60 xl:col-span-2">
                <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
                  <h2 className="flex items-center gap-2 text-sm font-semibold text-white"><ShieldCheck size={16} className="text-amber-300" /> Evidence Inspector</h2>
                  {selNode && <span className="font-mono text-xs text-slate-500">{selNode.id}</span>}
                </div>
                <div className="max-h-[470px] space-y-4 overflow-y-auto p-4 text-sm">
                  {selNode?.kind === "event" && <EventPanel report={report} counts={counts} />}
                  {selFinding && <Evidence f={selFinding} />}
                  {selNode?.kind === "repair" && repairs.filter((r) => r.kind === "offline_packet").map((r) => <PacketCard key={r.id} r={r} />)}
                  {selSource && (
                    <div>
                      <Label>Source content · {selSource.id} ({selSource.type})</Label>
                      <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] text-slate-300">{selSource.content || "(empty)"}</pre>
                    </div>
                  )}
                </div>
              </section>
            </div>

            <div className="grid gap-5 xl:grid-cols-2">
              <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <h2 className="mb-3 text-sm font-semibold text-white">Findings</h2>
                <div className="space-y-2">
                  {findings.map((f) => {
                    const nodeId = gNodes.find((n) => n.finding_key === f.key && n.kind !== "repair" && (n.kind === "dependency" || f.key.startsWith("unverified")))?.id;
                    return (
                      <button key={f.id} onClick={() => nodeId && setSelected(nodeId)}
                        className={`block w-full rounded-lg border p-3 text-left transition hover:brightness-125 ${st(f.status).border} ${st(f.status).bg}`}>
                        <div className="flex items-start justify-between gap-3">
                          <div className="font-medium text-white">{f.title}</div><Pill status={f.status} />
                        </div>
                        <p className="mt-1 break-words text-[13px] text-slate-300">{f.summary}</p>
                        <p className="mt-1 font-mono text-[11px] text-slate-500">cites: {f.source_ids.join(", ") || "—"}</p>
                      </button>
                    );
                  })}
                </div>
              </section>

              <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <h2 className="mb-3 text-sm font-semibold text-white">Safe Repairs &amp; Pending Approvals</h2>
                {repairs.length === 0 && <p className="text-sm text-slate-500">No repairs needed for this run.</p>}
                <div className="space-y-3">
                  {repairs.map((r) => r.kind === "offline_packet" ? <PacketCard key={r.id} r={r} /> : (
                    <div key={r.id} className={`rounded-lg border p-3 ${st(r.status).border} ${st(r.status).bg}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="font-medium text-white">{r.title}</div><Pill status={r.status} />
                      </div>
                      <pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[11px] text-slate-300">{r.body}</pre>
                      <div className="mt-2 flex items-center gap-2">
                        <button disabled={!!busy || r.status === "approved"} onClick={() => setRepair(r.id, "approved")} className="inline-flex items-center gap-1 rounded-md bg-emerald-500 px-2.5 py-1 text-xs font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-40"><Check size={13} /> Approve</button>
                        <button disabled={!!busy || r.status === "dismissed"} onClick={() => setRepair(r.id, "dismissed")} className="inline-flex items-center gap-1 rounded-md border border-slate-600 px-2.5 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-40"><X size={13} /> Dismiss</button>
                        <span className="text-[11px] text-slate-500">Draft only. Nothing is sent and no calendar is changed.</span>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <div className="grid gap-5 xl:grid-cols-2">
              <CogneePanel cognee={report.cognee} extraction={report.extraction} />
              <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-white"><FileText size={16} className="text-amber-300" /> Persisted Inputs (SQLite)</h2>
                <div className="space-y-2">
                  {sources.map((s) => (
                    <button key={s.id} onClick={() => setOpenSource(s)} className="flex w-full items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-left hover:border-slate-600">
                      <span className="min-w-0"><span className="font-mono text-xs text-amber-300">{s.id}</span> <span className="text-sm text-slate-200">{s.title}</span>
                        <span className="block truncate text-[11px] text-slate-500">{s.type}{s.meta?.parser ? ` · parsed by ${s.meta.parser}` : ""} · updated {new Date(s.updated_at).toLocaleString()}</span></span>
                      <span className="text-xs text-slate-500">open</span>
                    </button>
                  ))}
                </div>
              </section>
            </div>
          </>
        )}
      </main>

      {showAdd && <AddModal onClose={() => setShowAdd(false)} onSaved={async () => { setShowAdd(false); await refresh(); }} />}
      {openSource && (
        <Modal title={`${openSource.id} · ${openSource.title}`} onClose={() => setOpenSource(null)}>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-slate-300">{openSource.content || "(empty)"}</pre>
        </Modal>
      )}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">{children}</div>;
}

function Shot({ path, alt }: { path: string; alt: string }) {
  return (
    <a href={API + path} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-lg border border-slate-700 bg-slate-950">
      <img src={API + path} alt={alt} className="max-h-72 w-full object-cover object-top" />
    </a>
  );
}

function EventPanel({ report, counts }: { report: Any; counts: Any }) {
  const r = report?.extraction?.resolved || {};
  const g = report?.extraction?.gemini;
  return (
    <div className="space-y-3">
      <p className="text-slate-300">
        <span className="font-semibold text-red-300">{counts.verified_issue} verified</span>,{" "}
        <span className="font-semibold text-amber-300">{counts.possible_issue} possible</span>,{" "}
        <span className="font-semibold text-emerald-300">{counts.fixed} verified fix{counts.fixed === 1 ? "" : "es"}</span>. Click any node to inspect its evidence.
      </p>
      <div>
        <Label>Hypotheses ({g?.ok ? `Gemini ${g.model}` : "Gemini unavailable, parser fallback"}) — not treated as verification</Label>
        <div className="overflow-hidden rounded-lg border border-slate-800">
          {Object.entries(r).map(([k, v]: Any) => (
            <div key={k} className="grid grid-cols-5 gap-2 border-b border-slate-800/70 px-3 py-1.5 text-xs last:border-0">
              <span className="col-span-2 font-mono text-slate-500">{k}</span>
              <span className="col-span-3 break-words text-slate-200">{v.value ?? "—"} <span className="text-slate-500">· {v.origin}{v.source_id ? ` · ${v.source_id}` : ""}</span></span>
            </div>
          ))}
        </div>
        {g?.failed_attempts?.length > 0 && <p className="mt-1 text-[11px] text-amber-300/80">Note: {g.failed_attempts.map((a: Any) => `${a.model} unavailable`).join(", ")}{g.ok ? `; answered by ${g.model}.` : "."}</p>}
      </div>
    </div>
  );
}

function Evidence({ f }: { f: Any }) {
  const ev = f.evidence || {};
  const web = ev.web;
  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-start justify-between gap-3"><h3 className="font-semibold text-white">{f.title}</h3><Pill status={f.status} /></div>
        <p className="mt-1 break-words text-slate-300">{f.summary}</p>
        <p className="mt-1 text-[11px] text-slate-500">Check used: {ev.check}</p>
      </div>
      {ev.citations?.length > 0 && (
        <div>
          <Label>Source citations</Label>
          <div className={`grid gap-2 ${f.key === "location" && ev.citations.length === 2 ? "grid-cols-2" : ""}`}>
            {ev.citations.map((c: Any, i: number) => (
              <div key={i} className={`rounded-lg border p-2 ${f.key === "location" ? (i === 0 ? "border-red-500/40 bg-red-500/5" : "border-emerald-500/40 bg-emerald-500/5") : "border-slate-700 bg-slate-950/60"}`}>
                <div className="font-mono text-[11px] text-amber-300">{c.source_id} <span className="text-slate-500">· {c.type}</span></div>
                <blockquote className="mt-1 break-words font-mono text-[12px] text-slate-200">“{c.quote}”</blockquote>
              </div>
            ))}
          </div>
        </div>
      )}
      {web && (
        <>
          <div>
            <Label>HTTP &amp; redirect chain (fresh context, zero cookies)</Label>
            <div className="space-y-1 rounded-lg border border-slate-700 bg-slate-950/60 p-2 font-mono text-[11px]">
              {(web.redirect_chain?.length ? web.redirect_chain : [{ url: web.url, status: web.http_status }]).map((h: Any, i: number) => (
                <div key={i} className="flex gap-2"><span className={h.status >= 300 && h.status < 400 ? "text-amber-300" : h.status >= 400 ? "text-red-300" : "text-emerald-300"}>{h.status ?? "—"}</span><span className="break-all text-slate-300">{i > 0 ? "→ " : ""}{h.url}</span></div>
              ))}
              {web.nav_error && <div className="text-red-300">{web.nav_error}</div>}
            </div>
          </div>
          <div>
            <Label>Checks</Label>
            <ul className="space-y-1">
              {web.checks.map((c: Any, i: number) => (
                <li key={i} className="flex gap-2 text-[12px]">
                  {c.passed ? <CircleCheck size={14} className="mt-0.5 shrink-0 text-emerald-400" /> : <TriangleAlert size={14} className="mt-0.5 shrink-0 text-red-400" />}
                  <span className="break-words"><span className="text-slate-200">{c.name}</span> <span className="break-all text-slate-500">— {c.detail}</span></span>
                </li>
              ))}
            </ul>
          </div>
          {web.screenshot && <div><Label>Playwright screenshot</Label><Shot path={web.screenshot} alt="Playwright screenshot of the portfolio URL" /></div>}
          {ev.previous && <p className="text-[11px] text-slate-500">Previous check: run #{ev.previous.run_id} was {st(ev.previous.status).label.toLowerCase()}{ev.previous.url ? ` for ${ev.previous.url}` : ""}.</p>}
        </>
      )}
      {(ev.referenced_version || ev.latest_version) && ev.referenced_resume && (
        <p className="font-mono text-[11px] text-slate-400">parsed versions: {ev.referenced_resume} → v{ev.referenced_version?.join(" / ")} · {ev.latest_resume} → v{ev.latest_version?.join(" / ")}</p>
      )}
      {ev.cognee_memory?.length > 0 && (
        <div>
          <Label>Cognee retrieved memory (hypothesis context)</Label>
          {ev.cognee_memory.map((m: string, i: number) => (
            <pre key={i} className="mb-1 max-h-32 overflow-auto whitespace-pre-wrap rounded-lg border border-violet-500/30 bg-violet-500/5 p-2 font-mono text-[11px] text-violet-100">{m}</pre>
          ))}
        </div>
      )}
    </div>
  );
}

function PacketCard({ r }: { r: Any }) {
  return (
    <div className={`rounded-lg border p-3 ${st(r.status).border} ${st(r.status).bg}`}>
      <div className="flex items-start justify-between gap-3"><div className="font-medium text-white">{r.title}</div><Pill status={r.status} /></div>
      <p className="mt-1 text-[13px] text-slate-300">{r.body}</p>
      {r.artifact_url && (
        <a href={API + r.artifact_url} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-emerald-300 hover:underline">
          <ExternalLink size={14} /> Open offline packet
        </a>
      )}
      {r.evidence?.retest?.checks && (
        <ul className="mt-2 space-y-0.5 text-[12px] text-slate-300">
          {r.evidence.retest.checks.map((c: Any, i: number) => (
            <li key={i} className="flex gap-1.5">{c.passed ? <CircleCheck size={13} className="mt-0.5 shrink-0 text-emerald-400" /> : <TriangleAlert size={13} className="mt-0.5 shrink-0 text-red-400" />}{c.name}</li>
          ))}
        </ul>
      )}
      {r.retest_screenshot && <div className="mt-2"><Label>Independent Playwright retest screenshot</Label><Shot path={r.retest_screenshot} alt="Playwright retest of the offline packet" /></div>}
    </div>
  );
}

function CogneePanel({ cognee, extraction }: { cognee: Any; extraction: Any }) {
  if (!cognee) return null;
  return (
    <section className="rounded-xl border border-violet-500/30 bg-slate-900/60 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-white"><Brain size={16} className="text-violet-300" /> Cognee Cloud Memory</h2>
        <span className="font-mono text-[11px] text-slate-500">dataset {cognee.dataset} · {cognee.elapsed_s}s</span>
      </div>
      {cognee.error && <p className="mb-2 text-sm text-red-300">Cognee error: {cognee.error}</p>}
      <p className="mb-2 text-xs text-slate-400">
        Ingested this run: <span className="font-mono text-violet-200">{cognee.ingested_source_ids?.join(", ") || "none"}</span>
        {" · "}already persisted from earlier runs: <span className="font-mono text-violet-200">{cognee.already_persisted_source_ids?.join(", ") || "none"}</span>
      </p>
      <Label>Retrieved for “{cognee.query}”</Label>
      {(cognee.memory_snippets?.length ? cognee.memory_snippets : ["(no memory returned)"]).map((m: string, i: number) => (
        <pre key={i} className="mb-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-violet-500/30 bg-violet-500/5 p-2 font-mono text-[11px] text-violet-100">{m}</pre>
      ))}
      {cognee.memory_chunks?.length > 0 && (
        <details className="mb-2"><summary className="cursor-pointer text-xs text-slate-400">Persisted graph chunks ({cognee.memory_chunks.length})</summary>
          {cognee.memory_chunks.map((m: string, i: number) => <pre key={i} className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 font-mono text-[10px] text-slate-400">{m}</pre>)}
        </details>
      )}
      <Label>API calls</Label>
      <div className="space-y-1 font-mono text-[11px]">
        {cognee.steps?.map((s: Any, i: number) => (
          <details key={i} className="rounded border border-slate-800 bg-slate-950/60 px-2 py-1">
            <summary className="cursor-pointer text-slate-300">
              <span className={s.http_status && s.http_status < 300 ? "text-emerald-300" : s.http_status === 409 ? "text-amber-300" : s.http_status ? "text-red-300" : "text-slate-500"}>{s.http_status ?? "·"}</span>{" "}
              {s.step}{s.source_id ? ` ${s.source_id}` : ""}{s.skipped ? " (skipped — not needed)" : ""}{s.http_status === 409 ? " (already stored — replaced via update)" : ""}{s.waited_s ? ` (${s.waited_s}s)` : ""}
            </summary>
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[10px] text-slate-500">{s.response || s.note}</pre>
          </details>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-slate-500">{extraction?.note}</p>
    </section>
  );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-2xl rounded-xl border border-slate-700 bg-slate-900 p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="truncate font-semibold text-white">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-white"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

function AddModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [mode, setMode] = useState<"file" | "text">("file");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    setSaving(true); setErr(null);
    try {
      const fd = new FormData();
      if (title) fd.append("title", title);
      if (mode === "file") { if (!file) throw new Error("Choose a file first."); fd.append("file", file); }
      else { if (!content.trim()) throw new Error("Paste a note or URL first."); fd.append("content", content); }
      await api("/api/sources", { method: "POST", body: fd });
      onSaved();
    } catch (e: Any) { setErr(e.message); setSaving(false); }
  };
  const tab = (m: "file" | "text", label: string, Icon: Any) => (
    <button onClick={() => setMode(m)} className={`inline-flex flex-1 items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium ${mode === m ? "border-amber-400 bg-amber-400/10 text-amber-200" : "border-slate-700 text-slate-300 hover:bg-slate-800"}`}><Icon size={15} /> {label}</button>
  );
  return (
    <Modal title="Add Input" onClose={onClose}>
      <div className="space-y-3">
        <div className="flex gap-2">{tab("file", "Upload .ics / .eml / .pdf", Upload)}{tab("text", "Paste note or URL", FileText)}</div>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (optional)" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-amber-400" />
        {mode === "file" ? (
          <input type="file" accept=".ics,.eml,.pdf" onChange={(e) => setFile(e.target.files?.[0] || null)} className="w-full rounded-lg border border-dashed border-slate-600 bg-slate-950 p-4 text-sm text-slate-300 file:mr-3 file:rounded-md file:border-0 file:bg-slate-700 file:px-3 file:py-1.5 file:text-white" />
        ) : (
          <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={6} placeholder="Paste a note, or a single https:// URL" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-amber-400" />
        )}
        <p className="text-xs text-slate-500">Saved to SQLite and included in the next run. PDFs are parsed with Docling (30s timeout, then pypdf). Inputs with no independent check are reported as possible issues, never as verified.</p>
        {err && <p className="text-sm text-red-300">{err}</p>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">Cancel</button>
          <button onClick={save} disabled={saving} className="inline-flex items-center gap-2 rounded-lg bg-amber-400 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-amber-300 disabled:opacity-50">{saving && <LoaderCircle size={14} className="animate-spin" />} Save input</button>
        </div>
      </div>
    </Modal>
  );
}
