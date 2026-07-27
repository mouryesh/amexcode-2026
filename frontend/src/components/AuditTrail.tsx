import { useState } from "react";
import { api, type AuditRecord, type VerifyResult } from "../api";

interface Props {
  records: AuditRecord[];
}

/**
 * The audit panel. Every decision, action and system call, in chain order,
 * with each row's link to the one before it.
 *
 * The point this makes on screen: a DECLINE writes rows too. A ledger that
 * only records successful writes cannot audit a refusal, and a refusal is what
 * a member disputes later.
 */
export function AuditTrail({ records }: Props) {
  const [verdict, setVerdict] = useState<VerifyResult | null>(null);
  const [checking, setChecking] = useState(false);

  async function verify() {
    setChecking(true);
    try {
      setVerdict(await api.verify());
    } catch (e) {
      setVerdict({ status: "TAMPERED", reason: (e as Error).message });
    } finally {
      setChecking(false);
    }
  }

  const counts = records.reduce<Record<string, number>>((acc, r) => {
    acc[r.event_type] = (acc[r.event_type] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <section className="pane">
      <h2>
        Audit trail
        <span className="spacer" />
        <button className="mini" onClick={verify} disabled={checking}>
          {checking ? "checking…" : "Verify chain"}
        </button>
      </h2>

      <div className="log audit">
        {records.length === 0 ? (
          <p className="empty">
            Every decision, action and system call appears here as it is written.
          </p>
        ) : (
          <>
            <div className="counts">
              {Object.entries(counts).map(([k, n]) => (
                <span key={k} className="chip muted">
                  {k} × {n}
                </span>
              ))}
            </div>
            {records.map((r) => (
              <Row key={r.record_id} rec={r} />
            ))}
          </>
        )}
      </div>

      <div className={`verdict ${verdict ? verdict.status.toLowerCase() : ""}`}>
        {!verdict && "Chain not yet verified."}
        {verdict?.status === "OK" &&
          `Chain OK — ${verdict.records} records, head ${verdict.chain_head?.slice(0, 16)}…`}
        {verdict?.status === "TAMPERED" &&
          `TAMPERED at record ${verdict.broken_at_record_id ?? "?"} — ${verdict.reason ?? ""}`}
      </div>
    </section>
  );
}

function Row({ rec }: { rec: AuditRecord }) {
  const [open, setOpen] = useState(false);
  const body = rec.decision ?? rec.inputs;

  return (
    <div className="row" onClick={() => setOpen((o) => !o)}>
      <div className="row-head">
        <span className="rid">#{rec.record_id}</span>
        <span className="etype">{rec.event_type}</span>
        <span className="act">{rec.action}</span>
        <span className="spacer" />
        <span className="ts">{rec.occurred_at.slice(11, 19)}</span>
      </div>

      <div className="hashline">
        {rec.prev_hash.slice(0, 10)} → <b>{rec.record_hash.slice(0, 10)}</b>
      </div>

      {rec.decision && (
        <div className="rowdec">
          {rec.decision.outcome} · {rec.decision.reason_code}
        </div>
      )}

      {open && <pre>{JSON.stringify(body, null, 1)}</pre>}
    </div>
  );
}
